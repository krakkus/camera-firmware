"""Discover attached V4L2 capture devices and identify them by USB port + device id.

port       where it is plugged in, e.g. "pci-0000:0d:00.0-usb-0:5.4:1.0"
device_id  what it is, including serial when the device has one,
           e.g. "usb-Fifine_Fifine_K420_YGR80PU1200Fi25072217"

/dev/videoN numbers change when devices are replugged; these two do not.
"""
from __future__ import annotations

import functools
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

BY_PATH = Path("/dev/v4l/by-path")
BY_ID = Path("/dev/v4l/by-id")
SUFFIX = "-video-index0"       # index0 is the capture node; index1 is usually metadata


@dataclass(frozen=True)
class VideoDevice:
    node: str          # current /dev/videoN
    port: str
    device_id: str
    name: str

    def to_dict(self) -> dict:
        return asdict(self)


def _links(directory: Path) -> dict[str, str]:
    """stem (name without the -video-index0 suffix) -> real device node."""
    if not directory.is_dir():
        return {}
    return {p.name[:-len(SUFFIX)]: os.path.realpath(p)
            for p in directory.iterdir() if p.name.endswith(SUFFIX)}


def list_video_devices() -> list[VideoDevice]:
    ids_by_node = {node: stem for stem, node in _links(BY_ID).items()}
    devices = []
    for port, node in _links(BY_PATH).items():
        if "-usbv2-" in port:           # duplicate alias of the same USB device
            continue
        try:
            name = Path(f"/sys/class/video4linux/{Path(node).name}/name").read_text().strip()
        except OSError:
            name = ""
        devices.append(VideoDevice(node, port, ids_by_node.get(node, ""), name))
    return sorted(devices, key=lambda d: d.port)


def find_device(port: str = "", device_id: str = "") -> VideoDevice | None:
    """Device matching every identifier that is given (port, device_id, or both)."""
    for d in list_video_devices():
        if (not port or d.port == port) and (not device_id or d.device_id == device_id):
            return d
    return None


# --- audio ------------------------------------------------------------------
#
# A camera's `audio_source` is "<port>+<device_id>" (either side may be empty, like
# for video), or "alsa:<name>" to use a raw ALSA device name such as "alsa:default".

@dataclass(frozen=True)
class AudioDevice:
    alsa: str          # name ffmpeg can open, e.g. plughw:CARD=K420,DEV=0
    port: str
    device_id: str
    name: str

    @property
    def key(self) -> str:
        return f"{self.port}+{self.device_id}"

    @property
    def card(self) -> str:
        return self.alsa.split("CARD=")[1].split(",")[0]

    def to_dict(self) -> dict:
        return {**asdict(self), "key": self.key}


