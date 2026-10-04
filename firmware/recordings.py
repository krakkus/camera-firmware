"""Listing of recording files on disk."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

NAME = re.compile(r"^(\d{8}_\d{6})(_motion|_object)?\.(mp4|m4a)$")


@dataclass
class Recording:
    camera_id: str
    camera_name: str
    name: str
    started: datetime          # local time, from the file name
    motion: bool
    object: bool
    audio_only: bool
    size: int
    hour_end: bool = False     # oldest file of its hour (list is newest first)

    @property
    def url(self) -> str:
        return f"/recordings/{quote(self.camera_id)}/{quote(self.name)}"

    @property
    def size_text(self) -> str:
        n = float(self.size)
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return f"{n:.0f} B" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024


def list_recordings(root: Path, names: dict[str, str], camera: str = "",
                    limit: int = 500) -> tuple[list[Recording], bool]:
    """Newest first. `names` maps camera id to display name; folders of cameras that
    no longer exist are still listed under their id. Returns (recordings, truncated)."""
    found: list[Recording] = []
    for cam_dir in sorted(Path(root).glob("*")):
        if not cam_dir.is_dir() or (camera and cam_dir.name != camera):
            continue
        for f in cam_dir.iterdir():
            m = NAME.match(f.name)
            if not m:
                continue
            found.append(Recording(
                cam_dir.name, names.get(cam_dir.name, cam_dir.name), f.name,
                datetime.strptime(m.group(1), "%Y%m%d_%H%M%S"), m.group(2) == "_motion",
                m.group(2) == "_object",
                m.group(3) == "m4a", f.stat().st_size))
    found.sort(key=lambda r: (r.started, r.camera_id), reverse=True)
    truncated = len(found) > limit
    found = found[:limit]
    for i, r in enumerate(found):
        nxt = found[i + 1].started if i + 1 < len(found) else None
        r.hour_end = nxt is None or (nxt.date(), nxt.hour) != (r.started.date(), r.started.hour)
    return found, truncated
