"""Microphone capture via ffmpeg (ALSA), shared between cameras by AudioHub."""
from __future__ import annotations

import logging
import subprocess
import tempfile
import threading
import time
from typing import Callable

from .devices import resolve_alsa

log = logging.getLogger(__name__)

RATE = 48000
SAMPLE_BYTES = 2                        # s16le, mono
CHUNK_SECONDS = 0.02
CHUNK_BYTES = int(RATE * CHUNK_SECONDS) * SAMPLE_BYTES
RESTART_DELAY = 3.0                     # also the hot-plug polling interval

ChunkCallback = Callable[[float, bytes], None]


class AudioCapture:
    """Captures one audio source and calls on_chunk(t_start, pcm) every 20 ms, with
    t_start on the time.monotonic() clock (same as video frames).

    The device is resolved again on every (re)start, so an unplugged microphone is
    waited for and picked up wherever it reappears."""

    def __init__(self, source: str, on_chunk: ChunkCallback) -> None:
        self.source = source
        self._on_chunk = on_chunk
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._thread = threading.Thread(target=self._run, name=f"audio-{source[-24:]}",
                                        daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None:
            proc.kill()          # unblocks the reader
        self._thread.join(timeout=5)

    def _run(self) -> None:
        waiting_logged = False
        while not self._stop.is_set():
            device = resolve_alsa(self.source)
            if device is None:
                if not waiting_logged:
                    log.warning("audio: %s not plugged in, waiting", self.source)
                    waiting_logged = True
                self._stop.wait(RESTART_DELAY)
                continue
            waiting_logged = False
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
                   "-f", "alsa", "-ac", "1", "-ar", str(RATE), "-i", device,
                   "-f", "s16le", "-ac", "1", "-ar", str(RATE), "pipe:1"]
            err = tempfile.TemporaryFile()
            proc = self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
            log.info("audio: capturing from %s", device)
            while not self._stop.is_set():
                data = proc.stdout.read(CHUNK_BYTES)
                if len(data) < CHUNK_BYTES:
                    break
                self._on_chunk(time.monotonic() - CHUNK_SECONDS, data)
            proc.kill()
            proc.wait()
            if not self._stop.is_set():
                err.seek(0)
                log.warning("audio: capture from %s stopped: %s", device,
                            err.read().decode(errors="replace").strip() or "no output")
            err.close()
            self._stop.wait(RESTART_DELAY)


class AudioHub:
    """Opens each audio source once, however many cameras use it, and only while
    at least one camera is subscribed."""

    class _Entry:
        def __init__(self) -> None:
            self.subscribers: list[ChunkCallback] = []
            self.capture: AudioCapture | None = None

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, AudioHub._Entry] = {}

    def subscribe(self, source: str, callback: ChunkCallback) -> None:
        with self._lock:
            entry = self._entries.get(source)
            if entry is None:
                entry = self._entries[source] = self._Entry()
                entry.capture = AudioCapture(
                    source, lambda t, d, e=entry: self._dispatch(e, t, d))
                entry.capture.start()
            entry.subscribers.append(callback)

    def unsubscribe(self, source: str, callback: ChunkCallback) -> None:
        with self._lock:
            entry = self._entries.get(source)
            if entry is None or callback not in entry.subscribers:
                return
            entry.subscribers.remove(callback)
            if entry.subscribers:
                return
            del self._entries[source]
        entry.capture.stop()            # outside the lock: joining can take a moment

    def stop(self) -> None:
        with self._lock:
            entries, self._entries = list(self._entries.values()), {}
        for e in entries:
            e.capture.stop()

    @staticmethod
    def _dispatch(entry: "AudioHub._Entry", t: float, data: bytes) -> None:
        for cb in list(entry.subscribers):
            cb(t, data)
