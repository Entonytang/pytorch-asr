#!python
import os
import sys
from pathlib import Path
from tqdm import tqdm
import multiprocessing as mp

import numpy as np
import torch
import torch.nn as nn
import torch.distributed as dist

import apex.parallel
from apex.fp16_utils import *

import torchvision.utils as tvu
from warpctc_pytorch import CTCLoss
import torchnet as tnt
import Levenshtein as Lev

from asr.utils.logger import logger
from asr.utils.misc import onehot2int, remove_duplicates, get_model_file_path
from asr.utils.lr_scheduler import CosineAnnealingWithRestartsLR
from asr.utils import params as p

from asr.kaldi.latgen import LatGenCTCDecoder


OPTIMIZER_TYPES = set([
    "sgd",
    "sgdr",
    "adamw",
])


def init_distributed(use_cuda, backend="nccl", init="slurm"):
    try:
        mp.set_start_method('spawn') # spawn, forkserver, and fork
    except RuntimeError:
        pass

    try:
        if init == "slurm":
            rank = int(os.environ['SLURM_PROCID'])
            world_size = int(os.environ['SLURM_NTASKS'])
            local_rank = int(os.environ['SLURM_LOCALID'])
            #maser_node = os.environ['SLURM_TOPOLOGY_ADDR']
            #maser_port = '23456'
        elif init == "ompi":
            rank = int(os.environ['OMPI_COMM_WORLD_RANK'])
            world_size = int(os.environ['OMPI_COMM_WORLD_SIZE'])
            local_rank = int(os.environ['OMPI_COMM_WORLD_LOCAL_RANK'])

        if use_cuda:
            torch.cuda.set_device(local_rank)
            print(f"set cuda device to cuda:{local_rank}")

        master_node = os.environ["MASTER_ADDR"]
        master_port = os.environ["MASTER_PORT"]
        init_method = f"tcp://{master_node}:{master_port}"
        #init_method = "env://"
        dist.init_process_group(backend=backend, init_method=init_method, world_size=world_size, rank=rank)
        print(f"initialized as {rank}/{world_size} via {init_method}")
    except:
        print(f"initialized as single process")


def is_distributed():
    try:
        return (dist.get_world_size() > 1)
    except:
        return False


def get_rank():
    try:
        return dist.get_rank()
    except:
        return None


def set_seed(seed=None):
    if seed is not None:
        logger.info(f"set random seed to {seed}")
        torch.manual_seed(seed)
        np.random.seed(seed)
        if args.use_cuda:
            torch.cuda.manual_seed(seed)


