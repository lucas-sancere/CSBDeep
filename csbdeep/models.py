from __future__ import print_function, unicode_literals, absolute_import, division
from six.moves import range, zip, map, reduce, filter

import argparse
import datetime

from .utils import _raise, consume, Path, load_json, save_json, normalize_mi_ma, from_tensor, to_tensor, tensor_num_channels
import warnings
import numpy as np
# from collections import namedtuple
import tensorflow as tf

from . import nets, train
from .predict import tiled_prediction, PadAndCropResizer


class Config(argparse.Namespace):
    """Default configuration for a (standard) CARE network.

    This configuration is meant to be used with :class:`csbdeep.models.CARE`.

    Parameters
    ----------
    n_dim : int
        Dimensionality of input images (2 or 3).
    n_channel_in : int
        Number of channels of given input image.
    n_channel_out : int
        Number of channels of predicted output image.
    probabilistic : bool
        Probabilistic prediction of per-pixel Laplace distributions or
        typical regression of per-pixel scalar values.
    kwargs : dict
        Overwrite (or add) configuration attributes (see below).

    Example
    -------
    >>> config = Config(2, probabilistic=True, unet_n_depth=3)

    Attributes
    ----------
    unet_residual : bool
        Parameter `residual` of :func:`csbdeep.nets.common_unet`. Default: ``n_channel_in == n_channel_out``
    unet_n_depth : int
        Parameter `n_depth` of :func:`csbdeep.nets.common_unet`. Default: ``2``
    unet_kern_size : int
        Parameter `kern_size` of :func:`csbdeep.nets.common_unet`. Default: ``5 if n_dim==2 else 3``
    unet_n_first : int
        Parameter `n_first` of :func:`csbdeep.nets.common_unet`. Default: ``32``
    unet_last_activation : str
        Parameter `last_activation` of :func:`csbdeep.nets.common_unet`. Default: ``linear``
    train_loss : str
        Name of training loss. Default: ``'laplace' if probabilistic else 'mae'``
    train_epochs : int
        Number of training epochs. Default: ``100``
    train_steps_per_epoch : int
        Number of parameter update steps per epoch. Default: ``400``
    train_learning_rate : float
        Learning rate for training. Default: ``0.0004``
    train_batch_size : int
        Batch size for training. Default: ``16``
    train_tensorboard : bool
        Enable TensorBoard for monitoring training progress. Default: ``True``
    train_checkpoint : str
        Name of checkpoint file for model weights (only best are saved); set to ``None`` to disable. Default: ``weights_best.h5``
    train_reduce_lr : dict
        Parameter :class:`dict` of ReduceLROnPlateau_ callback; set to ``None`` to disable. Default: ``{'factor': 0.5, 'patience': 10}``

        .. _ReduceLROnPlateau: https://keras.io/callbacks/#reducelronplateau
    """

    def __init__(self, n_dim, n_channel_in=1, n_channel_out=1, probabilistic=False, **kwargs):
        """See class docstring."""
        n_dim in (2,3) or _raise(ValueError())

        self.n_dim                 = n_dim
        self.n_channel_in          = n_channel_in
        self.n_channel_out         = n_channel_out
        self.probabilistic         = probabilistic

        self.unet_residual         = self.n_channel_in == self.n_channel_out
        self.unet_n_depth          = 2
        self.unet_kern_size        = 5 if self.n_dim==2 else 3
        self.unet_n_first          = 32
        self.unet_last_activation  = 'linear'
        self.unet_input_shape      = self.n_dim*(None,) + (self.n_channel_in,)

        self.train_loss            = 'laplace' if self.probabilistic else 'mae'
        self.train_epochs          = 100
        self.train_steps_per_epoch = 400
        self.train_learning_rate   = 0.0004
        self.train_batch_size      = 16
        self.train_tensorboard     = True
        self.train_checkpoint      = 'weights_best.h5'
        self.train_reduce_lr       = {'factor': 0.5, 'patience': 10}

        for k in kwargs:
            setattr(self, k, kwargs[k])

        # TODO: param checks



