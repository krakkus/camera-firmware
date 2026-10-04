"""Which set of camera controls applies right now: the light one or the dark one.

By sun it is dark from sunset (plus the camera's sun_offset minutes) until sunrise (minus
them), computed with astral for the device's location (DeviceConfig latitude/longitude,
or else the city of the system time zone).

By light level the picture's mean brightness decides. The camera cannot be asked how much
light there is (in its auto mode it does not report the exposure it uses), so the picture
is the only measure. That picture already shows the active set's effect, for instance a
brighter night set, so switching needs a gap: dark below `dark_below`, light again only
above `light_above`, each held for HOLD seconds. Set the gap wider than the dark set
brightens the picture, or it will switch back and forth.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import cv2
import numpy as np
from astral import Observer
from astral.geocoder import all_locations, database
from astral.sun import elevation, sun

from .camera import CameraSettings
from .camera_server import DeviceConfig

log = logging.getLogger(__name__)

HOLD = 120.0            # seconds a light level must hold before switching
SAMPLE_EVERY = 1.0      # seconds between measurements
SMOOTHING = 0.2         # of each new measurement (headlights passing by barely count)


def local_zone() -> tuple[str, tzinfo]:
    """The system time zone's name and tzinfo (UTC when it cannot be told)."""
    name = os.environ.get("TZ") or os.path.realpath("/etc/localtime").partition("zoneinfo/")[2]
    try:
        return name, ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC", ZoneInfo("UTC")


def location(cfg: DeviceConfig) -> dict | None:
    """Where the sun is computed for: the configured coordinates, or else a city in the
    system time zone from astral's list. None if neither is known."""
    if cfg.latitude is not None and cfg.longitude is not None:
        return {"latitude": cfg.latitude, "longitude": cfg.longitude, "source": "configured"}
    zone, _ = local_zone()
    city = next((c for c in all_locations(database()) if c.timezone == zone), None)
    if city is None:
        return None
    return {"latitude": round(city.latitude, 4), "longitude": round(city.longitude, 4),
            "source": f"time zone ({city.name})"}


def sun_window(cfg: DeviceConfig, offset_min: int, day: date | None = None) -> dict | None:
    """Today's light period: {"light_from": sunrise - offset, "dark_from": sunset + offset}
    as local datetimes, or {"always": "dark" | "light"} where the sun does not rise or set.
    None without a location."""
    loc = location(cfg)
    if loc is None:
        return None
    _, tz = local_zone()
    obs = Observer(loc["latitude"], loc["longitude"])
    day = day or datetime.now(tz).date()
    try:
        s = sun(obs, day, tzinfo=tz)
    except ValueError:                  # polar day or night
        noon = datetime(day.year, day.month, day.day, 12, tzinfo=tz)
        return {"always": "light" if elevation(obs, noon) > 0 else "dark"}
    off = timedelta(minutes=offset_min)
    return {"light_from": s["sunrise"] - off, "dark_from": s["sunset"] + off}


class DayNight:
    def __init__(self, config: DeviceConfig) -> None:
        self._config = config
        self._window: tuple[tuple, dict | None] | None = None     # (date, offset), window
        self.dark = False
        self.level: float | None = None     # smoothed mean gray, 0-255
        self._next = 0.0
        self._since: float | None = None    # when the level first called for a switch

    def update(self, frame: np.ndarray, s: CameraSettings) -> bool:
        """True while the dark set applies."""
        if s.profile_switch == "sun":
            self.dark = self._sun(s)
        elif s.profile_switch == "light":
            self._measure(frame, s)
        else:
            self.dark = False
        return self.dark

    def window(self, s: CameraSettings) -> dict | None:
        """Today's sun window for this camera, computed once a day (or on a change)."""
        _, tz = local_zone()
        cfg = self._config
        key = (datetime.now(tz).date(), s.sun_offset, cfg.latitude, cfg.longitude)
        if self._window is None or self._window[0] != key:
            self._window = (key, sun_window(cfg, s.sun_offset, key[0]))
        return self._window[1]

    def _sun(self, s: CameraSettings) -> bool:
        w = self.window(s)
        if w is None:
            return False            # no location: stay on the light set
        if "always" in w:
            return w["always"] == "dark"
        now = datetime.now(w["light_from"].tzinfo)
        return not w["light_from"] <= now < w["dark_from"]

    def _measure(self, frame: np.ndarray, s: CameraSettings) -> None:
        now = time.monotonic()
        if now < self._next:
            return
        self._next = now + SAMPLE_EVERY
        small = cv2.resize(frame, (160, max(1, frame.shape[0] * 160 // frame.shape[1])))
        mean = float(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).mean())
        if self.level is None:              # first look: no history, decide right away
            self.level = mean
            self.dark = mean < (s.dark_below + s.light_above) / 2
            return
        self.level += SMOOTHING * (mean - self.level)
        wants = self.level > s.light_above if self.dark else self.level < s.dark_below
        if not wants:
            self._since = None
        elif self._since is None:
            self._since = now
        elif now - self._since >= HOLD:
            self.dark, self._since = not self.dark, None
