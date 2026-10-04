# Camera firmware

A Python service with a Flask web interface that manages any number of cameras and
microphones: live view, snapshots, RTSP streams, recording to mp4 (continuous, on motion, or when a chosen
object appears), automatic disk pruning, and per-camera image controls. It runs as one
process; there is nothing to install besides Python packages and two command-line tools.

- **Cameras are hot-pluggable** and identified by USB port + device id, not `/dev/videoN`.
- **Any number of cameras, including none.** Unconfigured devices show up as "new device"
  cards you can configure with one click.
- **Audio** from a camera's own microphone (or any ALSA device), shared between cameras.
- **Recording** as fragmented H.264/AAC mp4, so a crash loses seconds, not a whole file.

## Requirements

- Linux and Python 3.10 or newer (the code uses nothing newer, but it is only tested on 3.12)
- `ffmpeg` with `libx264` and `aac` (recording and microphone capture)
- `v4l2-ctl` (package `v4l-utils`), for camera modes and hardware image controls
- `udevadm` (part of systemd/udev), to identify audio devices
- Python package `astral` (in `requirements.txt`), for sunrise and sunset
- Optional: `nvidia-smi` (or `intel_gpu_top` plus a sudo rule) for the GPU chart; a YOLOv8
  ONNX model for object detection (see [models/README.md](models/README.md))

## Quick start

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.json config.json      # see "Configuration" before exposing it
.venv/bin/python main.py                # or: main.py --config /path/to/config.json
```

Then open http://127.0.0.1:5000. The page lists every camera and microphone it finds.

> **Everything needs the device token** (see [Access](#access)): it is generated on first
> start and written to `config.json` as `token`. The example config binds to `127.0.0.1`;
> without a `config.json` the service listens on **all** interfaces (`0.0.0.0`). Traffic is
> HTTPS (self-signed, see [Access](#access)) next to plain HTTP and RTSP, which anyone on the
> network can read, token included: don't expose it to the internet; use a VPN for that. It also runs on Flask's development server,
> not a production WSGI server.

## Configuration

`config.json` holds device-wide settings and the saved cameras. The web UI edits it; it is
written atomically on every change. It is not committed (it contains this machine's device
serials); `config.example.json` shows the format.

| `config` key | Default | Meaning |
|---|---|---|
| `device_name` | `camera-server` | Shown in the page header |
| `host`, `port` | `0.0.0.0`, `5000` | Where the web server listens (restart to change) |
| `storage_dir` | `recordings` | Recordings go to `<storage_dir>/<camera id>/`. Relative paths are relative to the working directory |
| `segment_minutes` | `5` | Maximum length of one recording file, in every record mode. The UI offers 1, 3, 5, 10 |
| `prune_enabled`, `min_free_percent` | `true`, `10` | Delete the oldest recordings when free space falls below this |
| `yolo_model` | `models/yolov8n.onnx` | YOLOv8-format ONNX model for object detection |
| `latitude`, `longitude` | none | Where the device is, for sunrise and sunset. Without them, a city in the system time zone |
| `token` | generated | The access token for everything (see [Access](#access)): 11 random URL-safe characters unless you set one (4-64 of `A-Z a-z 0-9 - _`) |
| `https_port` | `8443` | HTTPS with a self-signed certificate, next to HTTP; `0` turns it off (restart to change) |
| `http_redirect` | `true` | Browsers opening a page over HTTP go to HTTPS; streams and API stay on HTTP too |
| `tls_dir` | `tls` | Where the certificate and key are kept |
| `rtsp_enabled`, `rtsp_port` | `true`, `8554` | RTSP server (see below); UDP viewers also use the port after it |

Each entry in `cameras` has an `id` (letters, digits, `-`, `_`; used in URLs), `name`,
`enabled`, a video source, an optional `audio_source`, and `settings`.

## Access

One token protects everything: pages, API, streams, snapshots, recordings and RTSP. It is
11 random URL-safe characters (like a YouTube video id) unless you set one; the Config
page shows it under Global and makes a new one. Give it in any of these ways:

- `?token=<token>` on any URL, for other programs: `http://<host>:5000/stream/cam0.mjpg?token=...`
- HTTP Basic (RTSP also Digest) with user `admin` or `root` and the token as password:
  `curl -u admin:<token> http://<host>:5000/api/cameras`
- in a browser: the login page, or any page opened once with `?token=`. Either sets a
  session cookie for a year. A new token logs every browser out, except the one that set it

A wrong token costs a second, to slow down guessing. The Live page's copy-ready URLs include
the token.

**HTTPS** runs on port 8443 next to HTTP, with a self-signed certificate made on first
start (in `tls/`, for the host name, `<host name>.local` and the current IP addresses), the
way most network cameras do it. Browsers warn once since nobody vouches for it: compare
the SHA-256 fingerprint on the Config page with the browser's. Opening a page over HTTP
sends the browser to HTTPS; streams, snapshots and the API stay on plain HTTP as well, for
programs that do not accept such a certificate (`curl -k`, ffmpeg's default and most players
do). Delete `tls/` and restart for a new certificate, after a name or address change. RTSP
is not encrypted.

