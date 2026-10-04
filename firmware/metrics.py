"""System performance sampling: CPU, memory, disk and GPU, every few seconds, kept for
about a day and persisted so the history survives a restart.

Independent of the cameras: it runs the same with none attached. Formats follow the
camera1 firmware so both performance pages read alike.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

SAMPLE_INTERVAL = 10                 # seconds
RETENTION_SECONDS = 25 * 3600        # a bit over the 24 h the page shows
PRUNE_EVERY = 600


# --- CPU, memory ----------------------------------------------------------------------

def _read_cpu_times() -> tuple[int, int]:
    """(total, idle) jiffies of all cores together, from the aggregate line of /proc/stat."""
    with open("/proc/stat") as f:
        parts = [int(x) for x in f.readline().split()[1:]]
    return sum(parts), parts[3] + parts[4]          # idle + iowait


def _sample_mem() -> dict:
    info: dict[str, int] = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, _, rest = line.partition(":")
            try:
                info[key] = int(rest.split()[0])    # kB
            except (ValueError, IndexError):
                continue
    total, avail = info.get("MemTotal", 0), info.get("MemAvailable", 0)
    used = max(0, total - avail)
    return {"mem_pct": round(100 * used / total, 1) if total else None,
            "mem_used_gb": round(used / 1024 ** 2, 2),
            "mem_total_gb": round(total / 1024 ** 2, 2)}


# --- disks ---------------------------------------------------------------------------

def _device_for_path(path: Path) -> str | None:
    """Name /proc/diskstats uses for the block device holding `path` ("sda1", "nvme0n1p2",
    "dm-0" for an LVM volume), or None for network/virtual filesystems. Uses the mount with
    the longest matching prefix, since the storage folder is usually a subdirectory."""
    try:
        real = os.path.realpath(path)
        best, source = "", None
        with open("/proc/mounts") as f:
            for line in f:
                fields = line.split()
                if len(fields) < 2:
                    continue
                mount = fields[1].replace("\\040", " ")
                if (real == mount or real.startswith(mount.rstrip("/") + "/")) and len(mount) > len(best):
                    best, source = mount, fields[0]
        if source and source.startswith("/dev/"):
            return os.path.basename(os.path.realpath(source))
    except OSError:
        pass
    return None


def _read_diskstats() -> dict[str, int]:
    """{device: io_ticks_ms}: milliseconds spent doing I/O, the counter `iostat -x %util`
    divides by elapsed time."""
    stats: dict[str, int] = {}
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                fields = line.split()
                if len(fields) >= 14:
                    stats[fields[2]] = int(fields[12])
    except OSError:
        pass
    return stats


def _disk_used_pct(path: Path) -> float | None:
    try:
        probe = Path(path)
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        usage = shutil.disk_usage(probe)
        return round(100 * usage.used / usage.total, 1) if usage.total else None
    except OSError:
        return None


# --- GPU -----------------------------------------------------------------------------

def _gpu_nvidia() -> dict | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,utilization.encoder,utilization.decoder,power.draw",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5).stdout
        gpu, enc, dec, power = [float(x) for x in out.splitlines()[0].split(",")]
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None
    return {"gpu_pct": round(gpu, 1), "gpu_video_pct": round(max(enc, dec), 1),
            "gpu_power_w": round(power, 1)}


def _gpu_intel() -> dict | None:
    """One sample from intel_gpu_top. It needs root (a sudoers rule limited to that one
    binary, used through `sudo -n` so it can never block on a password) and has no
    single-shot mode, so run it briefly and take the last complete CSV row."""
    try:
        text = subprocess.run(["sudo", "-n", "intel_gpu_top", "-c", "-s", "500"],
                              capture_output=True, text=True, timeout=1.8).stdout
    except subprocess.TimeoutExpired as e:
        text = e.stdout or ""
        if isinstance(text, bytes):                 # CPython hands back raw bytes here
            text = text.decode(errors="replace")
    except Exception:
        return None
    lines = [l for l in text.strip().splitlines() if l.strip()]
    if len(lines) < 2:
        return None
    header = lines[0].split(",")
    row = next((dict(zip(header, c.split(","))) for c in reversed(lines[1:])
                if len(c.split(",")) == len(header)), None)
    if row is None:
        return None
    try:
        return {"gpu_pct": round(float(row.get("RCS %", 0.0)), 1),
                "gpu_video_pct": round(float(row.get("VCS %", 0.0)), 1),
                "gpu_power_w": round(float(row.get("Power W gpu", 0.0)), 1)}
    except (TypeError, ValueError):
        return None


def detect_gpu_sampler() -> Callable[[], dict | None] | None:
    """The first GPU tool that actually returns data here (AMD is not supported)."""
    for tool, sampler in (("nvidia-smi", _gpu_nvidia), ("intel_gpu_top", _gpu_intel)):
        if shutil.which(tool) and sampler():
            return sampler
    return None


# --- the sampler -----------------------------------------------------------------------

class Metrics:
    def __init__(self, path: str | os.PathLike, storage_root: Callable[[], Path],
                 interval: float = SAMPLE_INTERVAL) -> None:
        self.path = Path(path)
        self._storage_root = storage_root           # the recordings disk follows the setting
        self.interval = interval
        self._samples: collections.deque[dict] = collections.deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._gpu = detect_gpu_sampler()
        self._thread = threading.Thread(target=self._run, name="metrics", daemon=True)
        self._load()

    @property
    def has_gpu(self) -> bool:
        return self._gpu is not None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def history(self, since: float | None = None) -> list[dict]:
        with self._lock:
            return [s for s in self._samples if since is None or s.get("ts", 0) > since]

    # -- persistence -------------------------------------------------------------------

    def _load(self) -> None:
        cutoff = time.time() - RETENTION_SECONDS
        try:
            with open(self.path) as f:
                for line in f:
                    try:
                        s = json.loads(line)
                    except ValueError:
                        continue
                    if s.get("ts", 0) >= cutoff:
                        self._samples.append(s)
        except FileNotFoundError:
            pass

    def _append(self, sample: dict) -> None:
        with self._lock:
            self._samples.append(sample)
        try:
            with open(self.path, "a") as f:
                f.write(json.dumps(sample) + "\n")
        except OSError:
            pass

    def _prune(self) -> None:
        cutoff = time.time() - RETENTION_SECONDS
        with self._lock:
            while self._samples and self._samples[0].get("ts", 0) < cutoff:
                self._samples.popleft()
            keep = list(self._samples)
        tmp = self.path.with_suffix(".tmp")
        try:
            with open(tmp, "w") as f:
                for s in keep:
                    f.write(json.dumps(s) + "\n")
            os.replace(tmp, self.path)
        except OSError:
            pass

    # -- sampling ------------------------------------------------------------------------

    def _run(self) -> None:
        prev_total, prev_idle = _read_cpu_times()
        prev_disk = _read_diskstats()
        prev_wall = time.time()
        last_prune = time.time()
        while not self._stop.wait(self.interval):
            now = time.time()
            try:
                elapsed = now - prev_wall
                total, idle = _read_cpu_times()
                d_total, d_idle = total - prev_total, idle - prev_idle
                cpu = round(100 * (1 - d_idle / d_total), 1) if d_total > 0 else None
                prev_total, prev_idle = total, idle

                disk_now = _read_diskstats()
                disk = {}
                for label, path in (("root", Path("/")), ("recordings", self._storage_root())):
                    entry: dict = {"used_pct": _disk_used_pct(path), "busy_pct": None}
                    dev = _device_for_path(path)
                    if dev and elapsed > 0 and dev in disk_now and dev in prev_disk:
                        ticks = disk_now[dev] - prev_disk[dev]
                        entry["busy_pct"] = round(min(100.0, 100 * ticks / (elapsed * 1000)), 1)
                    disk[label] = entry
                prev_disk, prev_wall = disk_now, now

                sample = {"ts": now, "cpu_pct": cpu, "disk": disk, **_sample_mem()}
                if self._gpu:
                    sample.update(self._gpu() or {})
                self._append(sample)
                if now - last_prune > PRUNE_EVERY:
                    self._prune()
                    last_prune = now
            except Exception:
                # one bad tick must never end the logging thread, silently, for good
                log.exception("metrics sample failed")
