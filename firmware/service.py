"""Service: ties the CameraServer model to running CameraWorkers.

Besides the configured cameras, it exposes "virtual" cameras: every attached video
device and microphone that no configured camera claims. A webcam and its own mic
(same USB device) show up as one virtual camera. Virtual cameras are not saved; the
first change made to one adopts it into the configuration under the same id.
"""
from __future__ import annotations

import logging
import re
import shutil
import threading
from pathlib import Path

from .audio import AudioHub
from . import controls
from .camera import Camera
from .camera_server import TOKEN_PATTERN, SEGMENT_CHOICES, CameraServer, DeviceConfig
from .devices import (AudioDevice, VideoDevice, audio_matches, list_audio_devices,
                      list_modes, list_video_devices, node_for, usb_device)
from .metrics import Metrics
from .objects import YoloDetector
from .rtsp import RtspServer, new_token
from .storage import Storage
from .worker import CameraWorker

log = logging.getLogger(__name__)

DISCOVERY_INTERVAL = 3.0
PRUNE_INTERVAL = 30.0


def _claims_video(cam: Camera, dev: VideoDevice) -> bool:
    return bool(cam.port or cam.device_id) and (not cam.port or cam.port == dev.port) \
        and (not cam.device_id or cam.device_id == dev.device_id)


def _claims_audio(cam: Camera, dev: AudioDevice) -> bool:
    return bool(cam.audio_source) and audio_matches(cam.audio_source, dev)


def _virtual_id(kind: str, ident: str) -> str:
    return f"{kind}-{re.sub(r'[^a-z0-9_-]+', '-', ident.lower()).strip('-')}"[:60]


