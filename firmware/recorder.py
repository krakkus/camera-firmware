"""Segmented H.264/AAC mp4 recording through ffmpeg, continuous or motion-triggered."""
from __future__ import annotations

import collections
import logging
import os
import queue
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from .audio import RATE, SAMPLE_BYTES
from .camera import CameraSettings
from .camera_server import DeviceConfig
from .objects import ObjectWatcher, PersonDetector

log = logging.getLogger(__name__)

AUDIO_STARVE_TIMEOUT = 1.0     # feed ffmpeg silence if the mic stops delivering for this long
AUDIO_FRESH_WITHIN = 2.0       # a mic counts as working if it delivered a chunk this recently


class MotionDetector:
    """Frame differencing against a slowly-adapting background."""

    ANALYSIS_WIDTH = 320

    def __init__(self) -> None:
        self._background: np.ndarray | None = None

    def detect(self, frame: np.ndarray, s: CameraSettings) -> bool:
        h, w = frame.shape[:2]
        small = cv2.resize(frame, (self.ANALYSIS_WIDTH, max(1, h * self.ANALYSIS_WIDTH // w)))
        gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (15, 15), 0)
        if self._background is None or self._background.shape != gray.shape:
            self._background = gray.astype("float32")
            return False
        diff = cv2.absdiff(gray, cv2.convertScaleAbs(self._background))
        cv2.accumulateWeighted(gray, self._background, 0.05)
        changed = cv2.countNonZero(cv2.threshold(diff, s.motion_threshold, 255, cv2.THRESH_BINARY)[1])
        return changed >= s.motion_min_area


class Encoder:
    """One ffmpeg process fed by us; `output` is everything after its inputs (codecs,
    format, destination). Used for recording segments and for live RTSP streams.

    Video: raw BGR frames on stdin, resampled here to constant fps from their capture
    times (webcams deliver irregular rates). Audio: raw PCM on an extra pipe, aligned to
    the first video frame's time.
    """

    def __init__(self, name: str, size: tuple[int, int] | None, fps: int, t0: float,
                 audio: bool, output: list[str]) -> None:
        """size None means audio only (audio must be True then)."""
        self.name, self.size, self.fps, self.t0 = name, size, fps, t0
        self.has_audio = audio
        self.failed = False
        self._next_idx = 0
        self._last: bytes | None = None
        self._audio_aligned = False
        self._aq: queue.Queue[bytes | None] = queue.Queue()
        self._athread: threading.Thread | None = None

        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        if size is not None:
            h, w = size
            cmd += ["-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}",
                    "-framerate", str(fps), "-i", "pipe:0"]
        pass_fds: tuple[int, ...] = ()
        audio_w = -1
        if audio:
            audio_r, audio_w = os.pipe()
            pass_fds = (audio_r,)
            cmd += ["-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", f"pipe:{audio_r}"]
        cmd += output

        self._err = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE if size is not None else subprocess.DEVNULL,
            stderr=self._err, pass_fds=pass_fds)
        if audio:
            os.close(audio_r)       # the child has its own copy
            self._athread = threading.Thread(target=self._audio_loop, args=(audio_w,),
                                             name=f"{name}-audio", daemon=True)
            self._athread.start()

    def write_video(self, frame: np.ndarray, t: float) -> None:
        if self.failed:
            return
        idx = round((t - self.t0) * self.fps)
        if idx < self._next_idx:
            return                              # camera ran faster than output fps: drop
        data = frame.tobytes()
        try:
            if self._last is not None:
                for _ in range(idx - self._next_idx):   # camera stalled: hold last frame
                    self.proc.stdin.write(self._last)
            self.proc.stdin.write(data)
        except (BrokenPipeError, ValueError):
            self.failed = True
            return
        self._last, self._next_idx = data, idx + 1

    def write_audio(self, t_start: float, pcm: bytes) -> None:
        """Never blocks: a writer thread drains the queue into the pipe."""
        if not self.has_audio:
            return
        if not self._audio_aligned:
            offset = round((t_start - self.t0) * RATE)      # samples
            if offset > 0:
                pcm = bytes(offset * SAMPLE_BYTES) + pcm    # audio starts after video: pad
            elif offset < 0:
                pcm = pcm[-offset * SAMPLE_BYTES:]          # audio starts before video: trim
                if not pcm:
                    return                                  # still before t0; try next chunk
            self._audio_aligned = True
        self._aq.put(pcm)

    def close(self) -> bool:
        """Finish: ffmpeg flushes and exits. Blocks meanwhile; call from a helper thread.
        Returns False if ffmpeg reported a problem (already logged)."""
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except (BrokenPipeError, ValueError, OSError):
            pass
        if self._athread:
            self._aq.put(None)
            self._athread.join(timeout=5)
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self._err.seek(0)
        msg = self._err.read().decode(errors="replace").strip()
        self._err.close()
        if self.proc.returncode or msg:
            log.warning("ffmpeg for %s exited %s: %s", self.name, self.proc.returncode, msg)
            return False
        return True

    def _audio_loop(self, fd: int) -> None:
        silence = bytes(int(RATE * AUDIO_STARVE_TIMEOUT) * SAMPLE_BYTES)
        with os.fdopen(fd, "wb", buffering=0) as pipe:
            try:
                while True:
                    try:
                        item = self._aq.get(timeout=AUDIO_STARVE_TIMEOUT)
                    except queue.Empty:
                        item = silence      # keep ffmpeg from stalling the video input
                    if item is None:
                        return
                    pipe.write(item)
            except BrokenPipeError:
                self.failed = True


class Segment(Encoder):
    """One recording file. Output is fragmented mp4, so a crash loses seconds, not the
    whole file."""

    def __init__(self, path: Path, size: tuple[int, int] | None, fps: int, t0: float,
                 audio: bool, crf: int) -> None:
        self.path = path
        out: list[str] = []
        if size is not None:
            out += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                    "-pix_fmt", "yuv420p", "-g", str(fps * 2)]
        if audio:
            out += ["-c:a", "aac", "-b:a", "96k"]
        if size is not None:
            out += ["-movflags", "+frag_keyframe+empty_moov+default_base_moof"]
        else:   # every audio packet is a keyframe, so fragment by time instead
            out += ["-f", "mp4", "-frag_duration", "2000000",
                    "-movflags", "+empty_moov+default_base_moof"]
        out.append(str(path))
        super().__init__(path.name, size, fps, t0, audio, out)

    def close(self) -> bool:
        ok = super().close()
        if ok:
            log.info("closed %s", self.path)
        return ok


