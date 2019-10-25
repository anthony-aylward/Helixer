import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)

from abc import ABC, abstractmethod
import os
import sys
try:
    import nni
except ImportError:
    pass
import time
import h5py
import random
import argparse
import datetime
import importlib
import numpy as np
import tensorflow as tf
from pprint import pprint
from functools import partial
from sklearn.preprocessing import MinMaxScaler

from keras_layer_normalization import LayerNormalization
from keras.callbacks import EarlyStopping, ModelCheckpoint, History, CSVLogger, Callback
from keras import optimizers
from keras import backend as K
from keras.models import load_model
from keras.utils import multi_gpu_model, Sequence

from F1Scores import F1Calculator
from ConfusionMatrix import ConfusionMatrix


def acc_region(y_true, y_pred, col, value):
    non_zero_pad_mask = K.any(tf.equal(y_true, tf.constant(1.0)), axis=-1)
    content_mask = tf.equal(y_true[:, :, :, col], tf.constant(value))
    mask = tf.logical_and(non_zero_pad_mask, content_mask)

    y_true = K.argmax(tf.boolean_mask(y_true, mask), axis=-1)
    y_pred = K.argmax(tf.boolean_mask(y_pred, mask), axis=-1)

    errors = K.cast(K.equal(y_true, y_pred), K.floatx())
    error_return = tf.cond(tf.equal(tf.size(errors), 0),
                           lambda: tf.constant(0.0), lambda: K.mean(errors))
    return error_return


def acc_g_oh(y_true, y_pred):
    return acc_region(y_true, y_pred, 0, 0.0)


def acc_ig_oh(y_true, y_pred):
    return acc_region(y_true, y_pred, 0, 1.0)


# Callbacks have to be done seperately for train/test as the way they are called by Keras
# is buggy currently
class ConfusionMatrixTest(Callback):
    def __init__(self, generator, label_dim):
        self.generator = generator
        self.label_dim = label_dim

    def on_test_end(self, logs=None):
        cm_calculator = ConfusionMatrix(self.generator, self.label_dim)
        cm_calculator.calculate_cm(self.model)


class ConfusionMatrixTrain(Callback):
    def __init__(self, generator, label_dim, save_model_path, report_to_nni=False):
        self.generator = generator
        self.label_dim = label_dim
        self.save_model_path = save_model_path
        self.report_to_nni = report_to_nni
        self.best_genic_f1 = 0.0

    def on_epoch_end(self, epoch, logs=None):
        start = time.time()
        cm_calculator = ConfusionMatrix(self.generator, self.label_dim)
        genic_f1 = cm_calculator.calculate_cm(self.model)
        if np.isnan(genic_f1):
            genic_f1 = 0.0
        print('\ncm calculation took: {:.2f} minutes\n'.format(int(time.time() - start) / 60))
        if self.report_to_nni:
            nni.report_intermediate_result(genic_f1)
        if genic_f1 > self.best_genic_f1:
            self.best_genic_f1 = genic_f1
            self.model.save(self.save_model_path)
            print('saved new best model with genic f1 of {} at {}'.format(self.best_genic_f1,
                                                                          self.save_model_path))

    def on_train_end(self, logs=None):
        if self.report_to_nni:
            nni.report_final_result(self.best_genic_f1)


class HelixerSequence(Sequence):
    def __init__(self, model, h5_file, mode, shuffle):
        assert mode in ['train', 'val', 'test']
        assert mode != 'test' or model.load_model_path  # assure that the mode param is correct
        self.model = model
        self.h5_file = h5_file
        self.mode = mode
        self.batch_size = self.model.batch_size
        self.float_precision = self.model.float_precision
        self.class_weights = self.model.class_weights
        self.meta_losses = self.model.meta_losses
        self.x_dset = h5_file['/data/X']
        self.y_dset = h5_file['/data/y']
        self.sw_dset = h5_file['/data/sample_weights']
        self.label_dim = self.y_dset.shape[-1]
        self._load_and_scale_meta_info()

        # set array of usable indexes, always exclude all erroneous sequences during training
        if mode == 'train':
            self.usable_idx = np.flatnonzero(np.array(h5_file['/data/err_samples']) == False)
        else:
            self.usable_idx = list(range(self.x_dset.shape[0]))
        if shuffle:
            random.shuffle(self.usable_idx)

    def _load_and_scale_meta_info(self):
        self.gc_contents = np.array(self.h5_file['/data/gc_contents'], dtype=self.float_precision)
        self.coord_lengths = np.array(self.h5_file['/data/coord_lengths'], dtype=self.float_precision)
        # scale gc content by their coord lengths
        self.gc_contents /= self.coord_lengths
        # log transform and standardize coord_lengths to [0, 1]
        # gc_contents should have a fine scale already
        self.coord_lengths = np.log(self.coord_lengths)
        self.coord_lengths = self.coord_lengths.reshape(-1, 1)
        self.coord_lengths = MinMaxScaler().fit(self.coord_lengths).transform(self.coord_lengths)
        # need to clip as values can be slightly above 1.0 (docs say otherwise..)
        self.coord_lengths = np.clip(self.coord_lengths, 0.0, 1.0).squeeze()
        assert np.all(np.logical_and(self.gc_contents >= 0.0, self.gc_contents <= 1.0))
        assert np.all(np.logical_and(self.coord_lengths >= 0.0, self.coord_lengths <= 1.0))

    def __len__(self):
        return 1
        # return int(np.ceil(len(self.usable_idx) / float(self.batch_size)))

    @abstractmethod
    def __getitem__(self, idx):
        pass