class Trainer:

    def __init__(self, model, init_lr=1e-4, max_norm=400, use_cuda=False,
                 fp16=False, log_dir='logs', model_prefix='model',
                 checkpoint=False, continue_from=None, opt_type="sgdr",
                 *args, **kwargs):
        if fp16:
            if not use_cuda:
                raise RuntimeError

        # training parameters
        self.init_lr = init_lr
        self.max_norm = max_norm
        self.use_cuda = use_cuda
        self.fp16 = fp16
        self.log_dir = log_dir
        self.model_prefix = model_prefix
        self.checkpoint = checkpoint
        self.epoch = 0

        # prepare visdom
        if logger.visdom is not None:
            logger.visdom.add_plot(title=f'train', xlabel='epoch', ylabel='loss')
            logger.visdom.add_plot(title=f'validate', xlabel='epoch', ylabel='LER')

        # setup model
        self.model = model
        if self.use_cuda:
            logger.debug("using cuda")
            self.model.cuda()

        # setup loss
        self.loss = CTCLoss(blank=0, size_average=True, length_average=True)

        # setup optimizer
        assert opt_type in OPTIMIZER_TYPES
        parameters = self.model.parameters()
        if opt_type == "sgd":
            logger.debug("using SGD")
            self.optimizer = torch.optim.SGD(parameters, lr=self.init_lr, momentum=0.9)
            self.lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(self.optimizer, T_max=5)
        elif opt_type == "sgdr":
            logger.debug("using SGDR")
            self.optimizer = torch.optim.SGD(parameters, lr=self.init_lr, momentum=0.9)
            #self.lr_scheduler = torch.optim.lr_scheduler.StepLR(self.optimizer, step_size=1, gamma=0.5)
            self.lr_scheduler = CosineAnnealingWithRestartsLR(self.optimizer, T_max=5, T_mult=2)
        elif opt_type == "adam":
            logger.debug("using AdamW")
            self.optimizer = torch.optim.Adam(parameters, lr=self.init_lr, betas=(0.9, 0.999), eps=1e-8,
                                              weight_decay=0.0005, l2_reg=False)
            self.lr_scheduler = None

        # setup decoder for test
        self.decoder = LatGenCTCDecoder()

        # load from pre-trained model if needed
        if continue_from is not None:
            self.load(continue_from)

        # FP16 and distributed after load
        if self.fp16:
            self.model = network_to_half(self.model)
            self.optimizer = FP16_Optimizer(self.optimizer, static_loss_scale=128.)

        if is_distributed():
            if self.use_cuda:
                local_rank = torch.cuda.current_device()
                if fp16:
                    self.model = apex.parallel.DistributedDataParallel(self.model)
                else:
                    self.model = nn.parallel.DistributedDataParallel(self.model,
                                                                     device_ids=[local_rank],
                                                                     output_device=local_rank)
            else:
                self.model = nn.parallel.DistributedDataParallel(self.model)


    def __get_model_name(self, desc):
        return str(get_model_file_path(self.log_dir, self.model_prefix, desc))

    def __remove_ckpt_files(self, epoch):
        for ckpt in Path(self.log_dir).rglob(f"*_epoch_{epoch:03d}_ckpt_*"):
            ckpt.unlink()

    def unit_train(self, data):
        raise NotImplementedError

    def train_epoch(self, data_loader):
        self.model.train()
        num_ckpt = int(np.ceil(len(data_loader) / 10))
        meter_loss = tnt.meter.MovingAverageValueMeter(len(data_loader) // 100 + 1)
        #meter_accuracy = tnt.meter.ClassErrorMeter(accuracy=True)
        #meter_confusion = tnt.meter.ConfusionMeter(p.NUM_CTC_LABELS, normalized=True)
        if self.lr_scheduler is not None:
            self.lr_scheduler.step()
            logger.debug(f"current lr = {self.lr_scheduler.get_lr()}")
        if is_distributed() and data_loader.sampler is not None:
            data_loader.sampler.set_epoch(self.epoch)

        # count the number of supervised batches seen in this epoch
        t = tqdm(enumerate(data_loader), total=len(data_loader), desc="training")
        for i, (data) in t:
            loss_value = self.unit_train(data)
            meter_loss.add(loss_value)
            t.set_description(f"training (loss: {meter_loss.value()[0]:.3f})")
            t.refresh()
            #self.meter_accuracy.add(ys_int, ys)
            #self.meter_confusion.add(ys_int, ys)

            if 0 < i < len(data_loader) and i % num_ckpt == 0:
                if not is_distributed() or (is_distributed() and dist.get_rank() == 0):
                    title = "train"
                    x = self.epoch + i / len(data_loader)
                    if logger.visdom is not None:
                        logger.visdom.add_point(title=title, x=x, y=meter_loss.value()[0])
                    if logger.tensorboard is not None:
                        logger.tensorboard.add_graph(self.model, xs)
                        xs_img = tvu.make_grid(xs[0, 0], normalize=True, scale_each=True)
                        logger.tensorboard.add_image('xs', x, xs_img)
                        ys_hat_img = tvu.make_grid(ys_hat[0].transpose(0, 1), normalize=True, scale_each=True)
                        logger.tensorboard.add_image('ys_hat', x, ys_hat_img)
                        logger.tensorboard.add_scalars(title, x, { 'loss': meter_loss.value()[0], })
                if self.checkpoint:
                    logger.info(f"training loss at epoch_{self.epoch:03d}_ckpt_{i:07d}: "
                                f"{meter_loss.value()[0]:5.3f}")
                    if not is_distributed() or (is_distributed() and dist.get_rank() == 0):
                        self.save(self.__get_model_name(f"epoch_{self.epoch:03d}_ckpt_{i:07d}"))
            #input("press key to continue")

        self.epoch += 1
        logger.info(f"epoch {self.epoch:03d}: "
                    f"training loss {meter_loss.value()[0]:5.3f} ")
                    #f"training accuracy {meter_accuracy.value()[0]:6.3f}")
        if not is_distributed() or (is_distributed() and dist.get_rank() == 0):
            self.save(self.__get_model_name(f"epoch_{self.epoch:03d}"))
            self.__remove_ckpt_files(self.epoch-1)

    def unit_validate(self, data):
        raise NotImplementedError

    def validate(self, data_loader):
        "validate with label error rate by the edit distance between hyps and refs"
        self.model.eval()
        with torch.no_grad():
            N, D = 0, 0
            t = tqdm(enumerate(data_loader), total=len(data_loader), desc="validating")
            for i, (data) in t:
                hyps, refs = self.unit_validate(data)
                # calculate ler
                N += self.edit_distance(refs, hyps)
                D += sum(len(r) for r in refs)
                ler = N * 100. / D
                t.set_description(f"validating (LER: {ler:.2f} %)")
                t.refresh()
            logger.info(f"validating at epoch {self.epoch:03d}: LER {ler:.2f} %")

            if not is_distributed() or (is_distributed() and dist.get_rank() == 0):
                title = f"validate"
                x = self.epoch - 1 + i / len(data_loader)
                if logger.visdom is not None:
                    logger.visdom.add_point(title=title, x=x, y=ler)
                if logger.tensorboard is not None:
                    logger.tensorboard.add_scalars(title, x, { 'LER': ler, })

    def unit_test(self, data):
        raise NotImplementedError

    def test(self, data_loader):
        "test with word error rate by the edit distance between hyps and refs"
        self.model.eval()
        with torch.no_grad():
            N, D = 0, 0
            t = tqdm(enumerate(data_loader), total=len(data_loader), desc="testing")
            for i, (data) in t:
                hyps, refs = self.unit_test(data)
                # calculate wer
                N += self.edit_distance(refs, hyps)
                D += sum(len(r) for r in refs)
                wer = N * 100. / D
                t.set_description(f"testing (WER: {wer:.2f} %)")
                t.refresh()
            logger.info(f"testing at epoch {self.epoch:03d}: WER {wer:.2f} %")

    def edit_distance(self, refs, hyps):
        assert len(refs) == len(hyps)
        n = 0
        for ref, hyp in zip(refs, hyps):
            r = [chr(c) for c in ref]
            h = [chr(c) for c in hyp]
            n += Lev.distance(''.join(r), ''.join(h))
        return n

    def save(self, file_path, **kwargs):
        Path(file_path).parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        logger.info(f"saving the model to {file_path}")
        states = kwargs
        states["epoch"] = self.epoch
        if is_distributed():
            model_state_dict = self.model.state_dict()
            strip_prefix = 9 if self.fp16 else 7
            # remove "module.1." prefix from keys
            states["model"] = {k[strip_prefix:]: v for k, v in model_state_dict.items()}
        else:
            states["model"] = self.model.state_dict()
        states["optimizer"] = self.optimizer.state_dict()
        states["lr_scheduler"] = self.lr_scheduler.state_dict()
        torch.save(states, file_path)

    def load(self, file_path):
        if isinstance(file_path, str):
            file_path = Path(file_path)
        if not file_path.exists():
            logger.error(f"no such file {file_path} exists")
            sys.exit(1)
        logger.info(f"loading the model from {file_path}")
        to_device = f"cuda:{torch.cuda.current_device()}" if self.use_cuda else "cpu"
        states = torch.load(file_path, map_location=to_device)
        self.epoch = states["epoch"]
        self.model.load_state_dict(states["model"])
        self.optimizer.load_state_dict(states["optimizer"])
        self.lr_scheduler.load_state_dict(states["lr_scheduler"])


class NonSplitTrainer(Trainer):
    """training model for overall utterance spectrogram as a single image"""

    def unit_train(self, data):
        xs, ys, frame_lens, label_lens, filenames, _ = data
        try:
            if self.use_cuda:
                xs = xs.cuda(non_blocking=True)
            ys_hat = self.model(xs)
            if self.fp16:
                ys_hat = ys_hat.float()
            ys_hat = ys_hat.transpose(0, 1).contiguous()  # TxNxH
            #frame_lens = torch.ceil(frame_lens.float() / FRAME_REDUCE_FACTOR).int()
            #torch.set_printoptions(threshold=5000000)
            #print(ys_hat.shape, frame_lens, ys.shape, label_lens)
            #print(onehot2int(ys_hat).squeeze(), ys)
            loss = self.loss(ys_hat, ys, frame_lens, label_lens)
            loss_value = loss.item()
            inf = float("inf")
            if loss_value == inf or loss_value == -inf:
                logger.warning("received an inf loss, setting loss value to 0")
                loss_value = 0
            self.optimizer.zero_grad()
            if self.fp16:
                self.optimizer.backward(loss)
                self.optimizer.clip_master_grads(self.max_norm)
            else:
                loss.backward()
                nn.utils.clip_grad_norm_(self.model.parameters(), self.max_norm)
            self.optimizer.step()
            torch.cuda.synchronize()
            del loss
            return loss_value
        except Exception as e:
            raise
            print(e)
            print(filenames, frame_lens, label_lens)
            return 0

    def unit_validate(self, data):
        xs, ys, frame_lens, label_lens, filenames, _ = data
        if self.use_cuda:
            xs = xs.cuda(non_blocking=True)
        ys_hat = self.model(xs)
        if self.fp16:
            ys_hat = ys_hat.float()
        # convert likes to ctc labels
        #frame_lens = torch.ceil(frame_lens.float() / FRAME_REDUCE_FACTOR).int()
        hyps = [onehot2int(yh[:s]).squeeze() for yh, s in zip(ys_hat, frame_lens)]
        hyps = [remove_duplicates(h, blank=0) for h in hyps]
        # slice the targets
        pos = torch.cat((torch.zeros((1, ), dtype=torch.long), torch.cumsum(label_lens, dim=0)))
        refs = [ys[s:l] for s, l in zip(pos[:-1], pos[1:])]
        return hyps, refs

    def unit_test(self, data):
        xs, ys, frame_lens, label_lens, filenames, texts = data
        if self.use_cuda:
            xs = xs.cuda(non_blocking=True)
        ys_hat = self.model(xs)
        if self.fp16:
            ys_hat = ys_hat.float()
        #frame_lens = torch.ceil(frame_lens.float() / FRAME_REDUCE_FACTOR).int()
        # latgen decoding
        loglikes = torch.log(ys_hat)
        if self.use_cuda:
            loglikes = loglikes.cpu()
        words, alignment, w_sizes, a_sizes = self.decoder(loglikes, frame_lens)
        hyps = [w[:s] for w, s in zip(words, w_sizes)]
        # convert target texts to word indices
        w2i = self.decoder.labeler.word2idx
        refs = [[w2i(w.strip()) for w in t.strip().split()] for t in texts]
        return hyps, refs


class SplitTrainer(Trainer):
    """ training model for splitting utterance into multiple images
        single image stands for localized timing segment corresponding to frame output
    """

    def unit_train(self, data):
        xs, ys, frame_lens, label_lens, filenames, _ = data
        try:
            if self.use_cuda:
                xs = xs.cuda(non_blocking=True)
            ys_hat = self.model(xs)
            if self.fp16:
                ys_hat = ys_hat.float()
            ys_hat = ys_hat.unsqueeze(dim=0).transpose(1, 2)
            pos = torch.cat((torch.zeros((1, ), dtype=torch.long), torch.cumsum(frame_lens, dim=0)))
            ys_hats = [ys_hat.narrow(2, p, l).clone() for p, l in zip(pos[:-1], frame_lens)]
            max_len = torch.max(frame_lens)
            ys_hats = [nn.ConstantPad1d((0, max_len-yh.size(2)), 0)(yh) for yh in ys_hats]
            ys_hat = torch.cat(ys_hats).transpose(1, 2).transpose(0, 1)
            loss = self.loss(ys_hat, ys, frame_lens, label_lens)
            loss_value = loss.item()
            inf = float("inf")
            if loss_value == inf or loss_value == -inf:
                logger.warning("received an inf loss, setting loss value to 0")
                loss_value = 0
            self.optimizer.zero_grad()
            if self.fp16:
                self.optimizer.backward(loss)
                self.optimizer.clip_master_grads(self.max_norm)
            else:
                loss.backward()
                nn.utils.clip_grad_norm_(self.model.parameters(), self.max_norm)
            self.optimizer.step()
            del loss
        except Exception as e:
            print(filenames, frame_lens, label_lens)
            raise
        return loss_value

    def unit_validate(self, data):
        xs, ys, frame_lens, label_lens, filenames, _ = data
        if self.use_cuda:
            xs = xs.cuda(non_blocking=True)
        ys_hat = self.model(xs)
        if self.fp16:
            ys_hat = ys_hat.float()
        pos = torch.cat((torch.zeros((1, ), dtype=torch.long), torch.cumsum(frame_lens, dim=0)))
        ys_hat = [ys_hat.narrow(0, p, l).clone() for p, l in zip(pos[:-1], frame_lens)]
        # convert likes to ctc labels
        hyps = [onehot2int(yh[:s]).squeeze() for yh, s in zip(ys_hat, frame_lens)]
        hyps = [remove_duplicates(h, blank=0) for h in hyps]
        # slice the targets
        pos = torch.cat((torch.zeros((1, ), dtype=torch.long), torch.cumsum(label_lens, dim=0)))
        refs = [ys[s:l] for s, l in zip(pos[:-1], pos[1:])]
        return hyps, refs

    def unit_test(self, data):
        xs, ys, frame_lens, label_lens, filenames, texts = data
        if self.use_cuda:
            xs = xs.cuda(non_blocking=True)
        ys_hat = self.model(xs)
        if self.fp16:
            ys_hat = ys_hat.float()
        ys_hat = ys_hat.unsqueeze(dim=0).transpose(1, 2)
        pos = torch.cat((torch.zeros((1, ), dtype=torch.long), torch.cumsum(frame_lens, dim=0)))
        ys_hats = [ys_hat.narrow(2, p, l).clone() for p, l in zip(pos[:-1], frame_lens)]
        max_len = torch.max(frame_lens)
        ys_hats = [nn.ConstantPad1d((0, max_len-yh.size(2)), 0)(yh) for yh in ys_hats]
        ys_hat = torch.cat(ys_hats).transpose(1, 2)
        # latgen decoding
        loglikes = torch.log(ys_hat)
        if self.use_cuda:
            loglikes = loglikes.cpu()
        words, alignment, w_sizes, a_sizes = self.decoder(loglikes, frame_lens)
        hyps = [w[:s] for w, s in zip(words, w_sizes)]
        # convert target texts to word indices
        w2i = self.decoder.labeler.word2idx
        refs = [[w2i(w.strip()) for w in t.strip().split()] for t in texts]
        return hyps, refs

if __name__ == "__main__":
    pass
