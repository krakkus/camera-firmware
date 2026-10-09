# Recording

[Documentation](index.md) › Recording

## Record modes

| Mode (`record_mode`) | Shown as | Records |
|---|---|---|
| `off` | No | nothing |
| `continuous` | Always | all the time |
| `motion` | Motion detect | while the picture changes |
| `object` | Person detect | while a person is seen |

Motion and object modes need video. An audio-only camera records `off` or `continuous`.

### Motion

Each frame is compared, at 320 pixels wide, with a slowly adapting background. A pixel
counts as changed when it differs by more than `motion_threshold` (1-255, default 25);
there is motion when at least `motion_min_area` pixels changed (default 500). Lower values
are more sensitive: more recordings, more false alarms from leaves, rain and headlights.

### People

OpenCV's built-in HOG people detector looks at the frames in a separate thread, about every
0.4 seconds, and a clip is recorded while a person is seen. `object_confidence` (0.05-0.95,
default 0.5; the page calls it "Strictness") is the score a detection must reach: higher
means fewer false alarms and more misses. It runs on the CPU and needs no model file.

### Before and after

In motion and object mode the last `pre_roll_seconds` of frames and sound (0-30, default 3)
are kept in memory, so a clip starts that long before the trigger. It keeps recording
`post_roll_seconds` (0-300, default 5) after the last trigger. The pre-roll is kept
uncompressed: at 1280×720 and 10 fps, 3 seconds take about 80 MB of memory per camera.

## Files

- Recordings go to `<storage_dir>/<camera id>/`, named after their start in local time:
  `20261004_221438.mp4`, `..._motion.mp4`, `..._object.mp4`, or `.m4a` for audio only.
- H.264 video (quality `record_crf`, 0-51, default 23: lower is better and bigger) and AAC
  audio when the camera has a microphone.
- **Fragmented mp4**: a crash or power cut loses a few seconds, not the file.
- One file is at most `segment_minutes` long (1, 3, 5 or 10; default 5), in every mode.
- Video is written at a constant frame rate: frames from an irregular webcam are repeated
  or dropped to fit. Audio is aligned to the first frame.

A change of resolution or storage location starts a new file. A change of frame rate (for
instance when the dark set uses another one) does not; the picture freezes for about a
second while the camera reopens.

## Software or hardware encoding

Per camera, **Encoding** on the Config page (`record_codec`) chooses how recordings are made.

| | Software (`x264`) | Hardware (`copy`) |
|---|---|---|
| What it does | the firmware encodes the frames it captures | records the camera's own H.264 stream, without encoding |
| Needs | any camera | a camera that offers H.264 in the chosen resolution and frame rate |
| CPU | high: it is the biggest load of the firmware | very low |
| Rotation, flips | yes | no |
| Audio | yes | no |
| Quality | `record_crf` | what the camera's encoder gives |

The Config page offers Hardware only when the camera has an H.264 mode at the chosen
resolution and frame rate; choosing it greys out rotation, flips and the quality. If a camera
that was saved as Hardware loses that mode, it is recorded in software instead.

### Hardware (copy) mode

One ffmpeg process owns the camera. It copies the camera's H.264 into a ring of 2-second
segments in `<storage_dir>/<camera id>/ring/`, and sends a small decoded picture (at most
960 pixels wide, 10 fps) to the firmware, which uses it for the live view, snapshots, motion
and person detection and the image controls. A recording is a range of ring segments joined
into one mp4 without encoding, so continuous, motion and person recording all work the same
way, with pre-roll and post-roll, and changing the record mode never reopens the camera.

- Cuts happen at the camera's keyframes, so pre-roll is accurate to about one keyframe
  interval, and a recording can run a little past its post-roll.
- The live view, snapshots and RTSP (which encodes the live frames itself) show the small
  picture, not the full resolution.
- Segments a crash or power cut left in the ring are made into recordings at the next start.

## Storage and pruning

The storage location is chosen on the Config page: suggested disks, or any path. A new
location applies to new files; existing recordings stay where they are.

With pruning on (`prune_enabled`), the oldest recordings of all cameras are deleted when
the free space on that disk drops below `min_free_percent` (1-50, default 10), until it is
about 2% above it. This is checked every 30 seconds and can be started with "Prune now".
Only files named like recordings are deleted, never one changed in the last 2 minutes.