class CARE(object):
    """Standard CARE network for image restoration and enhancement.

    Uses a convolutional neural network created by :func:`csbdeep.nets.common_unet`.
    Note that isotropic reconstruction and manifold extraction/projection are not supported here.


    Parameters
    ----------
    config : :class:`csbdeep.models.Config` or None
        Configuration for CARE network. Will be saved to disk as JSON (``config.json``).
        If set to ``None``, will be loaded from disk (must exist).
    name : str or None
        Model name. Uses a timestamp if set to ``None`` (default).
    outdir : str
        Output directory that contains (or will contain) a folder with the given model name.

    Raises
    ------
    FileNotFoundError
        If ``config=None`` and config cannot be loaded from disk.

    Example
    -------
    >>> model = CARE(config, 'my_model')
    """

    def __init__(self, config, name=None, outdir='.'):
        """See class docstring."""
        self.config = config
        self.outdir = Path(outdir)
        self.name = name
        self._set_logdir()
        self._model_prepared = False
        self.keras_model = self._build()


    def _set_logdir(self):
        if self.name is None:
            self.name = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S.%f")
        self.logdir = self.outdir / self.name

        config_file =  self.logdir / 'config.json'
        if self.config is None:
            if not config_file.exists():
                raise FileNotFoundError("config file doesn't exist: %s" % str(config_file.resolve()))
            else:
                config_dict = load_json(config_file)
                self.config = Config(**config_dict)
        else:
            if self.logdir.exists():
                warnings.warn('output path for model already exists, files may be overwritten: %s' % str(self.logdir.resolve()))
            self.logdir.mkdir(parents=True, exist_ok=True)
            save_json(vars(self.config), config_file)


    def _build(self):
        return nets.common_unet(
            n_dim           = self.config.n_dim,
            n_channel_out   = self.config.n_channel_out,
            prob_out        = self.config.probabilistic,
            residual        = self.config.unet_residual,
            n_depth         = self.config.unet_n_depth,
            kern_size       = self.config.unet_kern_size,
            n_first         = self.config.unet_n_first,
            last_activation = self.config.unet_last_activation,
        )(self.config.unet_input_shape)


    def load_weights(self, name='weights_best.h5'):
        """Load neural network weights from model folder.

        Parameters
        ----------
        name : str
            Name of HDF5 weight file (as saved during or after training).
        """
        self.keras_model.load_weights(str(self.logdir/name))


    def prepare_for_training(self, optimizer=None, **kwargs):
        """Prepare for neural network training.

        Calls :func:`csbdeep.train.prepare_model` and creates
        `Keras Callbacks <https://keras.io/callbacks/>`_ to be used for training.

        Note that this method will be implicitly called once by :func:`train`
        (with default arguments) if not done so explicitly beforehand.

        Parameters
        ----------
        optimizer : obj or None
            Instance of a `Keras Optimizer <https://keras.io/optimizers/>`_ to be used for training.
            If ``None`` (default), uses ``Adam`` with the learning rate specified in ``config``.
        kwargs : dict
            Additional arguments for :func:`csbdeep.train.prepare_model`.

        """
        if optimizer is None:
            from keras.optimizers import Adam
            optimizer = Adam(lr=self.config.train_learning_rate)
        self.callbacks = train.prepare_model(self.keras_model, optimizer, self.config.train_loss, **kwargs)

        if self.config.train_checkpoint is not None:
            from keras.callbacks import ModelCheckpoint
            self.callbacks.append(ModelCheckpoint(str(self.logdir / self.config.train_checkpoint), save_best_only=True, save_weights_only=True))

        if self.config.train_tensorboard:
            from csbdeep.tf import CARETensorBoard
            self.callbacks.append(CARETensorBoard(log_dir=str(self.logdir), prefix_with_timestamp=False, n_images=3, write_images=True, prob_out=self.config.probabilistic))

        if self.config.train_reduce_lr is not None:
            from keras.callbacks import ReduceLROnPlateau
            self.callbacks.append(ReduceLROnPlateau(**self.config.train_reduce_lr, verbose=True))

        self._model_prepared = True


    def train(self, X,Y, validation_data, epochs=None, steps_per_epoch=None):
        """Train the neural network with the given data.

        Parameters
        ----------
        X : :class:`numpy.ndarray`
            Array of source images
        Y : :class:`numpy.ndarray`
            Array of target images
        validation_data : tuple(:class:`numpy.ndarray`, :class:`numpy.ndarray`)
            Tuple of arrays for source and target validation images
        epochs : int
            Optional argument to use instead of the value from ``config``.
        steps_per_epoch : int
            Optional argument to use instead of the value from ``config``.

        """
        if not self._model_prepared:
            self.prepare_for_training()

        # if self.logdir.exists():
        #     warnings.warn('output path for model already exists, files may be overwritten during training: %s' % str(self.logdir.resolve()))

        if epochs is None:
            epochs = self.config.train_epochs
        if steps_per_epoch is None:
            steps_per_epoch = self.config.train_steps_per_epoch

        training_data = train.DataWrapper(X, Y, self.config.train_batch_size)

        history = self.keras_model.fit_generator(generator=training_data, validation_data=validation_data,
                                                 epochs=epochs, steps_per_epoch=steps_per_epoch,
                                                 callbacks=self.callbacks, verbose=1)

        self.keras_model.save_weights(str(self.logdir/'weights_final.h5'))
        return history


    def export_TF(self):
        """Export neural network via :func:`csbdeep.tf.export_SavedModel`."""
        from csbdeep.tf import export_SavedModel
        fout = self.logdir / 'TF_SavedModel.zip'
        export_SavedModel(self.keras_model, str(fout))
        print("\nModel exported in TensorFlow's SavedModel format:\n%s" % str(fout.resolve()))


    def predict(self, img, normalizer, resizer=PadAndCropResizer(), channel=None, n_tiles=1, **kwargs):
        """TODO."""
        if channel is None:
            self.config.n_channel_in == 1 or _raise(ValueError())
        else:
            -img.ndim <= channel < img.ndim or _raise(ValueError())
            if channel < 0:
                channel %= img.ndim
            self.config.n_channel_in == img.shape[channel] or _raise(ValueError())

        n_channel_predicted = self.config.n_channel_out * (2 if self.config.probabilistic else 1)

        # resize: make divisible by power of 2 to allow downsampling steps in unet
        div_n = 2 ** self.config.unet_n_depth
        x = resizer.before(img,div_n,channel)

        # normalize
        x = normalizer.before(x,channel)

        # prediction function
        def _predict(x):
            return from_tensor(self.keras_model.predict(to_tensor(x,channel=channel),**kwargs),channel=0)

        done = False
        while not done:
            try:
                if n_tiles == 1:
                    x = _predict(x)
                else:
                    if channel is None:
                        shape_out = (n_channel_predicted,) + x.shape
                    else:
                        shape_out = (n_channel_predicted,) + tuple((s for i,s in enumerate(x.shape) if i != channel))

                    x = tiled_prediction(_predict, x, shape_out, channel=channel, n_tiles=n_tiles)
                done = True
            except tf.errors.ResourceExhaustedError:
                n_tiles = max(4, 2*n_tiles)
                print('Out of memory, retrying with n_tiles = %d' % n_tiles)

        x.shape[0] == n_channel_predicted or _raise(ValueError())

        x = resizer.after(x,channel=0)

        # separate mean and scale
        if self.config.probabilistic:
            _n = n_channel_predicted // 2
            mean, scale = x[:_n], x[_n:]
        else:
            mean, scale = x, None

        if channel is not None:
            # move output channel to same dimension as in input image
            mean = np.moveaxis(mean, 0, channel)
            if self.config.probabilistic:
                scale = np.moveaxis(scale, 0, channel)
        else:
            # remove dummy channel dimension altogether
            if self.config.n_channel_out == 1:
                mean = mean[0]
                if self.config.probabilistic:
                    scale = scale[0]

        if normalizer.do_after:
            self.config.n_channel_in == self.config.n_channel_out or _raise(ValueError())
            mean, scale = normalizer.after(mean, scale)

        return mean, scale
