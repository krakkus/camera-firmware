"""Where recordings live: usage, choosing a location, and pruning when the disk fills."""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .camera_server import DEFAULT_STORAGE_DIR, DeviceConfig
from .recordings import NAME

log = logging.getLogger(__name__)

PRUNE_MARGIN = 2.0          # delete down to min_free_percent + this, so we don't prune every minute
RECENT_SECONDS = 120        # files modified this recently may still be written to: never delete
PSEUDO_FS = {"proc", "sysfs", "tmpfs", "devtmpfs", "devpts", "cgroup", "cgroup2", "overlay",
             "squashfs", "efivarfs", "securityfs", "debugfs", "tracefs", "configfs", "fusectl",
             "mqueue", "hugetlbfs", "pstore", "bpf", "autofs", "binfmt_misc", "ramfs", "nsfs",
             "fuse.portal", "fuse.gvfsd-fuse"}


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


@dataclass
class PruneResult:
    at: float = 0.0
    deleted: int = 0
    freed: int = 0
    free_percent: float = 0.0
    note: str = ""              # e.g. why nothing was deleted

    def to_dict(self) -> dict:
        return {"at": datetime.fromtimestamp(self.at).strftime("%Y-%m-%d %H:%M:%S") if self.at else None,
                "deleted": self.deleted, "freed": self.freed, "freed_text": human(self.freed),
                "free_percent": round(self.free_percent, 1), "note": self.note}


class Storage:
    def __init__(self, config: DeviceConfig) -> None:
        self._cfg = config
        self._lock = threading.Lock()           # one prune at a time
        self.last_prune = PruneResult()

    @property
    def root(self) -> Path:
        return Path(self._cfg.storage_dir).expanduser().resolve()

    # -- information ----------------------------------------------------------

    def usage(self) -> dict:
        root = self.root
        info: dict = {"path": str(root), "min_free_percent": self._cfg.min_free_percent,
                      "prune_enabled": self._cfg.prune_enabled,
                      "last_prune": self.last_prune.to_dict(), "error": None}
        try:
            probe = root
            while not probe.exists() and probe != probe.parent:    # not created yet: use its parent disk
                probe = probe.parent
            du = shutil.disk_usage(probe)
        except OSError as e:
            return {**info, "error": str(e)}
        recs = self._recordings()
        info.update(
            total=du.total, used=du.used, free=du.free,
            free_percent=round(100 * du.free / du.total, 1) if du.total else 0.0,
            total_text=human(du.total), free_text=human(du.free),
            recordings=len(recs), recordings_bytes=sum(r[2] for r in recs),
            recordings_text=human(sum(r[2] for r in recs)),
            writable=os.access(probe, os.W_OK))
        if not root.exists():
            info["note"] = "folder will be created when the first recording starts"
        return info

    def candidates(self) -> list[dict]:
        """Places a user can pick: the configured one, the default, and writable mounts."""
        paths: dict[str, str] = {}
        for p in (str(self.root), str(Path(DEFAULT_STORAGE_DIR).resolve())):
            paths[p] = ""
        try:
            for line in Path("/proc/mounts").read_text().splitlines():
                _dev, mount, fstype = line.split()[:3]
                mount = mount.replace("\\040", " ")
                if fstype in PSEUDO_FS or not os.access(mount, os.W_OK | os.X_OK):
                    continue
                paths.setdefault(str(Path(mount) / "camera-storage"), mount)
        except OSError:
            pass
        out = []
        for path, mount in paths.items():
            try:
                probe = Path(path)
                while not probe.exists() and probe != probe.parent:
                    probe = probe.parent
                du = shutil.disk_usage(probe)
                label = f"{path} - {human(du.free)} free of {human(du.total)}"
            except OSError:
                label = path
            if path == str(Path(DEFAULT_STORAGE_DIR).resolve()):
                label += " (default)"
            out.append({"path": path, "label": label, "selected": path == str(self.root)})
        return out

    def validate_path(self, raw: str) -> str:
        """Check a user-chosen location can be written to; creates it. Returns the
        absolute path to store."""
        raw = (raw or "").strip()
        if not raw:
            raise ValueError("storage path must not be empty")
        path = Path(raw).expanduser().resolve()
        try:
            path.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path, prefix=".write-test-"):
                pass
        except OSError as e:
            raise ValueError(f"cannot write to {path}: {e.strerror or e}") from None
        # keep relative paths relative: they follow the working directory if it moves
        return raw if not os.path.isabs(os.path.expanduser(raw)) and not raw.startswith("~") else str(path)

    # -- pruning --------------------------------------------------------------

    def _recordings(self) -> list[tuple[datetime, Path, int]]:
        """(started, path, size) of every recording under the storage root, oldest first."""
        found = []
        root = self.root
        if not root.is_dir():
            return found
        for cam_dir in root.iterdir():
            if not cam_dir.is_dir():
                continue
            for f in cam_dir.iterdir():
                m = NAME.match(f.name)
                if not m:
                    continue                        # never touch files we didn't write
                try:
                    found.append((datetime.strptime(m.group(1), "%Y%m%d_%H%M%S"), f, f.stat().st_size))
                except OSError:
                    pass
        found.sort(key=lambda r: (r[0], str(r[1])))
        return found

    def free_percent(self) -> float:
        probe = self.root
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        du = shutil.disk_usage(probe)
        return 100 * du.free / du.total if du.total else 100.0

    def prune(self, force: bool = False) -> PruneResult:
        """Delete the oldest recordings while free space is below the minimum.
        force=True runs even if pruning is switched off (the "Prune now" button)."""
        with self._lock:
            cfg = self._cfg
            res = PruneResult(at=time.time())
            try:
                res.free_percent = self.free_percent()
            except OSError as e:
                res.note = f"cannot read disk usage: {e}"
                self.last_prune = res
                return res
            if not (cfg.prune_enabled or force):
                res.note = "pruning is off"
                return res
            if res.free_percent >= cfg.min_free_percent:
                res.note = "enough free space"
                if force:
                    self.last_prune = res
                return res
            target = cfg.min_free_percent + PRUNE_MARGIN
            now = time.time()
            for started, path, size in self._recordings():
                if res.free_percent >= target:
                    break
                try:
                    if now - path.stat().st_mtime < RECENT_SECONDS:
                        continue                    # probably still being recorded
                    path.unlink()
                except OSError as e:
                    log.warning("prune: cannot delete %s: %s", path, e)
                    continue
                res.deleted += 1
                res.freed += size
                res.free_percent = self.free_percent()
            if res.free_percent < cfg.min_free_percent:
                res.note = "still below the minimum: nothing older left to delete"
                log.error("storage: only %.1f%% free and no more recordings can be pruned",
                          res.free_percent)
            if res.deleted:
                log.info("prune: deleted %d recordings (%s), %.1f%% free now",
                         res.deleted, human(res.freed), res.free_percent)
            self.last_prune = res
            return res
