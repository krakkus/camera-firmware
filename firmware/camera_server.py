"""CameraServer: device-level config plus the collection of cameras."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .camera import Camera

SEGMENT_CHOICES = (1, 3, 5, 10)         # minutes offered by the UI/API
DEFAULT_STORAGE_DIR = "recordings"      # in the working directory, unless configured elsewhere
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,64}$")     # URL-safe without escaping


@dataclass
class DeviceConfig:
    device_name: str = "camera-server"
    host: str = "0.0.0.0"
    port: int = 5000
    # where recordings go: <storage_dir>/<camera_id>/<file>. Relative paths are
    # relative to the working directory the service was started from.
    storage_dir: str = DEFAULT_STORAGE_DIR
    # when free space on the storage disk falls below min_free_percent, the oldest
    # recordings are deleted until it is back above it (plus a small margin)
    prune_enabled: bool = True
    min_free_percent: float = 10.0
    # maximum length of one recording file, in every record mode. The web UI offers
    # SEGMENT_CHOICES; the config file may hold any positive number (handy for testing).
    segment_minutes: float = 5
    # The device's access token, the only credential: it goes in the URL, or is the
    # password of user "admin"/"root". Used by RTSP. Generated on first start if empty.
    token: str = ""
    # where the device is, for sunrise and sunset (daynight.py). None: a city in the
    # system time zone
    latitude: float | None = None
    longitude: float | None = None
    # HTTPS next to HTTP, with a self-signed certificate made on first start (tls.py).
    # 0: off. Plain HTTP stays for programs that cannot accept such a certificate;
    # browsers opening a page over HTTP are sent to HTTPS when http_redirect is on.
    https_port: int = 8443
    http_redirect: bool = True
    tls_dir: str = "tls"            # cert.pem and key.pem; relative to the working directory
    # live RTSP streams (see rtsp.py)
    rtsp_enabled: bool = True
    rtsp_port: int = 8554           # TCP; UDP viewers also use this port and the next

    def validate(self) -> None:
        if self.segment_minutes <= 0:
            raise ValueError("segment_minutes must be positive")
        if not 1 <= self.min_free_percent <= 50:
            raise ValueError("min_free_percent must be 1-50")
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("set both latitude and longitude, or neither")
        if self.latitude is not None and not (-90 <= self.latitude <= 90
                                              and -180 <= self.longitude <= 180):
            raise ValueError("latitude must be -90 to 90 and longitude -180 to 180")
        if self.token and not TOKEN_PATTERN.match(self.token):
            raise ValueError("token must be 4-64 letters, digits, - or _")


class CameraServer:
    """Holds device config and any number of Camera objects.

    Thread-safe, since the service loop and Flask request threads will share it.
    State is persisted to a JSON file so config survives restarts.
    """

    def __init__(self, config: DeviceConfig | None = None,
                 config_path: str | os.PathLike | None = None) -> None:
        self.config = config or DeviceConfig()
        self.config_path = Path(config_path) if config_path else None
        self._cameras: dict[str, Camera] = {}
        self._lock = threading.RLock()

    # --- camera management -------------------------------------------------

    def add_camera(self, camera: Camera) -> Camera:
        with self._lock:
            if camera.id in self._cameras:
                raise ValueError(f"camera '{camera.id}' already exists")
            self._cameras[camera.id] = camera
        return camera

    def remove_camera(self, camera_id: str) -> Camera:
        with self._lock:
            try:
                return self._cameras.pop(camera_id)
            except KeyError:
                raise KeyError(f"no such camera: {camera_id}") from None

    def get_camera(self, camera_id: str) -> Camera:
        with self._lock:
            try:
                return self._cameras[camera_id]
            except KeyError:
                raise KeyError(f"no such camera: {camera_id}") from None

    def cameras(self) -> list[Camera]:
        with self._lock:
            return list(self._cameras.values())

    # --- persistence -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "config": asdict(self.config),
                "cameras": [c.to_dict() for c in self._cameras.values()],
            }

    @classmethod
    def from_dict(cls, data: dict[str, Any],
                  config_path: str | os.PathLike | None = None) -> CameraServer:
        cfg = dict(data.get("config", {}))
        cfg.pop("yolo_model", None)         # the YOLO detector was replaced by OpenCV's own
        cfg.pop("ignored_devices", None)    # a short-lived setting that was removed again
        cfg.pop("timezone", None)           # never used: times follow the system time zone
        if "rtsp_token" in cfg:             # renamed: the token is not only for RTSP
            cfg.setdefault("token", cfg.pop("rtsp_token"))
        if "recordings_dir" in cfg:         # renamed
            cfg.setdefault("storage_dir", cfg.pop("recordings_dir"))
        server = cls(DeviceConfig(**cfg), config_path)
        for cam in data.get("cameras", []):
            server.add_camera(Camera.from_dict(cam))
        return server

    def save(self) -> None:
        if not self.config_path:
            raise RuntimeError("no config_path set")
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        # write to a temp file then rename, so a crash can't leave a half-written config
        fd, tmp = tempfile.mkstemp(dir=self.config_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.to_dict(), f, indent=2)
            os.replace(tmp, self.config_path)
        except BaseException:
            os.unlink(tmp)
            raise

    @classmethod
    def load(cls, config_path: str | os.PathLike) -> CameraServer:
        """Load from file; returns a fresh server bound to the path if it doesn't exist."""
        path = Path(config_path)
        if not path.exists():
            return cls(config_path=path)
        return cls.from_dict(json.loads(path.read_text()), path)
