# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

r"""Script to download the Imagenet dataset and upload to gcs.

To run the script setup a virtualenv with the following libraries installed.
- `gcloud`: Follow the instructions on
  [cloud SDK docs](https://cloud.google.com/sdk/downloads) followed by
  installing the python api using `pip install gcloud`.
- `google-cloud-storage`: Install with `pip install google-cloud-storage`
- `tensorflow`: Install with `pip install tensorflow`

Once you have all the above libraries setup, you should register on the
[Imagenet website](http://image-net.org/download-images) to get your
username and access_key.

Make sure you have around 300GB of disc space available on the machine where
you're running this script. You can run the script using the following command.
```
python imagenet_to_gcs.py \
  --project="TEST_PROJECT" \
  --gcs_output_path="gs://TEST_BUCKET/IMAGENET_DIR" \
  --local_scratch_dir="./imagenet" \
  --imagenet_username=FILL_ME_IN \
  --imagenet_access_key=FILL_ME_IN \
```

Optionally if the raw data has already been downloaded you can provide a direct
`raw_data_directory` path. If raw data directory is provided it should be in
the format:
- Training images: train/n03062245/n03062245_4620.JPEG
- Validation Images: validation/ILSVRC2012_val_00000001.JPEG
- Validation Labels: synset_labels.txt
"""

import math
import os
import sys
import random
import tarfile
import urllib
import tqdm
import time

from absl import app
from absl import flags
import tensorflow.compat.v1 as tf

from google.cloud import storage

from multiprocessing import Pool

flags.DEFINE_string(
    'out', None, 'tfrecord output path.')
flags.DEFINE_string(
    'name', None, 'tfrecord prefix. E.g. --name danbooru2019')
flags.DEFINE_list(
    'glob', None, 'Comma-separated glob patterns. E.g. --glob "data/images/*/*.jpg"')
flags.DEFINE_string(
    'files', None, 'Name of a file that specifies the images in the dataset, one per line. E.g. --files my_list_of_images.txt')
flags.DEFINE_integer(
    'shards', 2048, 'Number of tfrecord files to generate')
flags.DEFINE_integer(
    'nprocs', 8, 'Number of processes to work in parallel')
flags.DEFINE_boolean(
    'directory_labels', False, 'Use the directory name of each file as a label')

FLAGS = flags.FLAGS

def _check_or_create_dir(directory):
  """Check if directory exists otherwise create it."""
  if not tf.gfile.Exists(directory):
    tf.gfile.MakeDirs(directory)

def _int64_feature(value):
  """Wrapper for inserting int64 features into Example proto."""
  if not isinstance(value, list):
    value = [value]
  return tf.train.Feature(int64_list=tf.train.Int64List(value=value))


def _as_bytes(x):
  if isinstance(x, str):
    return x.encode('utf8')
  return x

def _bytes_feature(value):
  """Wrapper for inserting bytes features into Example proto."""
  return tf.train.Feature(bytes_list=tf.train.BytesList(value=[_as_bytes(value)]))


def _convert_to_example(filename, image_buffer, label, synset, height, width):
  """Build an Example proto for an example.

  Args:
    filename: string, path to an image file, e.g., '/path/to/example.JPG'
    image_buffer: string, JPEG encoding of RGB image
    label: integer, identifier for the ground truth for the network
    synset: string, unique WordNet ID specifying the label, e.g., 'n02323233'
    height: integer, image height in pixels
    width: integer, image width in pixels
  Returns:
    Example proto
  """
  colorspace = 'RGB'
  channels = 3
  image_format = 'JPEG'

  example = tf.train.Example(features=tf.train.Features(feature={
      'image/height': _int64_feature(height),
      'image/width': _int64_feature(width),
      'image/colorspace': _bytes_feature(colorspace),
      'image/channels': _int64_feature(channels),
      'image/class/label': _int64_feature(label),
      'image/class/synset': _bytes_feature(synset),
      'image/format': _bytes_feature(image_format),
      'image/filename': _bytes_feature(os.path.basename(filename)),
      'image/encoded': _bytes_feature(image_buffer)}))
  return example


def _is_png(filename):
  """Determine if a file contains a PNG format image.

  Args:
    filename: string, path of the image file.

  Returns:
    boolean indicating if the image is a PNG.
  """
  return filename.lower().endswith('.png')