## Web pages

| Page | What it does |
|---|---|
| **Live** (`/`) | Every camera's live view with its stream, frame and RTSP URLs ready to copy |
| **Config** (`/config`) | Per camera: sources, resolution and fps, image controls, orientation, recording, motion and object detection, with a live preview. Also global settings, storage, and the list of detected devices |
| **Recordings** (`/recordings`) | Newest first, grouped by hour, with inline playback and download. Filter by camera |
| **Performance** (`/performance`) | CPU, memory, GPU, disk busy and disk space over the last 24 h; scroll to zoom, drag to pan |

## How it works

### Cameras and identity

A USB camera is identified by its **port** (e.g. `pci-0000:0d:00.0-usb-0:5.4:1.0`) and
**device id** (including the serial number when it has one). The service resolves these to
the current `/dev/videoN` every time it opens the camera, so unplugging and replugging works,
even into the same port. An unplugged camera is retried every 3 seconds. Set one or both:
with only `device_id` the camera follows the device to any port; with only `port` it is
whatever is plugged in there.

Other sources work too: an RTSP/HTTP URL or a plain `/dev/videoN` path. Those cameras stream,
record and detect the same way, but have no hot-plug identity, and URL sources have no
hardware image controls and no audio of their own.

### Virtual cameras

Every video device and microphone that no saved camera uses appears as a **new device**
card. A webcam and its own microphone (same USB device) come as one card. Nothing is saved
until you change something on it. Deleting a saved camera whose device is still attached
makes it reappear as a new device.

### Audio

A camera's `audio_source` is `"<port>+<device_id>"` of a microphone, or `"alsa:<name>"`. Several
cameras can use the same microphone; it is opened once, and only while a camera records. A
camera with no video is an **audio-only camera**: it records `.m4a` files and supports the
`off` and `continuous` modes.

### Recording

Per camera, `record_mode` is one of:

- `off`
- `continuous` (the UI calls it "Always")
- `motion`: frame differencing, with `pre_roll_seconds` / `post_roll_seconds`
- `object`: YOLO runs on the same frames in a side thread; a clip is recorded while any chosen
  class (`object_classes`, the 80 COCO names) is seen at `object_confidence` or higher

Files are named `<YYYYmmdd_HHMMSS>[_motion|_object].mp4` (`.m4a` for audio only). Video is
resampled to a constant frame rate; audio is aligned to the first frame. Motion and object
modes need a video source.

### Storage and pruning

When free space on the storage disk drops below `min_free_percent`, the oldest recordings
(across all cameras) are deleted until it is about 2% above the limit. It checks every 30
seconds. It only deletes files named like its own recordings, and never one modified in the
last 2 minutes. Changing `storage_dir` starts new files in the new place; existing recordings
are not moved.

### Image controls

The Config page lists the hardware controls the camera really offers (read with `v4l2-ctl`):
typically exposure, white balance, brightness, contrast, saturation, sharpness, gain and
power-line frequency. Each has a mode:

- **Camera**: the camera's automatic mode where it has one, otherwise its default value
- **Manual**: a value you enter, checked against the camera's range
- **Software**: a loop adjusts the control from the picture (available for exposure,
  contrast, brightness and gamma). Every 1.5 s it measures the frame with the brightest 5%
  ignored and nudges one control: exposure toward a mean brightness of 100, contrast toward a
  spread of 45, brightness to keep the black level near 12. An **offset** slider (-100..+100)
  shifts each target. If exposure changes visibly do nothing, as on some webcams that ignore
  it, the loop gives up on it and the page says so

### Day and night

Each camera can keep two sets of image controls, **Light** and **Dark** (tabs on the Config
page), each with its own mode per control and its own frame rate (`fps`, `fps_dark`; 0 is the
same as `fps`). A lower frame rate allows longer exposures, so a brighter picture at night.
Switching to a set with another frame rate reopens the camera: about a second of frozen
picture, but the recording file carries on. The page offers the rates the camera lists for
the resolution (in any pixel format: some do their lowest rate only uncompressed) and shows
the rate it delivers. **Day/night** chooses when the Dark set applies:

- **Off**: never; one set, day and night
- **By light level**: when the picture's mean brightness (0-255) stays below *dark below*
  for 2 minutes, and back to Light when it stays above *light again above* for 2 minutes.
  The Dark set itself brightens the picture, so keep the gap between the two wider than
  that, or it switches back and forth
