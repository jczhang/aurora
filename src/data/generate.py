"""Utilities for generating datasets from hooktheory theorytabs and audio.

generate_specs takes in theorytabs and audio and filters the tabs for
well-formedness and suitability, including deduplication and whether audio
source exists. The tabs are converted to an intermediate format called a spec
used for learning.
<theorytabs> should be a directory of xml files containing raw theorytabs.
<audio> should be a directory of audio files of the form
[YouTube id].[extension] or [YouTube id],[begin time],[end time].[extension],
or be a text file with the YouTube filenames on each line.
The clean tabs are written to the <output> directory as individual json files.

clip_audio takes in specs and raw audio files with the filename convention
[YouTube id].extension. The audio files are clipped according to the cleaned
tabs.
<specs> should be a directory of json files containing specs.
<raw_audio> should be a directory of audio files of the form
[YouTube id].[extension].
The clipped audio is written to the <output> directory as individual audio
files of the form [YouTube id],[begin time],[end time].[extension].
This requires installation of ffmpeg with libvpx and libvorbis.

generate_dataset takes in cleaned tabs (generated by generate_specs) and
clipped audio files (generated by clip_audio). The data is serialized into
a single TensorFlow TFRecords <output> file.

Usage:
  generate.py generate_specs <theorytabs> <audio> <output>
  generate.py clip_audio <specs> <raw_audio> <output>
  generate.py generate_dataset <specs> <clipped_audio> <output>
"""

from __future__ import print_function

import collections
import glob
import json
import math
import os
import subprocess
import sys

import docopt
import librosa
import tensorflow as tf

import theorytab

CLIP_NAME_PATTERN = "{},{:.2f},{:.2f}"
CLIP_SPEC_PATTERN = "{},{:.2f},{:.2f}.json"


def generate_spec(clip):
  return clip


def generate_specs(theorytabs, audio, output):
  # Get the audio filenames without extensions. In generating specs, we hold
  # the available audio fixed.
  if os.path.isdir(audio):
    audio_paths = next(os.walk(audio))[2]
    audio_names = [os.path.splitext(os.path.split(path)[1])[0]
                   for path in audio_paths]
  elif os.path.isfile(audio):
    with open(audio) as f:
      audio_names = [os.path.splitext(line)[0] for line in f.readlines()]
  theorytab_paths = next(os.walk(theorytabs))[2]

  for filename in theorytab_paths:
    tab = theorytab.Theorytab(os.path.join(theorytabs, filename))
    for clip in tab.clips():
      youtube_id = clip['audio_source']['youtube_id']
      start_time = clip['audio_source']['start_time']
      end_time = clip['audio_source']['end_time']
      clip_filename = CLIP_NAME_PATTERN.format(
          youtube_id, start_time, end_time)
      if youtube_id in audio_names or clip_filename in audio_names:
        print(clip_filename)
        spec = generate_spec(clip)
        spec_filename = CLIP_SPEC_PATTERN.format(
            youtube_id, start_time, end_time)
        spec_path = os.path.join(output, spec_filename)

        os.makedirs(os.path.dirname(spec_path), exist_ok=True)
        with open(spec_path, 'w') as f:
          json.dump(spec, f, ensure_ascii=False)


def clip_audio(specs, raw_audio, output):
  # Load the spec data. In clipping audio, we hold the specs fixed.
  spec_filenames = next(os.walk(specs))[2]
  if len(spec_filenames) == 0:
    print("No specs found.")
    return
  for spec_filename in spec_filenames:
    with open(os.path.join(specs, spec_filename)) as f:
      spec = json.load(f)
    youtube_id = spec['audio_source']['youtube_id']
    start_time = spec['audio_source']['start_time']
    end_time = spec['audio_source']['end_time']

    raw_audio_filenames = glob.glob(os.path.join(raw_audio, youtube_id + '.*'))
    if len(raw_audio_filenames) == 0:
      # No audio file found, skip.
      continue
    raw_audio_filename = raw_audio_filenames[0]
    raw_audio_extension = os.path.splitext(raw_audio_filename)[1]
    clip_filename = os.path.join(
        output, CLIP_NAME_PATTERN.format(youtube_id, start_time, end_time) +
        raw_audio_extension)

    # Call ffmpeg to output the trimmed clip.
    os.makedirs(os.path.dirname(clip_filename), exist_ok=True)
    call1 = ['ffmpeg', '-loglevel', 'error', '-n',
             '-ss', str(start_time), '-t', str(end_time - start_time),
             '-i', raw_audio_filename]
    if raw_audio_extension == 'ogg':
      call2 = ['-codec:a', 'libvorbis', '-strict', 'experimental']
    else:
      call2 = []
    call3 = [clip_filename]
    process = subprocess.run(call1 + call2 + call3)
    if process.returncode != 0:
      print("Error: {} encountered by {}".format(
          process.returncode, clip_filename))
    else:
      print(clip_filename)


