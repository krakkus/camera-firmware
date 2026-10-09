"""H.264 copy mode, for cameras that can deliver H.264 themselves.

One ffmpeg process owns the camera and does two things with the camera's H.264 stream:
  - copies it, without encoding, into a ring of short .ts segments on disk, and
  - decodes it, scaled down and slowed to PREVIEW_FPS, into raw frames on a pipe. Those
    frames take the place of the OpenCV capture: live view, snapshots, motion and person
    detection and the image controls all work on them.

RingClips then joins ring segments into the mp4 files in the camera's recording folder.
A recording is always a range of ring segments, so continuous, motion and person
recording share one mechanism and the camera never has to be reopened to change mode.

The recordings are the camera's own pictures: no rotation, flips or audio, and the
quality is whatever the camera's encoder gives (record_crf does not apply).
"""
from __future__ import annotations

import logging
import re
import signal
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SEGMENT_SECONDS = 2         # ring segments are cut at the first keyframe after this
PREVIEW_WIDTH = 960         # frames given to the rest of the firmware: at most this wide
PREVIEW_FPS = 10            # ... and at most this many per second
READ_TIMEOUT = 5.0          # no frame for this long: the capture counts as failed
FINALIZE_WAIT = 10.0        # how long a finished clip waits for its last segment to close
TIDY_INTERVAL = 5.0
NAME_FORMAT = "%Y%m%d_%H%M%S"
SEGMENT = re.compile(r"^(\d{8}_\d{6})\.ts$")


def preview_size(width: int, height: int) -> tuple[int, int]:
    """(width, height) of the frames read() returns: even numbers, same aspect."""
    w = min(width, PREVIEW_WIDTH) // 2 * 2
    return w, max(2, round(height * w / width / 2) * 2)


