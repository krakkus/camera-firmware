"""CameraWorker: capture thread for one Camera. Owns the OpenCV device, the
latest-frame buffer used by the web server, and the recorder."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from .audio import AudioHub
from .camera_server import DeviceConfig
from .controls import ControlManager
from .daynight import DayNight
from .objects import YoloDetector
from .camera import Camera
from .devices import find_device, pick_fourcc, resolve_alsa
from .recorder import Recorder

log = logging.getLogger(__name__)

RECONNECT_DELAY = 3.0       # also the hot-plug polling interval
FPS_WARMUP_FRAMES = 15


class CameraWorker:
    def __init__(self, camera: Camera, hub: AudioHub, config: DeviceConfig,
                 detector: YoloDetector) -> None:
        self.camera = camera
        self.recorder = Recorder(camera.id, lambda: camera.settings, config, detector)
        self._hub = hub
        self._ctl: ControlManager | None = None     # hardware image controls of the open device
        self._daynight = DayNight(config)           # which set of controls applies
        self._audio_source: str | None = None   # source currently subscribed to
        self.connected = False
        self.device: str | None = None      # node currently open
        self.problem: str | None = None     # why not connected, if known
        self.measured_fps = 0.0
        self.target_fps = camera.settings.fps   # requested from the camera now (light or dark set)
        self._cond = threading.Condition()
        self._jpeg: bytes | None = None
        self._seq = 0
        self._listeners: list[Callable[[np.ndarray, float], None]] = []   # live streams
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"cam-{camera.id}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    # -- consumers (Flask threads) ---------------------------------------

    def latest_jpeg(self) -> bytes | None:
        with self._cond:
            return self._jpeg

    def stream_jpegs(self):
        """Yield each new JPEG as it arrives (the last one again after 2 s idle); ends when the worker stops."""
        seq = 0
        while not self._stop.is_set():
            with self._cond:
                self._cond.wait_for(lambda: self._seq != seq or self._stop.is_set(), timeout=2)
                if self._jpeg is None:
                    continue
                # With no new frame in 2 s the last one is sent again: it keeps an idle
                # connection from being dropped, and a vanished client fails the write
                # and frees its thread instead of waiting forever.
                seq, jpeg = self._seq, self._jpeg
            yield jpeg

    def add_frame_listener(self, cb: Callable[[np.ndarray, float], None]) -> None:
        """cb(frame, capture time) runs on the capture thread for every frame: keep it short."""
        self._listeners.append(cb)

    def remove_frame_listener(self, cb: Callable[[np.ndarray, float], None]) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    def status(self) -> dict:
        src = self.camera.audio_source
        return {
            "audio": {"source": src, "present": resolve_alsa(src) is not None} if src else None,
            "connected": self.connected,
            "device": self.device,
            "problem": self.problem,
            "fps": round(self.measured_fps, 1),
            "recording": self.recorder.recording,
            "motion": self.recorder.motion,
            "objects": self.recorder.objects,
            "storage_error": self.recorder.error,
            "controls": self._ctl.software_values() if self._ctl else {},
            "controls_ineffective": self._ctl.ineffective() if self._ctl else [],
            "profile": "dark" if self._daynight.dark else "light",
            "light_level": None if self._daynight.level is None else round(self._daynight.level),
            "sun": self._sun_status(),
        }

    def _sun_status(self) -> dict | None:
        """Today's sunrise/sunset switching times, when this camera switches by sun."""
        s = self.camera.settings
        if s.profile_switch != "sun":
            return None
        w = self._daynight.window(s)
        if w is None:
            return {"error": "location unknown: set latitude and longitude"}
        if "always" in w:
            return w
        return {"light_from": w["light_from"].strftime("%H:%M"),
                "dark_from": w["dark_from"].strftime("%H:%M")}

    # -- capture loop ----------------------------------------------------

    def _resolve(self) -> str | None:
        """What to open right now: the node where a port/id camera is currently
        plugged in, or the configured source. None while a USB camera is unplugged."""
        cam = self.camera
        if cam.port or cam.device_id:
            dev = find_device(cam.port, cam.device_id)
            return dev.node if dev else None
        return cam.source

    def _open(self) -> cv2.VideoCapture | None:
        src = self._resolve()
        if src is None:
            self._note("not plugged in")
            return None
        if src.isdigit():
            cap = cv2.VideoCapture(int(src))
        elif src.startswith("/dev/video"):
            cap = cv2.VideoCapture(src, cv2.CAP_V4L2)
        else:
            cap = cv2.VideoCapture(src)
        if not cap.isOpened():
            cap.release()
            self._note(f"cannot open {src}")
            return None
        s = self.camera.settings
        fourcc = pick_fourcc(src, s.width, s.height, self.target_fps) if src.startswith("/dev/video") else ""
        if fourcc:      # MJPG, when the mode exists in it, gives full fps over USB
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, s.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, s.height)
        cap.set(cv2.CAP_PROP_FPS, self.target_fps)
        self._note(None)
        self.device = src
        if src.startswith("/dev/video"):
            self._ctl = ControlManager(src)
        # the day/night state is kept: a switch to a set with another frame rate reopens
        # the camera, and the first frames after that must not decide it all over again
        log.info("%s: opened %s", self.camera.id, src)
        return cap

    def _note(self, problem: str | None) -> None:
        """Track why the camera is unavailable; log only when that changes."""
        if problem != self.problem:
            if problem:
                log.warning("%s: %s, waiting", self.camera.id, problem)
            self.problem = problem
        if problem:
            self.device = None

    def _run(self) -> None:
        cap: cv2.VideoCapture | None = None
        applied: tuple | None = None   # capture params currently set on the device
        last_tick = time.monotonic()
        frames = 0                     # since open; recording waits for fps to settle
        try:
            while not self._stop.is_set():
                try:
                    cam, s = self.camera, self.camera.settings
                    self._sync_audio(cam.audio_source if cam.enabled and s.record_mode != "off"
                                     else None)
                    if not cam.has_video:
                        # audio-only camera: the recorder works straight off the audio hub
                        self.recorder.set_audio_only(True)
                        if not self._audio_source:
                            self.recorder.close()       # no more chunks will arrive to stop it
                        self._release(cap); cap = None; applied = None
                        self.connected = bool(cam.enabled and cam.audio_source
                                              and resolve_alsa(cam.audio_source))
                        self._stop.wait(0.5)
                        continue
                    self.recorder.set_audio_only(False)
                    if not cam.enabled:
                        self._release(cap); cap = None; applied = None
                        self.recorder.close()
                        self._stop.wait(0.5)
                        continue

                    # the dark set may ask for another frame rate: reopen in that mode
                    self.target_fps = (s.fps_dark or s.fps) if self._daynight.dark else s.fps
                    want = (cam.source, cam.port, cam.device_id, s.width, s.height, self.target_fps)
                    if cap is not None and want != applied:
                        self._release(cap); cap = None     # reopen with new capture params
                    if cap is None:
                        cap = self._open()
                        applied = want
                        if cap is None:
                            self._stop.wait(RECONNECT_DELAY)
                            continue
                        self.connected = True
                        self.measured_fps, frames, last_tick = 0.0, 0, time.monotonic()

                    ok, frame = cap.read()
                    if not ok:
                        log.warning("%s: read failed (unplugged?), reconnecting", cam.id)
                        self._release(cap); cap = None
                        self.recorder.close()
                        self._note("read failed")
                        self._stop.wait(RECONNECT_DELAY)
                        continue

                    frame = self._adjust(frame)
                    now = time.monotonic()
                    dt = now - last_tick
                    last_tick = now
                    frames += 1
                    if dt > 0 and frames > 1:      # first interval includes camera startup
                        self.measured_fps = (0.9 * self.measured_fps + 0.1 / dt
                                             if self.measured_fps else 1 / dt)

                    dark = self._daynight.update(frame, s)
                    if self._ctl:
                        self._ctl.sync(s.controls_dark if dark and s.controls_dark else s.controls)
                        self._ctl.tick(frame, self.target_fps)
                    if frames > FPS_WARMUP_FRAMES:
                        self.recorder.process(frame, s, min(self.measured_fps or self.target_fps,
                                                            self.target_fps))
                    self._publish(frame, s.jpeg_quality)
                    for cb in list(self._listeners):
                        cb(frame, now)
                except Exception:
                    log.exception("%s: capture loop error, restarting camera", self.camera.id)
                    self._release(cap); cap = None
                    self.recorder.close()
                    self._stop.wait(RECONNECT_DELAY)
        finally:
            self._sync_audio(None)
            self._release(cap)
            self.recorder.stop()

    def _sync_audio(self, source: str | None) -> None:
        """Subscribe to the microphone only while it is needed."""
        source = source or None
        if source == self._audio_source:
            return
        if self._audio_source:
            self._hub.unsubscribe(self._audio_source, self.recorder.feed_audio)
        self._audio_source = source
        self.recorder.wants_audio = bool(source)
        if source:
            self._hub.subscribe(source, self.recorder.feed_audio)

    def _release(self, cap: cv2.VideoCapture | None) -> None:
        if cap is not None:
            cap.release()
        if self._ctl:
            self._ctl.close()
            self._ctl = None
        self.connected = False
        self.device = None
        self.measured_fps = 0.0
        with self._cond:
            self._jpeg = None       # don't serve a stale frame from a gone camera

    def _publish(self, frame: np.ndarray, quality: int) -> None:
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            return
        with self._cond:
            self._jpeg = buf.tobytes()
            self._seq += 1
            self._cond.notify_all()

    def _adjust(self, frame: np.ndarray) -> np.ndarray:
        """Software rotation and flips from settings."""
        s = self.camera.settings
        if s.rotation == 90:
            frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
        elif s.rotation == 180:
            frame = cv2.rotate(frame, cv2.ROTATE_180)
        elif s.rotation == 270:
            frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
        if s.flip_horizontal and s.flip_vertical:
            frame = cv2.flip(frame, -1)
        elif s.flip_horizontal:
            frame = cv2.flip(frame, 1)
        elif s.flip_vertical:
            frame = cv2.flip(frame, 0)
        return frame
