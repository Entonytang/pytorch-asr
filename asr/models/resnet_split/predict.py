#!python
import sys
import argparse
from pathlib import Path

import numpy as np
import torch

from asr.utils.dataset import SplitPredictDataset
from asr.utils.dataloader import SplitPredictDataLoader
from asr.utils.logger import logger, set_logfile, version_log
from asr.utils import params as p
from asr.kaldi.latgen import LatGenCTCDecoder

from ..predictor import SplitPredictor
from .network import resnet50, resnet101


def predict(argv):
    parser = argparse.ArgumentParser(description="ResNet prediction")
    parser.add_argument('--verbose', default=False, action='store_true', help="set true if you need to check AM output")
    parser.add_argument('--use-cuda', default=False, action='store_true', help="use cuda")
    parser.add_argument('--batch-size', default=8, type=int, help="number of simultaneous decoding")
    parser.add_argument('--log-dir', default='./logs_resnet_ctc', type=str, help="filename for logging the outputs")
    parser.add_argument('--continue-from', type=str, help="model file path to make continued from")
    parser.add_argument('wav_files', type=str, nargs='+', help="list of wav_files for prediction")

    args = parser.parse_args(argv)

    set_logfile(Path(args.log_dir, "predict.log"))
    version_log(args)

    if args.continue_from is None:
        logger.error("model name is missing: add '--continue-from <model-name>' in options")
        #parser.print_help()
        sys.exit(1)

    model = resnet50(num_classes=p.NUM_CTC_LABELS)
    predictor = SplitPredictor(model, **vars(args))

    dataset = SplitPredictDataset(args.wav_files)
    dataloader = SplitPredictDataLoader(dataset=dataset, batch_size=args.batch_size,
                                        pin_memory=args.use_cuda)

    # run prediction
    predictor.decode(dataloader)


if __name__ == "__main__":
    pass