class Recorder:
    """Feed it every frame (process) and every audio chunk (feed_audio); it decides
    what to write according to the settings.

    Files: <dir>/<camera_id>/<YYYYmmdd_HHMMSS>[_motion|_object].mp4 (.m4a if audio only)
    """

    def __init__(self, camera_id: str, get_settings: Callable[[], CameraSettings],
                 config: DeviceConfig, detector: PersonDetector) -> None:
        self._camera_id = camera_id
        self._config = config               # global: storage location, maximum file length
        self.error: str | None = None       # why recording can't start (e.g. storage not writable)
        self._error_logged = 0.0
        self._watcher = ObjectWatcher(detector)
        self.objects: list[str] = []        # classes detected right now (object mode)
        self._get_settings = get_settings   # audio-only recording has no frames to carry them
        self._audio_only = False
        self.wants_audio = False            # camera has an audio source (set by the worker)
        self._detector = MotionDetector()
        self._seg: Segment | None = None
        self._segment_start = 0.0
        self._last_motion = 0.0
        self._pre_roll: collections.deque[tuple[float, np.ndarray]] = collections.deque()
        # audio ring covers pre-roll, plus the gap between segments when rotating
        self._lock = threading.RLock()
        self._audio_ring: collections.deque[tuple[float, bytes]] = collections.deque()
        self._audio_keep = 1.0
        self._last_audio = 0.0
        self.motion = False

    @property
    def dir(self) -> Path:
        return Path(self._config.storage_dir).expanduser().resolve() / self._camera_id

    def _ensure_dir(self) -> bool:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.error = f"storage: cannot create {self.dir}: {e.strerror or e}"
            if time.monotonic() - self._error_logged > 30:     # don't flood the log
                log.error(self.error)
                self._error_logged = time.monotonic()
            return False
        self.error = None
        return True

    @property
    def recording(self) -> bool:
        return self._seg is not None

    # -- audio thread --------------------------------------------------

    def set_audio_only(self, flag: bool) -> None:
        """Audio-only cameras record straight from the audio chunks, with no frames."""
        with self._lock:
            if flag != self._audio_only:
                self._close()
                self._audio_ring.clear()
                self._audio_only = flag

    def feed_audio(self, t_start: float, pcm: bytes) -> None:
        with self._lock:
            self._last_audio = time.monotonic()
            if self._audio_only:
                self._feed_audio_only(t_start, pcm)
                return
            if self._seg is not None:
                self._seg.write_audio(t_start, pcm)
                return
            self._audio_ring.append((t_start, pcm))
            while self._audio_ring and t_start - self._audio_ring[0][0] > self._audio_keep:
                self._audio_ring.popleft()

    def _feed_audio_only(self, t: float, pcm: bytes) -> None:
        s = self._get_settings()
        if s.record_mode == "off":      # "motion" can't apply without video: record continuously
            self._close()
            return
        seg = self._seg
        if seg is not None and (t - self._segment_start >= self._max_segment() or seg.failed
                                or seg.path.parent != self.dir):   # storage location changed
            self._close()
            seg = None
        if seg is None:
            if not self._ensure_dir():
                return
            path = self.dir / f"{datetime.now():%Y%m%d_%H%M%S}.m4a"
            try:
                seg = Segment(path, None, 0, t, True, s.record_crf)
            except OSError as e:
                log.error("cannot start ffmpeg: %s", e)
                return
            self._seg, self._segment_start = seg, t
            log.info("recording audio to %s", path)
        seg.write_audio(t, pcm)

    # -- capture thread ------------------------------------------------

    def process(self, frame: np.ndarray, s: CameraSettings, fps: float) -> None:
        now = time.monotonic()
        mode = s.record_mode

        if mode in ("off", "continuous", "motion"):
            self.objects = []
            self._watcher.reset()

        if mode == "off":
            self._close()
            self._pre_roll.clear()
            self.motion = False
            return

        if mode == "continuous":
            self._audio_keep = 1.0
            self._pre_roll.clear()
            self.motion = False
            self._write(now, frame, s, fps, "", [])
            return

        # motion / object mode: something has to trigger the recording
        self._audio_keep = max(1.0, float(s.pre_roll_seconds))
        if mode == "motion":
            self.motion = self._detector.detect(frame, s)
            self.objects = []
            trigger, suffix = self.motion, "_motion"
        else:
            self.motion = False
            self._watcher.submit(frame, s.object_confidence)
            self.objects = self._watcher.current()
            trigger, suffix = bool(self.objects), "_object"
        if trigger:
            self._last_motion = now
        active = self._seg is not None
        triggered = trigger or (active and now - self._last_motion <= s.post_roll_seconds)

        if triggered:
            pre = [] if active else list(self._pre_roll)
            self._pre_roll.clear()
            self._write(now, frame, s, fps, suffix, pre)
        else:
            self._close()
            self._pre_roll.append((now, frame))
            while self._pre_roll and now - self._pre_roll[0][0] > s.pre_roll_seconds:
                self._pre_roll.popleft()

    def close(self) -> None:
        """Stop recording and wait until the file is finalized."""
        closer = self._close()
        if closer:
            closer.join(timeout=30)

    def stop(self) -> None:
        """close(), and end the object detection thread too (for shutdown)."""
        self._watcher.close()
        self.close()

    def _max_segment(self) -> float:
        return self._config.segment_minutes * 60

    # -- internals -----------------------------------------------------

    def _write(self, now: float, frame: np.ndarray, s: CameraSettings, fps: float,
               suffix: str, pre: list[tuple[float, np.ndarray]]) -> None:
        seg = self._seg
        if seg is not None and (now - self._segment_start >= self._max_segment()
                                or frame.shape[:2] != seg.size or seg.failed
                                or seg.path.parent != self.dir):   # storage location changed
            self._close()
        if self._seg is None:
            t0 = pre[0][0] if pre else now
            if not self._open(frame, s, fps, suffix, t0):
                return
            for t, old in pre:
                self._seg.write_video(old, t)
        self._seg.write_video(frame, now)

    def _open(self, frame: np.ndarray, s: CameraSettings, fps: float,
              suffix: str, t0: float) -> bool:
        if not self._ensure_dir():
            return False
        path = self.dir / f"{datetime.now():%Y%m%d_%H%M%S}{suffix}.mp4"
        fps_out = max(1, round(fps))
        audio = time.monotonic() - self._last_audio < AUDIO_FRESH_WITHIN
        if self.wants_audio and not audio:
            log.warning("camera has an audio source but the microphone is not delivering; "
                        "recording without audio")
        try:
            with self._lock:
                seg = Segment(path, frame.shape[:2], fps_out, t0, audio, s.record_crf)
                self._seg = seg
                for ts, pcm in self._audio_ring:
                    seg.write_audio(ts, pcm)
                self._audio_ring.clear()
        except OSError as e:
            log.error("cannot start ffmpeg: %s", e)
            return False
        self._segment_start = t0
        log.info("recording to %s (%d fps%s)", path, fps_out, ", audio" if audio else "")
        return True

    def _close(self) -> threading.Thread | None:
        with self._lock:
            seg, self._seg = self._seg, None
        if seg is None:
            return None
        # finalizing can take a moment; don't stall capture meanwhile
        t = threading.Thread(target=seg.close, name="segment-close", daemon=True)
        t.start()
        return t
