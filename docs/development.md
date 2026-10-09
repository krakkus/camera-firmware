# Development

[Documentation](index.md) › Development

## Layout

```
main.py                 entry point: loads config.json, starts the service and web servers
firmware/
  camera.py             Camera and CameraSettings: validation, conversion of old settings
  camera_server.py      DeviceConfig (global settings) and the camera list, saved as JSON
  service.py            runs a worker per camera, finds new devices, prunes storage
  worker.py             per-camera capture thread: frames for the live view, recorder,
                        image controls, day/night and RTSP
  recorder.py           ffmpeg encoder feed, recording segments, motion detection, pre-roll
  rtsp.py               RTSP server: on-demand encoder per camera, fan-out to viewers
  controls.py           V4L2 controls and the software loop
  daynight.py           which control set applies: light level, or sunset and sunrise
  devices.py            finding cameras, microphones and capture modes
  audio.py              microphone capture, shared between cameras
  objects.py            person detection (OpenCV HOG)
  storage.py            storage location, usage, pruning
  metrics.py            performance sampler
  auth.py               the token: URL, user/password, session cookie
  tls.py                the self-signed HTTPS certificate
  config_form.py        view model for the Config page
  web.py, templates/    Flask app, pages and API
  static/vendor/        Chart.js, its zoom plugin and Hammer.js, served locally
deploy/                 systemd unit
```

## How it fits together

- One **worker thread per camera** opens the device with OpenCV, applies rotation, and for
  every frame: updates the image controls, picks the light or dark set, feeds the recorder,
  publishes a JPEG for the live view, and hands the frame to RTSP publishers.
- The **recorder** writes raw frames into an ffmpeg process per file (`Encoder` in
  `recorder.py`), resampled to a constant frame rate, with microphone audio on a second pipe.
- **RTSP** reuses that `Encoder`, writing RTP to local UDP sockets; `rtsp.py` answers the RTSP
  protocol and copies the packets to every viewer.
- **Image control** writes go through a separate thread (`v4l2-ctl` can be slow), so they
  never hold up capture.
- The **service** loop looks for devices every 3 seconds and creates or removes workers for
  new devices.

## Known limits

- Linux only. AMD GPUs are not sampled.
- Some webcams accept exposure or gain changes but ignore them.
- Object detection runs on the CPU; each camera in object mode adds a check about every
  0.4 s.
- Flask's built-in web server; fine on a local network, not built to face the internet.
- Templates are cached: restart after editing one.
- No automated tests yet.
