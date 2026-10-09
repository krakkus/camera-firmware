# Installation

[Documentation](index.md) › Installation

## Requirements

- Linux, Python 3.10 or newer (tested on 3.12)
- `ffmpeg` with `libx264` and `aac`: recording, RTSP and microphone capture
- `v4l2-ctl` (package `v4l-utils`): camera modes and image controls
- `openssl`: the HTTPS certificate
- `udevadm` (part of systemd): recognising microphones
- Python packages from `requirements.txt`: Flask, OpenCV (headless), NumPy, astral

Optional:

- `nvidia-smi`, or `intel_gpu_top` with a sudo rule (below), for the GPU chart

## Install

```sh
sudo apt install python3-venv ffmpeg v4l-utils openssl
git clone https://github.com/krakkus/camera-firmware.git
cd camera-firmware
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.json config.json
```

The user that runs it needs access to the cameras and microphones: membership of the
`video` and `audio` groups (`sudo usermod -aG video,audio $USER`, then log in again).

## First start

```sh
.venv/bin/python main.py                  # or: main.py --config /path/to/config.json
```

It listens on `127.0.0.1:5000` with the example config. On first start it makes:

- a **token** (written to `config.json` as `"token"`): the password for everything, see
  [Access and security](access.md)
- a **self-signed certificate** in `tls/`, for HTTPS on port 8443

Open http://127.0.0.1:5000, log in with the token, and every attached camera and
microphone shows up as a "new device". Change anything on one and press Save to keep it.

To reach it from other machines set `"host": "0.0.0.0"` in `config.json` (and choose a
`port`, for example 8080). Without a `config.json` at all, the service creates one that
listens on all interfaces.

## Run as a service

A systemd unit starts it at boot and restarts it if it stops. Adjust the user and paths in
[deploy/camera-firmware.service](../deploy/camera-firmware.service), then:

```sh
sudo cp deploy/camera-firmware.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now camera-firmware
journalctl -u camera-firmware -f           # its log
```

The unit stops the service with SIGINT, so recordings are closed cleanly.

## GPU chart (optional)

With an NVIDIA card, `nvidia-smi` is used as is. For Intel graphics, `intel_gpu_top`
(package `intel-gpu-tools`) needs root; allow just that command without a password:

```sh
echo "$USER ALL=(root) NOPASSWD: /usr/bin/intel_gpu_top" | sudo tee /etc/sudoers.d/intel_gpu_top
```

AMD cards are not sampled; the chart then says so.

## Update

```sh
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart camera-firmware
```

`config.json`, `tls/`, recordings and the performance history are not in git and stay as
they are. Settings from older versions are converted when they are loaded.