- **By sunset and sunrise**: computed with [astral](https://astral.readthedocs.io) for the
  configured `latitude`/`longitude` (or the time zone's city). *Minutes into the night*
  moves both ends: dark starts that long after sunset and ends that long before sunrise

The page shows which set is in use, and the light level or today's times.

### RTSP streams

Every camera with video is also an RTSP stream: H.264, plus AAC when the camera has a
microphone. For VLC, OBS (Media Source), NVR software, Home Assistant and the like:

```
rtsp://<host>:8554/<camera id>?token=<token>
rtsp://<host>:8554/<token>/<camera id>          for clients that drop the ?query
rtsp://admin:<token>@<host>:8554/<camera id>    for clients that want a user name
```

The token is the only credential. Where a user name is required, `admin` or `root` both
work, with the token as the password (Basic or Digest). The Config page shows it under Global and makes
a new one; changing it locks out new connections that use the old one.

Nothing is encoded while nobody watches. The first viewer of a camera starts one ffmpeg
encoder fed with the same frames as the live view and the recorder (so rotation and image
controls apply) and with the camera's microphone; every other viewer of that camera gets a
copy of the same stream. It stops 10 seconds after the last viewer leaves. A viewer
starting while it runs waits for the next keyframe (one per second), and the first viewer
waits about a second for the encoder. Viewers can use TCP (interleaved) or UDP. The stream
ends, and clients reconnect, when the camera is unplugged or its resolution changes.

The URLs on the Live page include the token, ready to copy.

### Performance history

A background thread samples CPU, memory, disk and GPU every 10 seconds into `metrics.jsonl`
(about 25 hours, kept across restarts). It runs with no cameras attached.

## HTTP API

All bodies are JSON; errors are `{"error": "..."}` with status 400 or 404. Every request
needs the token (see [Access](#access)); without it the answer is 401.

| Request | Purpose |
|---|---|
| `GET /frame/<id>.jpg` | Latest frame (503 until one arrives or while disconnected) |
| `GET /stream/<id>.mjpg` | Live MJPEG stream |
| `rtsp://<host>:8554/<id>?token=<token>` | Live H.264/AAC stream (RTSP) |
| `GET /api/cameras` | All cameras (saved and virtual) with live status |
| `POST /api/cameras` | Add a camera: `{"id", "name", "source" \| "port"/"device_id", ...}` |
| `GET` / `PATCH` / `DELETE /api/cameras/<id>` | Read, change (`name`, `source`, `port`, `device_id`, `audio_source`, `enabled`, `settings`) or delete. Changing a new device saves it |
| `POST /api/cameras/<id>/reset` | Restore a saved camera's settings to the defaults |
| `GET /api/cameras/<id>/controls` | Hardware controls offered, saved modes, and values the software loop has set |
| `GET /api/cameras/<id>/recordings` | Recording files of a camera |
| `GET /recordings/<id>/<file>` | A recording (`?download=1` to download) |
| `GET` / `PATCH /api/device` | Global settings (`device_name`, `segment_minutes`, `storage_dir`, `prune_enabled`, `min_free_percent`, `token`, `rtsp_enabled`) |
| `GET /api/storage`, `POST /api/storage/prune` | Disk usage and candidate locations; prune now |
| `GET /api/devices` | Detected video and audio devices and which camera uses each |
| `GET /metrics/history`, `/metrics/latest?since=<ts>` | Performance samples |

Example: switch a camera to object recording and watch for cats.

```sh
curl -u admin:<token> -X PATCH localhost:5000/api/cameras/cam0 -H 'content-type: application/json' \
  -d '{"settings": {"record_mode": "object", "object_classes": ["cat"], "object_confidence": 0.6}}'
```

## Layout

```
main.py                 entry point
firmware/
  camera.py             Camera and CameraSettings (validation, migration)
  camera_server.py      DeviceConfig, CameraServer (cameras + JSON persistence)
  service.py            runs workers, virtual cameras, storage and discovery loops
  worker.py             per-camera capture thread, preview and recorder feed
  recorder.py           ffmpeg encoder feed and segments, motion detection, pre-roll
  rtsp.py               RTSP server, encoding on demand
  objects.py, coco.py   YOLO detector and class names
  audio.py              microphone capture, shared between cameras
  controls.py           V4L2 controls and the software tuning loop
  devices.py            discovery of cameras, microphones and capture modes
  storage.py            storage location, usage, pruning
  auth.py               the device token: URL, Basic auth, session cookie
  tls.py                self-signed HTTPS certificate
  metrics.py            performance sampler
  web.py, templates/    Flask app and pages
  static/vendor/        Chart.js, zoom plugin, Hammer.js (served locally)
models/README.md        how to produce the object-detection model
```

## Known limits

- Linux only. AMD GPUs are not sampled; the GPU chart then says so.
- Some webcams accept exposure and gain writes but ignore them.
- Detection runs on the CPU. Each camera in object mode adds a YOLO check about every 0.4 s.
- Templates are cached by Flask: restart the service after editing one.
- There is no automated test suite yet.
