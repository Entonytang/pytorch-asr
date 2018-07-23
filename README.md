# ASR with PyTorch

This repository maintains an experimental code for speech recognition using [PyTorch](https://github.com/pytorch/pytorch) and [Kaldi](https://github.com/kaldi-asr/kaldi).
The Kaldi latgen decoder is integrated with PyTorch binding for CTC based acoustic model training.
The code was tested with Python 3.7 and PyTorch 0.5.

## Installation

We recommend [pyenv](https://github.com/pyenv/pyenv). We assume you already have pyenv and Python 3.7.0, PyTorch 0.5.0a0, and Kaldi 5.3+.

Currently, AdamW and SGDR patch ([PR #4429](https://github.com/pytorch/pytorch/pull/4429) and [PR #7821](https://github.com/pytorch/pytorch/pull/7821)) has to be applied
to Pytorch 0.5.0a0 ([git branch v0.4.1](https://github.com/pytorch/pytorch/tree/v0.4.1)) to use AdamW or SGDR optimizer. If you don't want to use this, please fix
the each optimizer setup in `model.py` files to avoid errors.
To avoid the `-fPIC` related compile error, you have to configure Kaldi with `--shared` option when you install it.
Do not forget to set `pyenv local 3.7.0` in the local repo if you decide to use pyenv.

Download:
```
$ git clone https://github.com/jinserk/pytorch-asr.git
```

Install required Python modules:
```
$ cd pytorch-asr
$ pip install -r requirements.txt
```

Modify the Kaldi path in `_path.py`:
```
$ cd asr/kaldi
$ vi _path.py

KALDI_ROOT = <kaldi-installation-path>
```

Build up PyTorch-binding of Kaldi decoder:
```
$ python build.py
```
This takes a while to download the Kaldi's official ASpIRE chain model and its post-processing.


## Training

Pytorch-asr is targeted to develop a framework supporting multiple acoustic models. You have to specify one of model to train or predict.
Currently, only `resnet_ctc` and `resnet_ed` model works for training and prediction. Try this model first.

If you do training for the first time, you have to prepare the dataset. Currently we support only the Kaldi's ASpIRE recipe datatset, originated from LDC's fisher corpus.
```
$ python prepare.py aspire
```

To train CTC model, you need to make the ctc labeling files from each utterence's transcript.
```
$ cd asr/kaldi
$ python prep_ctc_trans.py ../../data/aspire
```

Start a new training with:
```
$ python train.py <model-name> --use-cuda
```
where `<model_name>` can be either of `resnet_ctc` or `resnet_ed`.
check `--help` option to see which parameters are available for the model.

If you want to resume training from a saved model file:
```
$ python train.py <model-name> --use-cuda --continue-from <model-file>
```

You can use `--tensorboard` option to see the loss propagation. You have to install Tensorboard and TensorboardX from pip to use it.


## Prediction

You can predict a sample with trained model file:
```
$ python predict.py <model-name> --continue-from <model-file> <target-wav-file1> <target-wav-file2> ...
```

