# neutorch
Neuron segmentation and synapse detection using PyTorch

> :warning: **This package is still under development, it was customized to process a Electron Microscopy volume. Currently, it is not suitable for other image volume processing in production.**

# Install
    python setup.py install

# Usage
## Train
We provide command line tool after installation. Check out the options provided:

    neutrain --help

# Credit
The design and implementation is based on some other packages.
- [PyTorchUtils](https://github.com/nicholasturner1/PyTorchUtils)
- [DeepEM](https://github.com/seung-lab/DeepEM)
- [torchio](https://github.com/fepegar/torchio)
- [pytorch_connectomics](https://github.com/zudi-lin/pytorch_connectomics)