def generate_dataset(specs, clipped_audio, output):
  spec_filenames = next(os.walk(specs))[2]
  audio_filenames = next(os.walk(clipped_audio))[2]
  clips = collections.defaultdict(dict)
  for spec_filename in spec_filenames:
    spec_root, spec_ext = os.path.splitext(spec_filename)
    clips[spec_root]['spec'] = spec_filename

  for audio_filename in audio_filenames:
    audio_root, audio_ext = os.path.splitext(audio_filename)
    clips[audio_root]['audio'] = audio_filename

  writer = tf.python_io.TFRecordWriter(output)
  for clip_name in clips:
    clip = clips[clip_name]
    if 'audio' in clip and 'spec' in clip:
      example = generate_example(os.path.join(specs, clip['spec']),
                                 os.path.join(clipped_audio, clip['audio']))
      writer.write(example.SerializeToString())
    print(clip_name)
  writer.close()


def generate_example(spec_filename, audio_filename):
  with open(spec_filename) as f:
    spec = json.load(f)

  spec_duration = (spec['audio_source']['end_time'] -
                   spec['audio_source']['start_time'])
  sample_duration = librosa.get_duration(filename=audio_filename)
  if not math.isclose(spec_duration, sample_duration):
    print("Warning: sample duration is {} but spec says {}".format(
        sample_duration, spec_duration))

  sample, sampling_rate = librosa.load(audio_filename, sr=44100)
  if sampling_rate != 44100:
    print("Warning: sampling rate is {}".format(sampling_rate))
  
  return tf.train.SequenceExample(
      context=tf.train.Features(feature={
          'data_source': tf.train.Feature(
              bytes_list=tf.train.BytesList(
                  value=[bytes(spec['data_source'], 'utf-8')])),
          'tonic': tf.train.Feature(
              int64_list=tf.train.Int64List(
                  value=[spec['key']['tonic']])),
          'mode': tf.train.Feature(
              int64_list=tf.train.Int64List(
                  value=[spec['key']['mode']])),
          'beats': tf.train.Feature(
              int64_list=tf.train.Int64List(
                  value=[spec['meter']['beats']])),
          'beats_per_measure': tf.train.Feature(
              int64_list=tf.train.Int64List(
                  value=[spec['meter']['beats_per_measure']])),
      }),
      feature_lists=tf.train.FeatureLists(feature_list={
          'audio': tf.train.FeatureList(
              feature=[tf.train.Feature(
                  float_list=tf.train.FloatList(value=sample.tolist()))]),
          'melody': tf.train.FeatureList(
              feature=[tf.train.Feature(
                  int64_list=tf.train.Int64List(value=[]))]),
          'harmony': tf.train.FeatureList(
              feature=[tf.train.Feature(
                  int64_list=tf.train.Int64List(value=[]))]),
  }))


if __name__ == '__main__':
  args = docopt.docopt(__doc__, sys.argv[1:])
  if args is not None:
    if args['generate_specs']:
      generate_specs(args['<theorytabs>'], args['<audio>'], args['<output>'])
    elif args['clip_audio']:
      clip_audio(args['<specs>'], args['<raw_audio>'], args['<output>'])
    elif args['generate_dataset']:
      generate_dataset(args['<specs>'], args['<clipped_audio>'],
                       args['<output>'])
