# Recording

[Documentation](index.md) › Recording

## Record modes

| Mode (`record_mode`) | Shown as | Records |
|---|---|---|
| `off` | No | nothing |
| `continuous` | Always | all the time |
| `motion` | Motion detect | while the picture changes |
| `object` | Object detect | while a chosen kind of object is seen |

Motion and object modes need video. An audio-only camera records `off` or `continuous`.

### Motion

Each frame is compared, at 320 pixels wide, with a slowly adapting background. A pixel
counts as changed when it differs by more than `motion_threshold` (1-255, default 25);
there is motion when at least `motion_min_area` pixels changed (default 500). Lower values
are more sensitive: more recordings, more false alarms from leaves, rain and headlights.

### Objects

A YOLOv8 model looks at the frames in a separate thread, about every 0.4 seconds. A clip is
recorded while any of the chosen `object_classes` (the 80 COCO names: person, car,
bicycle, cat, dog, ...) is seen with at least `object_confidence` (0.05-0.95, default 0.5).
It runs on the CPU. The model is not included, see [Installation](install.md#object-detection-optional);
without it, the API refuses this mode.

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

## Storage and pruning

The storage location is chosen on the Config page: suggested disks, or any path. A new
location applies to new files; existing recordings stay where they are.

With pruning on (`prune_enabled`), the oldest recordings of all cameras are deleted when
the free space on that disk drops below `min_free_percent` (1-50, default 10), until it is
about 2% above it. This is checked every 30 seconds and can be started with "Prune now".
Only files named like recordings are deleted, never one changed in the last 2 minutes.