def _is_cmyk(filename):
  """Determine if file contains a CMYK JPEG format image.

  Args:
    filename: string, path of the image file.

  Returns:
    boolean indicating if the image is a JPEG encoded with CMYK color space.
  """
  # File list from:
  # https://github.com/cytsai/ilsvrc-cmyk-image-list
  blacklist = set(['n01739381_1309.JPEG', 'n02077923_14822.JPEG',
                   'n02447366_23489.JPEG', 'n02492035_15739.JPEG',
                   'n02747177_10752.JPEG', 'n03018349_4028.JPEG',
                   'n03062245_4620.JPEG', 'n03347037_9675.JPEG',
                   'n03467068_12171.JPEG', 'n03529860_11437.JPEG',
                   'n03544143_17228.JPEG', 'n03633091_5218.JPEG',
                   'n03710637_5125.JPEG', 'n03961711_5286.JPEG',
                   'n04033995_2932.JPEG', 'n04258138_17003.JPEG',
                   'n04264628_27969.JPEG', 'n04336792_7448.JPEG',
                   'n04371774_5854.JPEG', 'n04596742_4225.JPEG',
                   'n07583066_647.JPEG', 'n13037406_4650.JPEG'])
  return os.path.basename(filename) in blacklist


class ImageCoder(object):
  """Helper class that provides TensorFlow image coding utilities."""

  def __init__(self):
    # Create a single Session to run all image coding calls.
    self._sess = tf.Session()

    # Initializes function that converts PNG to JPEG data.
    self._png_data = tf.placeholder(dtype=tf.string)
    image = tf.io.decode_image(self._png_data, channels=3)
    self._to_jpeg = tf.image.encode_jpeg(image, format='rgb', quality=100)

    # Initializes function that converts CMYK JPEG data to RGB JPEG data.
    self._cmyk_data = tf.placeholder(dtype=tf.string)
    image = tf.image.decode_jpeg(self._cmyk_data, channels=0)
    self._cmyk_to_rgb = tf.image.encode_jpeg(image, format='rgb', quality=100)

    # Initializes function that decodes RGB JPEG data.
    self._decode_jpeg_data = tf.placeholder(dtype=tf.string)
    self._decode_jpeg = tf.image.decode_image(self._decode_jpeg_data, channels=3)
    self._is_jpeg = tf.io.is_jpeg(self._decode_jpeg_data)

  def is_jpeg(self, image_data):
    return self._sess.run(self._is_jpeg,
                          feed_dict={self._decode_jpeg_data: image_data})

  def to_jpeg(self, image_data):
    return self._sess.run(self._to_jpeg,
                          feed_dict={self._png_data: image_data})

  def cmyk_to_rgb(self, image_data):
    return self._sess.run(self._cmyk_to_rgb,
                          feed_dict={self._cmyk_data: image_data})

  def decode_jpeg(self, image_data):
    image = self._sess.run(self._decode_jpeg,
                           feed_dict={self._decode_jpeg_data: image_data})
    assert len(image.shape) == 3
    assert image.shape[2] == 3
    return image

g_coder = None

def get_coder():
  global g_coder
  if g_coder is None:
    g_coder = ImageCoder()
  return g_coder

def _initializer(lock):
  tqdm.tqdm.set_lock(lock)
  get_coder()

def _process_image(filename, coder):
  """Process a single image file.

  Args:
    filename: string, path to an image file e.g., '/path/to/example.JPG'.
    coder: instance of ImageCoder to provide TensorFlow image coding utils.
  Returns:
    image_buffer: string, JPEG encoding of RGB image.
    height: integer, image height in pixels.
    width: integer, image width in pixels.
  """
  # Read the image file.
  with tf.gfile.GFile(filename, 'rb') as f:
    image_data = f.read()

  # Decode the RGB JPEG.
  image = coder.decode_jpeg(image_data)

  # Check that image converted to RGB
  assert len(image.shape) == 3
  height = image.shape[0]
  width = image.shape[1]
  assert image.shape[2] == 3

  return image_data, height, width

def _process_image_files_batch(output_file, filenames, labels=None, pbar=None, coder=None):
  """Processes and saves list of images as TFRecords.

  Args:
    coder: instance of ImageCoder to provide TensorFlow image coding utils.
    output_file: string, unique identifier specifying the data set
    filenames: list of strings; each string is a path to an image file
    labels: map of string to integer; id for all synset labels
  """
  writer = None

  if coder is None:
    coder = get_coder()

  if labels is None:
    labels = [0 for _ in range(len(filenames))]

  for label, filename in zip(labels, filenames):
    try:
      image_buffer, height, width = _process_image(filename, coder)
      synset = b''
      example = _convert_to_example(filename, image_buffer, label,
                                    synset, height, width)
      if writer is None:
        writer = tf.python_io.TFRecordWriter(output_file)#+'.tmp')
      writer.write(example.SerializeToString())
    except Exception as e:
      if isinstance(e, KeyboardInterrupt):
        break
      import traceback
      traceback.print_exc()
      sys.stderr.write('Failed: %s\n' % filename)
      sys.stderr.flush()
    finally:
      if pbar is not None:
        pbar.update(1)

  if writer is not None:
    writer.close()
    #os.rename(output_file+'.tmp', output_file)

  return writer is not None