class Service:
    def __init__(self, server: CameraServer, metrics_interval: float = 10,
                 metrics_path: str = "metrics.jsonl") -> None:
        self.server = server
        self.storage = Storage(server.config)
        self.metrics = Metrics(metrics_path, lambda: self.storage.root, metrics_interval)
        self.hub = AudioHub()
        self.detector = YoloDetector(server.config.yolo_model)
        self._workers: dict[str, CameraWorker] = {}
        self.rtsp: RtspServer | None = None
        self._virtual: dict[str, Camera] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._monitor = threading.Thread(target=self._monitor_loop, name="discovery",
                                         daemon=True)
        self._pruner = threading.Thread(target=self._prune_loop, name="prune", daemon=True)

    def start(self) -> None:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg not found; it is required for recording")
        self._migrate_audio_sources()
        for cam in self.server.cameras():
            self._start_worker(cam)
        self.refresh()
        self._monitor.start()
        self._pruner.start()
        self.metrics.start()
        if not self.server.config.token:
            self.server.config.token = new_token()
            self._persist()
        if self.server.config.rtsp_enabled:
            self._start_rtsp()

    def stop(self) -> None:
        self._stop.set()
        self.metrics.stop()
        self._stop_rtsp()
        with self._lock:
            workers, self._workers = list(self._workers.values()), {}
        for w in workers:
            w.stop()
        self.hub.stop()

    # -- lookup -------------------------------------------------------------

    def cameras(self) -> list[Camera]:
        """Configured cameras first, then virtual ones."""
        with self._lock:
            return self.server.cameras() + list(self._virtual.values())

    def camera(self, camera_id: str) -> Camera:
        with self._lock:
            if camera_id in self._virtual:
                return self._virtual[camera_id]
            return self.server.get_camera(camera_id)

    def is_virtual(self, camera_id: str) -> bool:
        with self._lock:
            return camera_id in self._virtual

    def worker(self, camera_id: str) -> CameraWorker:
        with self._lock:
            try:
                return self._workers[camera_id]
            except KeyError:
                raise KeyError(f"no such camera: {camera_id}") from None

    def devices(self) -> dict:
        """Attached devices, each with the configured camera that claims it (if any)."""
        cams = self.server.cameras()
        video = [{**d.to_dict(), "camera": next((c.id for c in cams if _claims_video(c, d)), None)}
                 for d in list_video_devices()]
        audio = [{**d.to_dict(), "camera": next((c.id for c in cams if _claims_audio(c, d)), None)}
                 for d in list_audio_devices()]
        return {"video": video, "audio": audio}

    # -- configuration changes ------------------------------------------------

    def modes_for(self, cam: Camera) -> list[tuple[int, int, int, str]]:
        """Capture modes (width, height, fps, fourcc) the camera's device offers now."""
        node = node_for(cam.port, cam.device_id, cam.source)
        return list_modes(node) if node else []

    def logical_controls(self, cam: Camera) -> list[controls.Logical]:
        """Hardware image controls the camera's device offers now (empty: none / unplugged)."""
        node = node_for(cam.port, cam.device_id, cam.source)
        return controls.logical_controls(node) if node else []

    def _check_controls(self, cam: Camera, new: dict) -> None:
        probe = Camera.from_dict(cam.to_dict())
        probe.update_settings(controls=new)             # structure
        node = node_for(cam.port, cam.device_id, cam.source)
        if node:
            controls.validate(node, new)                # ranges, against the real device

    def update_device(self, device_name=None, segment_minutes=None, storage_dir=None,
                      prune_enabled=None, min_free_percent=None, rtsp_enabled=None,
                      token=None, latitude=..., longitude=...) -> None:
        """Change global settings; running cameras pick them up immediately. Everything
        is validated first, so a bad value changes nothing."""
        cfg = self.server.config
        if segment_minutes is not None and segment_minutes not in SEGMENT_CHOICES:
            raise ValueError(f"segment_minutes must be one of {', '.join(map(str, SEGMENT_CHOICES))}")
        if device_name is not None and not str(device_name).strip():
            raise ValueError("device_name must not be empty")
        if min_free_percent is not None and not 1 <= min_free_percent <= 50:
            raise ValueError("min_free_percent must be 1-50")
        if token is not None and not TOKEN_PATTERN.match(str(token)):
            raise ValueError("token must be 4-64 letters, digits, - or _")
        if (latitude is ...) != (longitude is ...):
            raise ValueError("set latitude and longitude together (null for both: from the time zone)")
        if latitude is not ...:
            probe = DeviceConfig(latitude=None if latitude is None else float(latitude),
                                 longitude=None if longitude is None else float(longitude))
            probe.validate()
            latitude, longitude = probe.latitude, probe.longitude
        if storage_dir is not None:
            storage_dir = self.storage.validate_path(storage_dir)
        if segment_minutes is not None:
            cfg.segment_minutes = segment_minutes
        if device_name is not None:
            cfg.device_name = str(device_name).strip()
        if storage_dir is not None:
            cfg.storage_dir = storage_dir
        if prune_enabled is not None:
            cfg.prune_enabled = bool(prune_enabled)
        if min_free_percent is not None:
            cfg.min_free_percent = float(min_free_percent)
        if token is not None:
            cfg.token = str(token)        # new connections need it at once
        if latitude is not ...:
            cfg.latitude, cfg.longitude = latitude, longitude
        if rtsp_enabled is not None and bool(rtsp_enabled) != cfg.rtsp_enabled:
            cfg.rtsp_enabled = bool(rtsp_enabled)
            if cfg.rtsp_enabled:
                self._start_rtsp()
            else:
                self._stop_rtsp()
        self._persist()

    def reset_camera(self, camera_id: str) -> Camera:
        """Restore a saved camera's settings to the defaults; the running camera follows
        within a moment (hardware controls go back to the camera's own modes)."""
        with self._lock:
            if camera_id in self._virtual:
                raise ValueError("this camera is not saved yet, so it has no settings to restore")
            cam = self.server.get_camera(camera_id)
            cam.reset_settings()
            self._persist()
            return cam

    def _check_object_mode(self, record_mode) -> None:
        if record_mode == "object" and not self.detector.available:
            raise ValueError(f"object detection model not found: {self.detector.path}")

    def add_camera(self, camera: Camera) -> None:
        self._check_object_mode(camera.settings.record_mode)
        with self._lock:
            self._adopt(camera)

    def remove_camera(self, camera_id: str) -> None:
        """Delete a saved camera; its recordings are kept. If its devices are still
        attached they come straight back as a new virtual camera with default settings."""
        with self._lock:
            self.server.remove_camera(camera_id)
            worker = self._workers.pop(camera_id, None)
            if worker:
                worker.stop()
            self._persist()
            self.refresh()          # its devices may now show up as a virtual camera

    def update_camera(self, camera_id: str, name=None, source=None, enabled=None,
                      port=None, device_id=None, audio_source=None, **settings) -> Camera:
        self._check_object_mode(settings.get("record_mode"))
        with self._lock:
            cam = self.camera(camera_id)
            for key in ("controls", "controls_dark"):
                if key in settings:
                    self._check_controls(cam, settings[key])
            if camera_id in self._virtual:
                # work on a copy so a rejected change leaves nothing half-adopted
                cam = Camera.from_dict(cam.to_dict())
            if camera_id in self._virtual:
                self._apply(cam, name, source, enabled, port, device_id, audio_source, settings)
                self._adopt(cam)
            else:
                self._apply(cam, name, source, enabled, port, device_id, audio_source, settings)
                self._persist()
            return cam

    @staticmethod
    def _apply(cam, name, source, enabled, port, device_id, audio_source, settings) -> None:
        if settings:
            cam.update_settings(**settings)
        if name is not None:
            cam.name = name
        if source is not None:
            cam.source = str(source)
        if enabled is not None:
            cam.enabled = bool(enabled)
        if port is not None:
            cam.port = port
        if device_id is not None:
            cam.device_id = device_id
        if audio_source is not None:
            cam.audio_source = audio_source

    def _adopt(self, cam: Camera) -> None:
        """Put a camera into the saved configuration and start it, replacing a virtual
        camera with the same id (which must release its devices first)."""
        old = self._virtual.pop(cam.id, None)
        if old is not None:
            worker = self._workers.pop(cam.id, None)
            if worker:
                worker.stop()
        try:
            self.server.add_camera(cam)
        except ValueError:
            if old is not None:             # put the virtual camera back
                self._virtual[cam.id] = old
                self._start_worker(old)
            raise
        self._persist()
        self.refresh()                      # drops virtual cameras that cam now claims
        self._start_worker(cam)

    # -- discovery ------------------------------------------------------------

    def refresh(self) -> None:
        """Reconcile virtual cameras with the devices currently attached."""
        with self._lock:
            wanted = self._discover_virtual()
            for cid in [c for c in self._virtual if c not in wanted]:
                del self._virtual[cid]
                worker = self._workers.pop(cid, None)
                if worker:
                    worker.stop()
            for cid, cam in wanted.items():
                if cid not in self._virtual:
                    self._virtual[cid] = cam
                    self._start_worker(cam)
                    log.info("new device: virtual camera %s (%s)", cid, cam.name)

    def _discover_virtual(self) -> dict[str, Camera]:
        configured = self.server.cameras()
        taken = {c.id for c in configured}
        videos = [d for d in list_video_devices() if not any(_claims_video(c, d) for c in configured)]
        mics = [d for d in list_audio_devices() if not any(_claims_audio(c, d) for c in configured)]
        out: dict[str, Camera] = {}
        for v in videos:
            # a webcam's own mic sits on the same USB device
            mic = next((m for m in mics if usb_device(m.port) and
                        usb_device(m.port) == usb_device(v.port)), None)
            if mic:
                mics.remove(mic)
            cam = Camera(_virtual_id("video", v.device_id or v.port), v.name or v.node,
                         port=v.port, device_id=v.device_id,
                         audio_source=mic.key if mic else "")
            out[cam.id] = cam
        for m in mics:
            cam = Camera(_virtual_id("audio", m.device_id or m.port), m.name,
                         audio_source=m.key)
            out[cam.id] = cam
        return {cid: c for cid, c in out.items() if cid not in taken}

    @property
    def recordings_dir(self) -> Path:
        """Where recordings are now; follows the global storage setting."""
        return self.storage.root

    def _prune_loop(self) -> None:
        while True:
            try:
                self.storage.prune()
            except Exception:
                log.exception("pruning failed")
            if self._stop.wait(PRUNE_INTERVAL):
                return

    def _monitor_loop(self) -> None:
        while not self._stop.wait(DISCOVERY_INTERVAL):
            try:
                self.refresh()
            except Exception:
                log.exception("device discovery failed")

    def _migrate_audio_sources(self) -> None:
        """Turn old-style "alsa:...CARD=x..." sources into hot-pluggable port+id keys."""
        changed = False
        devices = list_audio_devices()
        for cam in self.server.cameras():
            if cam.audio_source.startswith("alsa:"):
                dev = next((d for d in devices if audio_matches(cam.audio_source, d)), None)
                if dev:
                    cam.audio_source = dev.key
                    changed = True
                    log.info("%s: audio_source migrated to %s", cam.id, dev.key)
        if changed:
            self._persist()

    # -- internals ------------------------------------------------------------

    def _start_rtsp(self) -> None:
        rtsp = RtspServer(self)
        try:
            rtsp.start()
        except OSError as e:
            log.error("rtsp: cannot listen on port %d: %s", self.server.config.rtsp_port, e)
            return
        self.rtsp = rtsp

    def _stop_rtsp(self) -> None:
        rtsp, self.rtsp = self.rtsp, None
        if rtsp is not None:
            rtsp.stop()

    def _start_worker(self, camera: Camera) -> None:
        worker = CameraWorker(camera, self.hub, self.server.config, self.detector)
        self._workers[camera.id] = worker
        worker.start()

    def _persist(self) -> None:
        if self.server.config_path:
            self.server.save()