class HelixerModel(ABC):
    def __init__(self):
        tf.logging.set_verbosity(tf.logging.ERROR)
        os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

        self.parser = argparse.ArgumentParser()
        self.parser.add_argument('-d', '--data-dir', type=str, default='')
        self.parser.add_argument('-sm', '--save-model-path', type=str, default='./best_model.h5')
        # training params
        self.parser.add_argument('-e', '--epochs', type=int, default=10000)
        # self.parser.add_argument('-p', '--patience', type=int, default=10)
        self.parser.add_argument('-bs', '--batch-size', type=int, default=8)
        self.parser.add_argument('-loss', '--loss', type=str, default='')
        self.parser.add_argument('-cn', '--clip-norm', type=float, default=1.0)
        self.parser.add_argument('-lr', '--learning-rate', type=float, default=1e-3)
        self.parser.add_argument('-cw', '--class-weights', type=str, default='None')
        self.parser.add_argument('-meta-losses', '--meta-losses', action='store_true')
        # testing
        self.parser.add_argument('-lm', '--load-model-path', type=str, default='')
        self.parser.add_argument('-td', '--test-data', type=str, default='')
        self.parser.add_argument('-po', '--prediction-output-path', type=str, default='predictions.h5')
        self.parser.add_argument('-ev', '--eval', action='store_true')
        # resources
        self.parser.add_argument('-fp', '--float-precision', type=str, default='float32')
        self.parser.add_argument('-gpus', '--gpus', type=int, default=1)
        self.parser.add_argument('-cpus', '--cpus', type=int, default=8)
        self.parser.add_argument('--specific-gpu-id', type=int, default=-1)
        # misc flags
        self.parser.add_argument('-nocm', '--no-confusion-matrix', action='store_true')
        self.parser.add_argument('-plot', '--plot', action='store_true')
        self.parser.add_argument('-nni', '--nni', action='store_true')
        self.parser.add_argument('-v', '--verbose', action='store_true')

    def parse_args(self):
        args = vars(self.parser.parse_args())
        self.__dict__.update(args)
        self.class_weights = eval(self.class_weights)
        if type(self.class_weights) is list:
            self.class_weights = np.array(self.class_weights, dtype=np.float32)

        if self.nni:
            hyperopt_args = nni.get_next_parameter()
            self.__dict__.update(hyperopt_args)
            nni_save_model_path = os.path.expandvars('$NNI_OUTPUT_DIR/best_model.h5')
            nni_pred_output_path = os.path.expandvars('$NNI_OUTPUT_DIR/predictions.h5')
            self.__dict__['save_model_path'] = nni_save_model_path
            self.__dict__['prediction_output_path'] = nni_pred_output_path
            args.update(hyperopt_args)
            # for the print out
            args['save_model_path'] = nni_save_model_path
            args['prediction_output_path'] = nni_pred_output_path
        if self.verbose:
            print()
            pprint(args)

    def generate_callbacks(self):
        if not self.no_confusion_matrix:
            cm_cb = ConfusionMatrixTrain(self.gen_validation_data(), self.label_dim,
                                         self.save_model_path, report_to_nni=self.nni)
            return [cm_cb]
        else:
            return []

    def set_resources(self):
        K.set_floatx(self.float_precision)
        if self.specific_gpu_id > -1:
            os.environ['CUDA_DEVICE_ORDER'] = 'PCI_BUS_ID';
            os.environ['CUDA_VISIBLE_DEVICES'] = str(self.specific_gpu_id)

    def gen_training_data(self):
        SequenceCls = self.sequence_cls()
        return SequenceCls(model=self,
                           h5_file=self.h5_train,
                           mode='train',
                           shuffle=True)

    def gen_validation_data(self):
        SequenceCls = self.sequence_cls()
        return SequenceCls(model=self,
                           h5_file=self.h5_val,
                           mode='val',
                           shuffle=False)

    def gen_test_data(self):
        SequenceCls = self.sequence_cls()
        return SequenceCls(model=self,
                           h5_file=self.h5_test,
                           mode='test',
                           shuffle=False)

    @abstractmethod
    def sequence_cls(self):
        pass

    @abstractmethod
    def model(self):
        pass

    @abstractmethod
    def compile_model(self, model):
        pass

    def plot_model(self, model):
        from keras.utils import plot_model
        plot_model(model, to_file='model.png')
        print('Plotted to model.png')
        sys.exit()

    def open_data_files(self):
        def get_n_correct_seqs(h5_file):
            err_samples = np.array(h5_file['/data/err_samples'])
            n_correct = np.count_nonzero(err_samples == False)
            if n_correct == 0:
                print('WARNING: no fully correct sample found')
            return n_correct

        def get_n_intergenic_seqs(h5_file):
            ic_samples = np.array(h5_file['/data/fully_intergenic_samples'])
            n_fully_ig = np.count_nonzero(ic_samples == True)
            if n_fully_ig == 0:
                print('WARNING: no fully intergenic samples found')
            return n_fully_ig

        self.label_dim = 4  # if we every enable multiple possible dimension again, here is the switch
        if not self.load_model_path:
            self.h5_train = h5py.File(os.path.join(self.data_dir, 'training_data.h5'), 'r')
            self.h5_val = h5py.File(os.path.join(self.data_dir, 'validation_data.h5'), 'r')
            self.shape_train = self.h5_train['/data/X'].shape
            self.shape_val = self.h5_val['/data/X'].shape

            n_train_correct_seqs = get_n_correct_seqs(self.h5_train)
            n_val_correct_seqs = get_n_correct_seqs(self.h5_val)

            n_train_seqs = n_train_correct_seqs
            n_val_seqs = self.shape_val[0]  # always validate on all

            n_intergenic_train_seqs = get_n_intergenic_seqs(self.h5_train)
            n_intergenic_val_seqs = get_n_intergenic_seqs(self.h5_val)
        else:
            self.h5_test = h5py.File(self.test_data, 'r')
            self.shape_test = self.h5_test['/data/X'].shape

            n_test_correct_seqs = get_n_correct_seqs(self.h5_test)
            n_test_seqs_with_intergenic = self.shape_test[0]
            n_intergenic_test_seqs = get_n_intergenic_seqs(self.h5_test)

        if self.verbose:
            print('\nData config: ')
            if not self.load_model_path:
                print(dict(self.h5_train.attrs))
                print('\nTraining data shape: {}'.format(self.shape_train[:2]))
                print('Validation data shape: {}'.format(self.shape_val[:2]))
                print('\nTotal est. training sequences: {}'.format(n_train_seqs))
                print('Total est. val sequences: {}'.format(n_val_seqs))
                print('\nEst. intergenic train/val seqs: {:.2f}% / {:.2f}%'.format(
                    n_intergenic_train_seqs / n_train_seqs * 100,
                    n_intergenic_val_seqs / n_val_seqs * 100))
                print('Fully correct train/val seqs: {:.2f}% / {:.2f}%\n'.format(
                    n_train_correct_seqs / self.shape_train[0] * 100,
                    n_val_correct_seqs / self.shape_val[0] * 100))
            else:
                print(dict(self.h5_test.attrs))
                print('\nTest data shape: {}'.format(self.shape_test[:2]))
                print('\nIntergenic test seqs: {:.2f}%'.format(
                    n_intergenic_test_seqs / n_test_seqs_with_intergenic * 100))
                print('Fully correct test seqs: {:.2f}%\n'.format(
                    n_test_correct_seqs / self.shape_test[0] * 100))

    def _make_predictions(self, model):
        # loop through batches and continously expand output dataset as everything might
        # not fit in memory
        pred_out = h5py.File(self.prediction_output_path, 'w')
        test_sequence = self.gen_test_data()
        for i in range(len(test_sequence)):
            if self.verbose:
                print(i, '/', len(test_sequence), end='\r')
            predictions = model.predict_on_batch(test_sequence[i][0])
            if type(predictions) is list:
                predictions, meta_predictions = predictions
            # join last two dims when predicting one hot labels
            predictions = predictions.reshape(predictions.shape[:2] + (-1,)).astype(np.float16)
            # reshape when predicting more than one point at a time
            if predictions.shape[2] != self.label_dim:
                n_points = predictions.shape[2] // self.label_dim
                predictions = predictions.reshape(
                    predictions.shape[0],
                    predictions.shape[1] * n_points,
                    self.label_dim,
                )
                # add 0-padding if needed
                n_removed = self.shape_test[1] - predictions.shape[1]
                if n_removed > 0:
                    zero_padding = np.zeros((predictions.shape[0], n_removed, predictions.shape[2]),
                                            dtype=np.float16)
                    predictions = np.concatenate((predictions, zero_padding), axis=1)
            # create or expand dataset
            if i == 0:
                old_len = 0
                pred_out.create_dataset('/predictions',
                                        data=predictions,
                                        maxshape=(None,) + predictions.shape[1:],
                                        chunks=(1,) + predictions.shape[1:],
                                        dtype='float16',
                                        compression='lzf',
                                        shuffle=True)
            else:
                old_len = pred_out['/predictions'].shape[0]
                pred_out['/predictions'].resize(old_len + predictions.shape[0], axis=0)
            # save predictions
            pred_out['/predictions'][old_len:] = predictions

        # add model config and other attributes to predictions
        h5_model = h5py.File(self.load_model_path, 'r')
        pred_out.attrs['model_config'] = h5_model.attrs['model_config']
        pred_out.attrs['n_bases_removed'] = n_removed
        pred_out.attrs['test_data_path'] = self.test_data
        pred_out.attrs['timestamp'] = str(datetime.datetime.now())
        pred_out.close()
        h5_model.close()

    def _load_helixer_model(self):
        model = load_model(self.load_model_path, custom_objects = {
            'LayerNormalization': LayerNormalization,
            'acc_g_oh': acc_g_oh,
            'acc_ig_oh': acc_ig_oh,
        })
        return model

    def run(self):
        self.set_resources()
        self.open_data_files()
        # we either train or predict
        if not self.load_model_path:
            model = self.model()
            if self.gpus >= 2:
                model = multi_gpu_model(model, gpus=self.gpus)

            if self.verbose:
                print(model.summary())
            else:
                print('Total params: {:,}'.format(model.count_params()))

            if self.plot:
                self.plot_model(model)

            self.optimizer = optimizers.Adam(lr=self.learning_rate, clipnorm=self.clip_norm)
            self.compile_model(model)

            model.fit_generator(generator=self.gen_training_data(),
                                epochs=self.epochs,
                                workers=0,  # run in main thread
                                # workers=1,
                                validation_data=self.gen_validation_data(),
                                callbacks=self.generate_callbacks(),
                                verbose=True)

            # set all model instance variables so predictions are made on the validation set
            self.h5_test = self.h5_val
            self.shape_test = self.shape_val
            self.load_model_path = self.save_model_path
            self.test_data = os.path.join(self.data_dir, 'validation_data.h5')
            self.class_weights = None
            model = self._load_helixer_model()
            self._make_predictions(model)
            print('Predictions made with {} on {} and saved to {}'.format(self.load_model_path,
                                                                          self.test_data,
                                                                          self.prediction_output_path))

            self.h5_train.close()
            self.h5_val.close()

        # predict instead of train
        else:
            assert self.test_data.endswith('.h5'), 'Need a h5 test data file when loading a model'
            assert self.load_model_path.endswith('.h5'), 'Need a h5 model file'
            model = self._load_helixer_model()

            if self.eval:
                if self.no_confusion_matrix:
                    callback = []
                else:
                    callback = [ConfusionMatrixTest(self.gen_test_data(), self.label_dim)]
                metrics = model.evaluate_generator(generator=self.gen_test_data(),
                                                   callbacks=callback,
                                                   verbose=True)
                metrics_names = model.metrics_names
                print({z[0]: z[1] for z in zip(metrics_names, metrics)})
            else:
                if os.path.isfile(self.prediction_output_path):
                    print('{} already existing and will be overridden.'.format(
                        self.prediction_output_path
                    ))
                if self.exclude_errors:
                    print('WARNING: --exclude-errors used in test mode')
                self._make_predictions(model)

            self.h5_test.close()