def tuples(l, n=2):
  r = []
  for i in range(0, len(l), n):
    r.append(l[i:i+n])
  return r

def shards(l, n):
  r = [[] for _ in range(n)]
  for v in tuples(l, n):
    for i, x in enumerate(v):
      r[i].append(x)
  return r

def _process_shards(filenames, labels, output_directory, prefix, shards, num_shards, worker_count, worker_index):
  files = []
  chunksize = int(math.ceil(len(filenames) / num_shards))

  with tqdm.tqdm(total=len(filenames) // worker_count, position=worker_index, dynamic_ncols=True, mininterval=1.0) as pbar:
    for shard in shards:
      chunk_files = filenames[shard * chunksize : (shard + 1) * chunksize]
      chunk_labels = labels[shard * chunksize : (shard + 1) * chunksize] if labels is not None else None
      output_file = os.path.join(
          output_directory, '%s-%.5d-of-%.5d' % (prefix, shard, num_shards))
      pbar.set_description(output_file)
      if _process_image_files_batch(output_file, chunk_files, labels=chunk_labels, pbar=pbar):
        files.append(output_file)
  return files

def _process_dataset(filenames, output_directory, prefix, num_shards, labels=None):
  """Processes and saves list of images as TFRecords.

  Args:
    filenames: list of strings; each string is a path to an image file
    labels: map of string to integer; id for all synset labels
    output_directory: path where output files should be created
    prefix: string; prefix for each file
    num_shards: number of chucks to split the filenames into

  Returns:
    files: list of tf-record filepaths created from processing the dataset.
  """
  _check_or_create_dir(output_directory)

  with open(os.path.join(output_directory, '%s-filenames.txt' % prefix), 'w') as f:
    for filename in filenames:
      f.write(filename + '\n')

  if labels is not None:
    with open(os.path.join(output_directory, '%s-labels.txt' % prefix), 'w') as f:
      for label in labels:
        f.write('{}\n'.format(label))

  with Pool(processes=FLAGS.nprocs, initializer=_initializer, initargs=(tqdm.tqdm.get_lock(),)) as pool:
    time.sleep(2.0) # give tensorflow logging some time to quit spamming the console
    chunks = shards(list(range(num_shards)), FLAGS.nprocs)
    pool.starmap(_process_shards, [(filenames, labels, output_directory, prefix, chunk, num_shards, FLAGS.nprocs, i) for i, chunk in enumerate(chunks)])

def convert_to_tf_records():
  """Convert the Imagenet dataset into TF-Record dumps."""

  # Shuffle training records to ensure we are distributing classes
  # across the batches.
  tf.logging.info('training records to ensure we are distributing classes across the batches.')
  random.seed(0)
  def make_shuffle_idx(n):
    order = [_ for _ in range(n)]
    random.shuffle(order)
    return order

  # Glob all the training files
  tf.logging.info('Glob all the training files.')
  training_files = []
  for pattern in (FLAGS.glob if FLAGS.glob is not None else []):
    training_files.extend(tf.gfile.Glob(pattern))
  if FLAGS.files is not None:
    with open(FLAGS.files) as f:
      training_files.extend(f.read().splitlines())
  assert len(training_files) > 0

  training_shuffle_idx = make_shuffle_idx(len(training_files))
  training_files = [training_files[i] for i in training_shuffle_idx]
  training_labels = None

  if FLAGS.directory_labels:
    labeldirs = dict([(i, x) for x, i in enumerate(sorted(set([os.path.dirname(x) for x in training_files])))])
    training_labels = [labeldirs[os.path.dirname(x)] for x in training_files]

  # Create training data
  tf.logging.info('Processing the training data.')
  training_records = _process_dataset(training_files, FLAGS.out, FLAGS.name, FLAGS.shards, labels=training_labels)

  return training_records

def main(argv):  # pylint: disable=unused-argument
  tf.logging.set_verbosity(tf.logging.INFO)

  if FLAGS.name is None:
    raise ValueError('--name must be provided. e.g. --name danbooru2019-s')

  if FLAGS.out is None:
    raise ValueError('--out must be provided. e.g. --out out/')

  if FLAGS.glob is None and FLAGS.files is None:
    raise ValueError('Must specify --files images.txt or at least one --glob pattern. Eg. --glob "data/*/*.jpg"')

  # Convert the raw data into tf-records
  training_records = convert_to_tf_records()


if __name__ == '__main__':
  app.run(main)