@functools.lru_cache(maxsize=None)
def _udev(card_num: str, card_id: str) -> dict[str, str]:
    # cached: properties of an attached card don't change. card_id is in the key so a
    # different device reusing the card number after replug is looked up afresh.
    try:
        out = subprocess.run(
            ["udevadm", "info", "-q", "property", "-p", f"/sys/class/sound/card{card_num}"],
            capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def list_audio_devices() -> list[AudioDevice]:
    """One entry per sound card that can capture."""
    devices = []
    for card_dir in sorted(Path("/proc/asound").glob("card[0-9]*")):
        num = card_dir.name[4:]
        try:
            card_id = (card_dir / "id").read_text().strip()
        except OSError:
            continue
        pcms = sorted(int(p.name[3:-1]) for p in card_dir.glob("pcm*c")
                      if p.name[3:-1].isdigit())
        if not pcms:
            continue                       # playback-only card
        props = _udev(num, card_id)
        serial = props.get("ID_SERIAL", "")
        bus = props.get("ID_BUS", "")
        devices.append(AudioDevice(
            alsa=f"plughw:CARD={card_id},DEV={pcms[0]}",
            port=props.get("ID_PATH", ""),
            device_id=f"{bus}-{serial}" if serial and bus else "",
            name=props.get("ID_MODEL", card_id).replace("_", " ")))
    return devices


def audio_matches(source: str, dev: AudioDevice) -> bool:
    """Does a camera's audio_source refer to this device?"""
    if source.startswith("alsa:"):
        return f"CARD={dev.card}," in source or source.endswith(f"CARD={dev.card}")
    port, _, device_id = source.partition("+")
    return bool(source) and (not port or dev.port == port) and \
        (not device_id or dev.device_id == device_id)


def find_audio_device(source: str) -> AudioDevice | None:
    return next((d for d in list_audio_devices() if audio_matches(source, d)), None)


def resolve_alsa(source: str) -> str | None:
    """ALSA name to capture from right now, or None while the device is unplugged."""
    if source.startswith("alsa:"):
        dev = find_audio_device(source)
        return dev.alsa if dev else source[5:]     # no known card: pass the name through
    dev = find_audio_device(source)
    return dev.alsa if dev else None


def usb_device(port: str) -> str:
    """Port without the USB interface suffix: a webcam's video and mic share this."""
    return port.rsplit(":", 1)[0] if "-usb-" in port else ""


# --- capture modes ------------------------------------------------------------

USABLE_FORMATS = ("MJPG", "YUYV")      # what OpenCV's V4L2 capture handles; MJPG is faster
_MODES_TTL = 30.0
_modes_cache: dict[tuple, tuple[float, list[tuple[int, int, int, str]]]] = {}   # every format


def node_identity(node: str) -> tuple:
    """Changes when another device takes over the node: udev creates /dev/videoN afresh
    on every plug-in. For caches keyed by node."""
    try:
        st = os.stat(node)
        return node, st.st_rdev, st.st_ctime_ns
    except OSError:
        return (node,)


def list_modes(node: str) -> list[tuple[int, int, int, str]]:
    """(width, height, fps, fourcc) the device offers that OpenCV can capture, via v4l2-ctl.
    Empty if unknown (not a V4L2 node, or v4l2-ctl missing)."""
    return [m for m in _all_modes(node) if m[3] in USABLE_FORMATS]


def h264_modes(node: str) -> list[str]:
    """The "WxH@fps" modes the camera delivers H.264 in (for the Config page)."""
    return [f"{w}x{h}@{r}" for w, h, r, f in _all_modes(node) if f == "H264"]


def has_h264(node: str, width: int, height: int, fps: int) -> bool:
    """Does the camera deliver H.264 itself in exactly this mode (for the copy mode)?"""
    return (width, height, fps, "H264") in _all_modes(node)


def _all_modes(node: str) -> list[tuple[int, int, int, str]]:
    now = time.monotonic()
    key = node_identity(node)
    cached = _modes_cache.get(key)
    if cached and now - cached[0] < _MODES_TTL:
        return cached[1]
    modes: list[tuple[int, int, int, str]] = []
    try:
        out = subprocess.run(["v4l2-ctl", "--list-formats-ext", "-d", node],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    fourcc, size = "", None
    for line in out.splitlines():
        line = line.strip()
        m = re.match(r"\[\d+\]: '(\w+)'", line)
        if m:
            fourcc, size = m.group(1), None
        m = re.match(r"Size: Discrete (\d+)x(\d+)", line)
        if m:
            size = (int(m.group(1)), int(m.group(2)))
        m = re.match(r"Interval: Discrete [\d.]+s \(([\d.]+) fps\)", line)
        if m and size and fourcc in (*USABLE_FORMATS, "H264"):
            modes.append((*size, round(float(m.group(1))), fourcc))
    modes.sort(key=lambda m: (-m[0] * m[1], -m[2], m[3]))
    _modes_cache[key] = (now, modes)
    return modes


def node_for(port: str, device_id: str, source: str) -> str:
    """The /dev/videoN a camera config points at right now, or ''."""
    if port or device_id:
        dev = find_device(port, device_id)
        return dev.node if dev else ""
    return source if source.startswith("/dev/video") else ""


def pick_fourcc(node: str, width: int, height: int, fps: int) -> str:
    """Pixel format to request: MJPG when the mode exists in it (much faster over USB)."""
    formats = {f for w, h, r, f in list_modes(node) if (w, h, r) == (width, height, fps)}
    return "MJPG" if "MJPG" in formats else ("YUYV" if "YUYV" in formats else "")