class H264Capture:
    """Stands in for cv2.VideoCapture: read() and release()."""

    def __init__(self, node: str, width: int, height: int, fps: int, ring: Path,
                 source: list[str] | None = None) -> None:
        """source: ffmpeg input arguments instead of the camera (for tests)."""
        self.size = preview_size(width, height)
        w, h = self.size
        self._bytes = w * h * 3
        self._ring = str(ring).replace("%", "%%")
        source = source or ["-f", "v4l2", "-input_format", "h264", "-video_size", f"{width}x{height}",
                            "-framerate", str(fps), "-use_wallclock_as_timestamps", "1", "-i", node]
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", *source,
               # the camera's stream, copied as it is
               "-map", "0:v", "-c:v", "copy", "-f", "segment",
               "-segment_time", str(SEGMENT_SECONDS), "-segment_format", "mpegts",
               "-reset_timestamps", "1", "-strftime", "1",
               f"{self._ring}/{NAME_FORMAT}.ts",
               # and a small decoded copy for looking at
               "-map", "0:v", "-vf", f"fps={min(fps, PREVIEW_FPS)},scale={w}:{h}",
               "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"]
        self._err = tempfile.TemporaryFile()
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=self._err,
                                      stdin=subprocess.DEVNULL, bufsize=0)
        self._cond = threading.Condition()
        self._frame: np.ndarray | None = None
        self._seq = self._read_seq = 0
        self._dead = False
        self._thread = threading.Thread(target=self._pump, name="h264-frames", daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        out = self._proc.stdout
        while True:
            buf = bytearray()
            while len(buf) < self._bytes:
                chunk = out.read(self._bytes - len(buf))
                if not chunk:
                    with self._cond:
                        self._dead = True
                        self._cond.notify_all()
                    return
                buf += chunk
            frame = np.frombuffer(bytes(buf), np.uint8).reshape(self.size[1], self.size[0], 3)
            with self._cond:
                self._frame, self._seq = frame, self._seq + 1
                self._cond.notify_all()

    def isOpened(self) -> bool:
        return self._proc.poll() is None

    def read(self) -> tuple[bool, np.ndarray | None]:
        with self._cond:
            self._cond.wait_for(lambda: self._seq != self._read_seq or self._dead,
                                timeout=READ_TIMEOUT)
            if self._seq != self._read_seq:
                self._read_seq = self._seq
                return True, self._frame.copy()
        return False, None

    def release(self) -> None:
        """Stop ffmpeg politely (SIGINT), so the last ring segment is closed properly."""
        if self._proc.poll() is None:
            self._proc.send_signal(signal.SIGINT)
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        self._thread.join(timeout=2)
        if self._err:
            self._err.seek(0)
            msg = self._err.read().decode(errors="replace").strip()
            self._err.close()
            self._err = None
            if msg:
                log.warning("ffmpeg (H.264 copy) said: %s", msg[-600:])


def _segments(ring: Path) -> list[tuple[datetime, Path]]:
    found = []
    try:
        for f in ring.iterdir():
            m = SEGMENT.match(f.name)
            if m:
                found.append((datetime.strptime(m.group(1), NAME_FORMAT), f))
    except OSError:
        pass
    return sorted(found)


class _Clip:
    def __init__(self, start: datetime, suffix: str, began: float) -> None:
        self.start, self.suffix, self.began = start, suffix, began


class RingClips:
    """Joins ring segments into recordings. update() says whether footage is wanted now."""

    def __init__(self, ring: Path, clips: Path, max_seconds: float) -> None:
        self.ring, self.clips, self.max_seconds = ring, clips, max_seconds
        self._clip: _Clip | None = None
        self._consumed = datetime.min       # footage up to here is in a recording already
        self._lock = threading.Lock()       # one assembly at a time, in order
        self._workers: list[threading.Thread] = []
        self._holds: list[datetime] = []    # starts of clips being joined: their segments stay
        self._last_tidy = 0.0

    @property
    def recording(self) -> bool:
        return self._clip is not None

    def recover(self) -> None:
        """Make recordings of segments left behind by a crash or a power cut."""
        group: list[tuple[datetime, Path]] = []
        for seg in _segments(self.ring) + [(datetime.max, Path())]:
            if group and (seg[0] - group[-1][0]).total_seconds() > 10 * SEGMENT_SECONDS:
                self._join(group, group[0][0], "", "recovered")
                group = []
            group.append(seg)

    def update(self, wanted: bool, suffix: str, pre_roll: float, now: float) -> None:
        clip = self._clip
        if wanted and clip is not None and now - clip.began >= self.max_seconds:
            self._finish(clip)                       # file length reached: next one follows on
            clip = self._clip = None
        if wanted and clip is None:
            start = datetime.now() - timedelta(seconds=pre_roll)
            self._clip = _Clip(start, suffix, now)
            log.info("recording clip from %s", start.strftime("%H:%M:%S"))
        elif not wanted and clip is not None:
            self._finish(clip)
            self._clip = None
        if now - self._last_tidy >= TIDY_INTERVAL:
            self._last_tidy = now
            self._tidy(pre_roll)

    def close(self) -> None:
        """End the current clip and wait until every recording is finished."""
        clip, self._clip = self._clip, None
        if clip is not None:
            self._finish(clip)
        for t in self._workers:
            t.join(timeout=FINALIZE_WAIT + 30)
        self._workers = []

    # -- internals -----------------------------------------------------

    def _finish(self, clip: _Clip) -> None:
        end = datetime.now()
        self._holds.append(clip.start)
        t = threading.Thread(target=self._assemble, args=(clip, end), name="clip", daemon=True)
        t.start()
        self._workers = [w for w in self._workers if w.is_alive()] + [t]

    def _assemble(self, clip: _Clip, end: datetime) -> None:
        try:
            self._assemble_locked(clip, end)
        finally:
            self._holds.remove(clip.start)

    def _assemble_locked(self, clip: _Clip, end: datetime) -> None:
        with self._lock:
            deadline = time.monotonic() + FINALIZE_WAIT
            while time.monotonic() < deadline:      # the segment holding `end` closes when the next opens
                segs = _segments(self.ring)
                if segs and segs[-1][0] > end:
                    break
                time.sleep(0.25)
            segs = [s for s in _segments(self.ring) if s[0] > self._consumed]
            before = [s for s in segs if s[0] <= clip.start]
            first = before[-1][0] if before else (segs[0][0] if segs else None)
            chosen = [s for s in segs if first is not None and first <= s[0] <= end]
            if not chosen:
                log.warning("clip from %s: no footage in the ring", clip.start)
                return
            self._join(chosen, chosen[0][0], clip.suffix, "clip")

    def _join(self, segs: list[tuple[datetime, Path]], start: datetime, suffix: str, what: str) -> None:
        segs = [s for s in segs if s[1].name and s[1].exists()]
        if not segs:
            return
        out = self.clips / f"{start.strftime(NAME_FORMAT)}{suffix}.mp4"
        while out.exists():                         # two clips in the same second
            start += timedelta(seconds=1)
            out = self.clips / f"{start.strftime(NAME_FORMAT)}{suffix}.mp4"
        tmp = out.with_suffix(".mp4.part")
        listing = self.ring / "join.txt"
        listing.write_text("".join(f"file '{p.name}'\n" for _, p in segs))
        try:
            r = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                 "-i", str(listing), "-c", "copy",
                 "-movflags", "+frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", str(tmp)],
                capture_output=True, timeout=600)
            if r.returncode or not tmp.exists() or tmp.stat().st_size == 0:
                log.error("joining %s failed: %s", what, r.stderr.decode(errors="replace")[-400:])
                tmp.unlink(missing_ok=True)
                return
            tmp.replace(out)
        except (OSError, subprocess.SubprocessError) as e:
            log.error("joining %s failed: %s", what, e)
            return
        finally:
            listing.unlink(missing_ok=True)
        self._consumed = max(self._consumed, segs[-1][0])
        log.debug("%s uses segments %s .. %s", out.name, segs[0][1].name, segs[-1][1].name)
        for _, p in segs:
            p.unlink(missing_ok=True)
        log.info("closed %s (%d segments)", out, len(segs))

    def _tidy(self, pre_roll: float) -> None:
        """Drop ring segments nothing needs any more: older than the pre-roll plus a margin,
        and older than the start of the clip being recorded."""
        horizon = datetime.now() - timedelta(seconds=pre_roll + 4 * SEGMENT_SECONDS + 5)
        wanted = [c.start for c in (self._clip,) if c is not None] + list(self._holds)
        if wanted:
            horizon = min(horizon, min(wanted) - timedelta(seconds=4 * SEGMENT_SECONDS))
        for start, p in _segments(self.ring)[:-2]:   # never the newest, still being written
            if start < horizon:
                p.unlink(missing_ok=True)
