"""
Copyright (c) 2018-2020 Intel Corporation

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from pathlib import Path
from functools import singledispatch
from collections import OrderedDict, namedtuple
import re
import wave

import cv2
from PIL import Image
import numpy as np
from numpy.lib.npyio import NpzFile

from ..utils import get_path, read_json, read_pickle, contains_all, UnsupportedPackage, get_parameter_value_from_config
from ..dependency import ClassProvider
from ..config import BaseField, StringField, ConfigValidator, ConfigError, DictField, ListField, BoolField, NumberField

try:
    import nibabel as nib
except ImportError as import_error:
    nib = UnsupportedPackage("nibabel", import_error.msg)

try:
    import pydicom
except ImportError as import_error:
    pydicom = UnsupportedPackage("pydicom", import_error.msg)

try:
    import skimage.io as sk
except ImportError as import_error:
    sk = UnsupportedPackage('skimage.io', import_error.msg)

REQUIRES_ANNOTATIONS = ['annotation_features_extractor', ]


class DataRepresentation:
    def __init__(self, data, meta=None, identifier=''):
        self.identifier = identifier
        self.data = data
        self.metadata = meta or {}
        if np.isscalar(data):
            self.metadata['image_size'] = 1
        elif isinstance(data, list) and np.isscalar(data[0]):
            self.metadata['image_size'] = len(data)
        elif isinstance(data, dict):
            self.metadata['image_size'] = data.values().next().shape
        else:
            self.metadata['image_size'] = data.shape if not isinstance(data, list) else np.shape(data[0])


ClipIdentifier = namedtuple('ClipIdentifier', ['video', 'clip_id', 'frames'])
MultiFramesInputIdentifier = namedtuple('MultiFramesInputIdentifier', ['input_id', 'frames'])
ImagePairIdentifier = namedtuple('ImagePairIdentifier', ['first', 'second'])


def create_reader(config):
    return BaseReader.provide(config.get('type', 'opencv_imread'), config.get('data_source'), config=config)


class DataReaderField(BaseField):
    def validate(self, entry_, field_uri=None):
        super().validate(entry_, field_uri)

        if entry_ is None:
            return

        field_uri = field_uri or self.field_uri
        if isinstance(entry_, str):
            StringField(choices=BaseReader.providers).validate(entry_, 'reader')
        elif isinstance(entry_, dict):
            class DictReaderValidator(ConfigValidator):
                type = StringField(choices=BaseReader.providers)

            dict_reader_validator = DictReaderValidator(
                'reader', on_extra_argument=DictReaderValidator.IGNORE_ON_EXTRA_ARGUMENT
            )
            dict_reader_validator.validate(entry_)
        else:
            self.raise_error(entry_, field_uri, 'reader must be either string or dictionary')


class BaseReader(ClassProvider):
    __provider_type__ = 'reader'

    def __init__(self, data_source, config=None, **kwargs):
        self.config = config or {}
        self.data_source = data_source
        self.read_dispatcher = singledispatch(self.read)
        self.read_dispatcher.register(list, self._read_list)
        self.read_dispatcher.register(ClipIdentifier, self._read_clip)
        self.read_dispatcher.register(MultiFramesInputIdentifier, self._read_frames_multi_input)
        self.read_dispatcher.register(ImagePairIdentifier, self._read_pair)
        self.multi_infer = False

        self.validate_config()
        self.configure()

    def __call__(self, identifier):
        return self.read_item(identifier)

    @classmethod
    def parameters(cls):
        return {
            'type': StringField(
                default=cls.__provider__ if hasattr(cls, '__provider__') else None, description='Reader type.'
            ),
            'multi_infer': BoolField(
                default=False, optional=True, description='Allows multi infer.'
            ),
        }

    def get_value_from_config(self, key):
        return get_parameter_value_from_config(self.config, self.parameters(), key)

    def configure(self):
        if not self.data_source:
            raise ConfigError('data_source parameter is required to create "{}" '
                              'data reader and read data'.format(self.__provider__))
        self.data_source = get_path(self.data_source, is_directory=True)
        self.multi_infer = self.get_value_from_config('multi_infer')

    def validate_config(self, **kwargs):
        if 'on_extra_argument' not in kwargs:
            kwargs['on_extra_argument'] = ConfigValidator.IGNORE_ON_EXTRA_ARGUMENT
        ConfigValidator(self.__class__.__name__, fields=self.parameters(), **kwargs).validate(self.config)

    def read(self, data_id):
        raise NotImplementedError

    def _read_list(self, data_id):
        return [self.read(identifier) for identifier in data_id]

    def _read_clip(self, data_id):
        video = Path(data_id.video)
        frames_identifiers = [video / frame for frame in data_id.frames]
        return self.read_dispatcher(frames_identifiers)

    def _read_frames_multi_input(self, data_id):
        return self.read_dispatcher(data_id.frames)

    def read_item(self, data_id):
        data_rep = DataRepresentation(self.read_dispatcher(data_id), identifier=data_id)
        if self.multi_infer:
            data_rep.metadata['multi_infer'] = True
        return data_rep

    def _read_pair(self, data_id):
        data = self.read_dispatcher([data_id.first, data_id.second])
        return data

    @property
    def name(self):
        return self.__provider__

    def reset(self):
        pass


class ReaderCombiner(BaseReader):
    __provider__ = 'combine_reader'

    @classmethod
    def parameters(cls):
        parameters = super().parameters()
        parameters.update({
            'scheme': DictField(value_type=DataReaderField(), key_type=StringField(), allow_empty=False,
                                description='Dictionary for describing reading scheme which depends on file names.')
        })
        return parameters

    def configure(self):
        scheme = self.get_value_from_config('scheme')
        reading_scheme = OrderedDict()
        for pattern, reader_config in scheme.items():
            reader_type = reader_config['type'] if isinstance(reader_config, dict) else reader_config
            reader_configuration = reader_config if isinstance(reader_config, dict) else None
            reader = BaseReader.provide(reader_type, self.data_source, reader_configuration)
            pattern = re.compile(pattern)
            reading_scheme[pattern] = reader

        self.reading_scheme = reading_scheme
        self.multi_infer = self.get_value_from_config('multi_infer')

    def read(self, data_id):
        for pattern, reader in self.reading_scheme.items():
            if pattern.match(str(data_id)):
                return reader.read(data_id)

        raise ConfigError('suitable data reader for {} not found'.format(data_id))


OPENCV_IMREAD_FLAGS = {
    'color': cv2.IMREAD_COLOR,
    'gray': cv2.IMREAD_GRAYSCALE,
    'unchanged': cv2.IMREAD_UNCHANGED
}


class OpenCVImageReader(BaseReader):
    __provider__ = 'opencv_imread'

    @classmethod
    def parameters(cls):
        parameters = super().parameters()
        parameters.update({
            'reading_flag': StringField(optional=True, choices=OPENCV_IMREAD_FLAGS, default='color',
                                        description='Flag which specifies the way image should be read.')
        })
        return parameters

    def configure(self):
        super().configure()
        self.flag = OPENCV_IMREAD_FLAGS[self.get_value_from_config('reading_flag')]

    def read(self, data_id):
        return cv2.imread(str(get_path(self.data_source / data_id)), self.flag)


class PillowImageReader(BaseReader):
    __provider__ = 'pillow_imread'

    def __init__(self, data_source, config=None, **kwargs):
        super().__init__(data_source, config)
        self.convert_to_rgb = True

    def read(self, data_id):
        with open(str(self.data_source / data_id), 'rb') as f:
            img = Image.open(f)

            return np.array(img.convert('RGB') if self.convert_to_rgb else img)


class ScipyImageReader(BaseReader):
    __provider__ = 'scipy_imread'

    def read(self, data_id):
        # reimplementation scipy.misc.imread
        image = Image.open(str(get_path(self.data_source / data_id)))
        if image.mode == 'P':
            image = image.convert('RGBA') if 'transparency' in image.info else image.convert('RGB')

        return np.array(image)


class OpenCVFrameReader(BaseReader):
    __provider__ = 'opencv_capture'

    def __init__(self, data_source, config=None, **kwargs):
        super().__init__(data_source, config)
        self.current = -1

    def read(self, data_id):
        if data_id < 0:
            raise IndexError('frame with {} index can not be grabbed, non-negative index is expected')
        if data_id < self.current:
            self.videocap.set(cv2.CAP_PROP_POS_FRAMES, data_id)
            self.current = data_id - 1

        return self._read_sequence(data_id)

    def _read_sequence(self, data_id):
        frame = None
        while self.current != data_id:
            success, frame = self.videocap.read()
            self.current += 1
            if not success:
                raise EOFError('frame with {} index does not exist in {}'.format(self.current, self.data_source))

        return frame

    def configure(self):
        if not self.data_source:
            raise ConfigError('data_source parameter is required to create "{}" '
                              'data reader and read data'.format(self.__provider__))
        self.data_source = get_path(self.data_source)
        self.videocap = cv2.VideoCapture(str(self.data_source))
        self.multi_infer = self.get_value_from_config('multi_infer')

    def reset(self):
        self.current = -1
        self.videocap.set(cv2.CAP_PROP_POS_FRAMES, 0)


class JSONReader(BaseReader):
    __provider__ = 'json_reader'

    @classmethod
    def parameters(cls):
        parameters = super().parameters()
        parameters.update({
            'key': StringField(optional=True, case_sensitive=True,
                               description='Key for reading from json dictionary.')
        })
        return parameters

    def configure(self):
        self.key = self.get_value_from_config('key')
        self.multi_infer = self.get_value_from_config('multi_infer')
        if not self.data_source:
            raise ConfigError('data_source parameter is required to create "{}" '
                              'data reader and read data'.format(self.__provider__))

    def read(self, data_id):
        data = read_json(str(self.data_source / data_id))
        if self.key:
            data = data.get(self.key)

            if not data:
                raise ConfigError('{} does not contain {}'.format(data_id, self.key))

        return np.array(data).astype(np.float32)


class NCFDataReader(BaseReader):
    __provider__ = 'ncf_data_reader'

    def configure(self):
        pass

    def read(self, data_id):
        if not isinstance(data_id, str):
            raise IndexError('Data identifier must be a string')

        return float(data_id.split(":")[1])


class NiftiImageReader(BaseReader):
    __provider__ = 'nifti_reader'

    @classmethod
    def parameters(cls):
        parameters = super().parameters()
        parameters.update({
            'channels_first': BoolField(optional=True, default=False,
                                        description='Allows read files and transpose in order where channels first.')
        })
        return parameters

    def configure(self):
        if isinstance(nib, UnsupportedPackage):
            nib.raise_error(self.__provider__)
        self.channels_first = self.get_value_from_config('channels_first')
        self.multi_infer = self.get_value_from_config('multi_infer')
        if not self.data_source:
            raise ConfigError('data_source parameter is required to create "{}" '
                              'data reader and read data'.format(self.__provider__))

    def read(self, data_id):
        nib_image = nib.load(str(get_path(self.data_source / data_id)))
        image = np.array(nib_image.dataobj)
        if len(image.shape) != 4:  # Make sure 4D
            image = np.expand_dims(image, -1)
        image = np.transpose(image, (3, 0, 1, 2) if self.channels_first else (2, 1, 0, 3))

        return image


class NumPyReader(BaseReader):
    __provider__ = 'numpy_reader'

    @classmethod
    def parameters(cls):
        parameters = super().parameters()
        parameters.update({
            'keys': StringField(optional=True, default="", description='Comma-separated model input names.'),
            'separator': StringField(optional=True,
                                     description='Separator symbol between input identifier and file identifier.'),
            'id_sep': StringField(
                optional=True, default="_",
                description='Separator symbol between input name and record number in input identifier.'
            ),
            'block': BoolField(optional=True, default=False, description='Allows block mode.'),
            'batch': NumberField(optional=True, default=1, description='Batch size')
        })
        return parameters

    def configure(self):
        self.is_text = self.config.get('text_file', False)
        self.multi_infer = self.get_value_from_config('multi_infer')
        self.keys = self.get_value_from_config('keys')
        self.keys = [t.strip() for t in self.keys.split(',')] if len(self.keys) > 0 else []
        self.separator = self.get_value_from_config('separator')
        self.id_sep = self.get_value_from_config('id_sep')
        self.block = self.get_value_from_config('block')
        self.batch = int(self.get_value_from_config('batch'))

        if self.separator and self.is_text:
            raise ConfigError('text file reading with numpy does')
        if not self.data_source:
            raise ConfigError('data_source parameter is required to create "{}" '
                              'data reader and read data'.format(self.__provider__))
        self.keyRegex = {k: re.compile(k + self.id_sep) for k in self.keys}
        self.valRegex = re.compile(r"([^0-9]+)([0-9]+)")

    def read(self, data_id):
        field_id = None
        if self.separator:
            field_id, data_id = str(data_id).split(self.separator)

        data = np.load(str(self.data_source / data_id))

        if not isinstance(data, NpzFile):
            return data

        if field_id is not None:
            key = [k for k, v in self.keyRegex.items() if v.match(field_id)]
            if len(key) > 0:
                if self.block:
                    res = data[key[0]]
                else:
                    recno = field_id.split('_')[-1]
                    recno = int(recno)
                    start = Path(data_id).name.split('.')[0]
                    start = int(start)
                    res = data[key[0]][recno - start, :]
                return res

        key = next(iter(data.keys()))
        return data[key]


class NumpyTXTReader(BaseReader):
    __provider__ = 'numpy_txt_reader'

    def read(self, data_id):
        return np.loadtxt(str(self.data_source / data_id))


class NumpyDictReader(BaseReader):
    __provider__ = 'numpy_dict_reader'

    def read(self, data_id):
        return np.load(str(self.data_source / data_id), allow_pickle=True)[()]

    def read_item(self, data_id):
        dict_data = self.read_dispatcher(data_id)
        identifier = []
        data = []
        for key, value in dict_data.items():
            identifier.append('{}.{}'.format(data_id, key))
            data.append(value)
        if len(data) == 1:
            return DataRepresentation(data[0], identifier=data_id)
        return DataRepresentation(data, identifier=identifier)


class TensorflowImageReader(BaseReader):
    __provider__ = 'tf_imread'

    def __init__(self, data_source, config=None, **kwargs):
        super().__init__(data_source, config)
        try:
            import tensorflow as tf # pylint: disable=C0415
        except ImportError as import_error:
            raise ImportError(
                'tf backend for image reading requires TensorFlow. '
                'Please install it before usage. {}'.format(import_error.msg)
            )
        tf.enable_eager_execution()

        def read_func(path):
            img_raw = tf.read_file(str(path))
            img_tensor = tf.image.decode_image(img_raw, channels=3)
            return img_tensor.numpy()

        self.read_realisation = read_func

    def read(self, data_id):
        return self.read_realisation(self.data_source / data_id)


class AnnotationFeaturesReader(BaseReader):
    __provider__ = 'annotation_features_extractor'

    @classmethod
    def parameters(cls):
        parameters = super().parameters()
        parameters.update({
            'features': ListField(allow_empty=False, value_type=str, description='List of features.')
        })
        return parameters

    def configure(self):
        self.feature_list = self.get_value_from_config('features')
        if not contains_all(self.data_source[0].__dict__, self.feature_list):
            raise ConfigError(
                'annotation_class prototype does not contain provided features {}'.format(', '.join(self.feature_list))
            )
        self.single = len(self.feature_list) == 1
        self.counter = 0
        self.subset = range(len(self.data_source))
        self.multi_infer = self.get_value_from_config('multi_infer')

    def read(self, data_id):
        relevant_annotation = self.data_source[self.subset[self.counter]]
        self.counter += 1
        features = [getattr(relevant_annotation, feature) for feature in self.feature_list]
        if self.single:
            return features[0]
        return features

    def _read_list(self, data_id):
        return self.read(data_id)

    def reset(self):
        self.subset = range(len(self.data_source))
        self.counter = 0


class WavReader(BaseReader):
    __provider__ = 'wav_reader'

    _samplewidth_types = {
        1: np.uint8,
        2: np.int16
    }

    def read(self, data_id):
        with wave.open(str(self.data_source / data_id), "rb") as wav:
            sample_rate = wav.getframerate()
            sample_width = wav.getsampwidth()
            nframes = wav.getnframes()
            data = wav.readframes(nframes)
            if self._samplewidth_types.get(sample_width):
                data = np.frombuffer(data, dtype=self._samplewidth_types[sample_width])
            else:
                raise RuntimeError("Reader {} couldn't process file {}: unsupported sample width {}"
                                   "(reader only supports {})"
                                   .format(self.__provider__, self.data_source / data_id,
                                           sample_width, [*self._samplewidth_types.keys()]))
            data = data.reshape(-1, wav.getnchannels()).T

        return data, {'sample_rate': sample_rate}

    def read_item(self, data_id):
        return DataRepresentation(*self.read_dispatcher(data_id), identifier=data_id)


class DicomReader(BaseReader):
    __provider__ = 'dicom_reader'

    def __init__(self, data_source, config=None, **kwargs):
        super().__init__(data_source, config)
        if isinstance(pydicom, UnsupportedPackage):
            pydicom.raise_error(self.__provider__)

    def read(self, data_id):
        dataset = pydicom.dcmread(str(self.data_source / data_id))
        return dataset.pixel_array


class PickleReader(BaseReader):
    __provider__ = 'pickle_reader'

    def read(self, data_id):
        data = read_pickle(self.data_source / data_id)
        if isinstance(data, list) and len(data) == 2 and isinstance(data[1], dict):
            return data

        return data, {}

    def read_item(self, data_id):
        return DataRepresentation(*self.read_dispatcher(data_id), identifier=data_id)


class SkimageReader(BaseReader):
    __provider__ = 'skimage_imread'

    def __init__(self, data_source, config=None, **kwargs):
        super().__init__(data_source, config)
        if isinstance(sk, UnsupportedPackage):
            sk.raise_error(self.__provider__)

    def read(self, data_id):
        return sk.imread(str(self.data_source / data_id))
