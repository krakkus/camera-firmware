"""Hardware image controls (V4L2) with three modes per control:

  camera    leave it to the camera: its automatic mode where it has one (exposure, white
            balance), otherwise its default value
  manual    a value entered by the user
  software  a closed loop steers the control from measured frame statistics (AutoTuner);
            available for exposure, contrast, brightness and gamma

What a camera offers is read from the device (v4l2-ctl), so the list, ranges and menu
entries differ per camera.
"""
from __future__ import annotations

import logging
import math
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import cv2
import numpy as np

from .devices import node_identity

log = logging.getLogger(__name__)

MODES = ("camera", "manual", "software")

# kernel names changed over time; accept both
EXPOSURE_AUTO = ("auto_exposure", "exposure_auto")
EXPOSURE_TIME = ("exposure_time_absolute", "exposure_absolute")
WB_AUTO = ("white_balance_automatic", "white_balance_temperature_auto")
WB_TEMP = ("white_balance_temperature",)

SOFTWARE_KEYS = ("exposure", "contrast", "brightness", "gamma")    # in tuning rotation order
ORDER = ("exposure", "white_balance", "brightness", "contrast", "saturation", "sharpness",
         "gain", "gamma", "power_line_frequency")
LABELS = {"exposure": "Exposure time", "white_balance": "White balance",
          "power_line_frequency": "Power line frequency"}


# --- reading what the device offers ----------------------------------------------

@dataclass
class Control:
    name: str
    kind: str                   # int | bool | menu
    min: int = 0
    max: int = 1
    step: int = 1
    default: int = 0
    value: int = 0
    menu: dict[int, str] = field(default_factory=dict)


_LINE = re.compile(r"^\s*(\w+)\s+0x[0-9a-f]+\s+\((\w+)\)\s*:\s*(.*)$")
_MENU_ITEM = re.compile(r"^\s+(\d+):\s*(.+?)\s*$")


def parse_controls(text: str) -> dict[str, Control]:
    controls: dict[str, Control] = {}
    last: Control | None = None
    for line in text.splitlines():
        m = _LINE.match(line)
        if m:
            name, kind, rest = m.groups()
            last = None
            if kind not in ("int", "bool", "menu", "intmenu"):
                continue
            nums = {k: int(v) for k, v in re.findall(r"\b(min|max|step|default|value)=(-?\d+)", rest)}
            c = Control(name, "menu" if kind == "intmenu" else kind, nums.get("min", 0),
                        nums.get("max", 1), nums.get("step", 1) or 1, nums.get("default", 0),
                        nums.get("value", nums.get("default", 0)))
            controls[name] = last = c
            continue
        m = _MENU_ITEM.match(line)
        if m and last is not None and last.kind == "menu":
            last.menu[int(m.group(1))] = m.group(2)
    return controls


_cache: dict[tuple, tuple[float, dict[str, Control]]] = {}


def list_controls(node: str, fresh: bool = False) -> dict[str, Control]:
    now = time.monotonic()
    key = node_identity(node)           # another camera on the same node: read afresh
    hit = _cache.get(key)
    if hit and not fresh and now - hit[0] < 10:
        return hit[1]
    try:
        out = subprocess.run(["v4l2-ctl", "-d", node, "--list-ctrls-menus"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    controls = parse_controls(out)
    _cache[key] = (now, controls)
    return controls


@dataclass
class Logical:
    """A control as the user sees it. Exposure and white balance each fold an
    automatic switch and a value into one."""
    key: str
    label: str
    kind: str                   # int | bool | menu
    min: int
    max: int
    step: int
    default: int
    value: int                  # what the device reports right now
    menu: list[tuple[int, str]]
    modes: list[str]
    raw: dict[str, str]         # roles -> V4L2 control names: auto / value

    def to_dict(self) -> dict:
        d = {"key": self.key, "label": self.label, "kind": self.kind, "min": self.min,
             "max": self.max, "step": self.step, "default": self.default, "value": self.value,
             "menu": self.menu, "modes": self.modes}
        if self.key in OFFSET_TARGETS:
            what, base, per = OFFSET_TARGETS[self.key]
            d["offset"] = {"what": what, "base": base, "per": per,
                           "min": OFFSET_RANGE[0], "max": OFFSET_RANGE[1]}
        return d


def _first(controls: dict[str, Control], names: tuple[str, ...]) -> Control | None:
    return next((controls[n] for n in names if n in controls), None)


def logical_controls(node: str) -> list[Logical]:
    ctls = list_controls(node)
    out: list[Logical] = []
    exp_auto, exp_time = _first(ctls, EXPOSURE_AUTO), _first(ctls, EXPOSURE_TIME)
    if exp_auto and exp_time:
        out.append(Logical("exposure", LABELS["exposure"], "int", exp_time.min, exp_time.max,
                           exp_time.step, exp_time.default, exp_time.value, [],
                           ["camera", "manual", "software"],
                           {"auto": exp_auto.name, "value": exp_time.name}))
    wb_auto, wb_temp = _first(ctls, WB_AUTO), _first(ctls, WB_TEMP)
    if wb_auto and wb_temp:
        out.append(Logical("white_balance", LABELS["white_balance"], "int", wb_temp.min,
                           wb_temp.max, wb_temp.step, wb_temp.default, wb_temp.value, [],
                           ["camera", "manual"],
                           {"auto": wb_auto.name, "value": wb_temp.name}))
    folded: set[str] = set()                # consumed by the exposure / white balance rows
    if exp_auto and exp_time:
        folded |= {exp_auto.name, exp_time.name}
    if wb_auto and wb_temp:
        folded |= {wb_auto.name, wb_temp.name}
    for c in ctls.values():
        if c.name in folded:
            continue
        modes = ["camera", "manual"] + (["software"] if c.name in SOFTWARE_KEYS else [])
        out.append(Logical(c.name, LABELS.get(c.name, c.name.replace("_", " ").capitalize()),
                           c.kind, c.min, c.max, c.step, c.default, c.value,
                           sorted(c.menu.items()), modes, {"value": c.name}))
    rank = {k: i for i, k in enumerate(ORDER)}
    out.sort(key=lambda l: (rank.get(l.key, len(ORDER)), l.key))
    return out


def validate(node: str, cfg: dict[str, dict]) -> None:
    """Check configured values against what the device really accepts. Controls the
    device doesn't have (or when it isn't plugged in) are not checked."""
    by_key = {l.key: l for l in logical_controls(node)}
    for key, c in cfg.items():
        l = by_key.get(key)
        if l is None:
            continue
        if c["mode"] not in l.modes:
            raise ValueError(f"{l.label}: {c['mode']} mode is not available for this camera")
        if c["mode"] == "manual":
            v = c.get("value")
            if v is None:
                raise ValueError(f"{l.label}: manual mode needs a value")
            allowed = {m[0] for m in l.menu}
            if l.kind == "menu" and v not in allowed:
                raise ValueError(f"{l.label}: {v} is not one of {sorted(allowed)}")
            if l.kind != "menu" and not l.min <= v <= l.max:
                raise ValueError(f"{l.label}: {v} is outside {l.min}-{l.max}")


# --- writing ------------------------------------------------------------------------

class ControlWriter:
    """Applies control writes from a thread: v4l2-ctl can block for a long time and
    must never stall frame capture. Later writes to a control replace pending ones."""

    def __init__(self, node: str) -> None:
        self.node = node
        self._pending: dict[str, int] = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._failed: set[str] = set()
        self._thread = threading.Thread(target=self._run, name="v4l2-ctl", daemon=True)
        self._thread.start()

    def set(self, name: str, value: int) -> None:
        with self._lock:
            self._pending[name] = int(value)
        self._wake.set()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            with self._lock:
                batch, self._pending = self._pending, {}
            for name, value in batch.items():          # insertion order: auto switch before value
                try:
                    r = subprocess.run(["v4l2-ctl", "-d", self.node, "-c", f"{name}={value}"],
                                       capture_output=True, text=True, timeout=5)
                    if r.returncode and name not in self._failed:
                        self._failed.add(name)
                        log.warning("%s: setting %s=%s failed: %s", self.node, name, value,
                                    (r.stderr or r.stdout).strip())
                except (OSError, subprocess.SubprocessError) as e:
                    log.warning("%s: v4l2-ctl failed: %s", self.node, e)


# --- software control loop ------------------------------------------------------------
# Ported from the "camera1" firmware. Every ADJUST_INTERVAL one control is nudged, in
# rotation, from statistics of the current frame. The step sizes there were written for
# one webcam's ranges (contrast 0-100, brightness -64..64, gamma 100..500); here they are
# scaled by each control's own range.

ADJUST_INTERVAL = 1.5          # seconds; let each change settle before measuring again
HIGHLIGHT_IGNORE_PERCENTILE = 95   # meter without the brightest 5%: sky, lamps, headlights
TARGET_MEAN = 100              # exposure: mean of the metered pixels
DEADBAND = 8
TARGET_CONTRAST_STD = 45       # contrast: spread of the metered pixels
CONTRAST_DEADBAND = 4
BLACK_POINT_PERCENTILE = 5     # brightness: keep a sliver of headroom above pure black
TARGET_BLACK_POINT = 12
BRIGHTNESS_DEADBAND = 3
# How far brightness moves the black level differs a lot between cameras and even across
# one camera's range (measured 0.25-1.5 gray levels per unit on one webcam), so a fixed step
# size either crawls or overshoots. The loop learns the slope from its own corrections
# (secant method), takes only part of the needed step, and never moves more than
# BRIGHTNESS_MAX_STEP of the range in one go.
BRIGHTNESS_DAMPING = 0.7
BRIGHTNESS_MAX_STEP = 0.15
BRIGHTNESS_SLOPE_RANGE = (0.15, 2.0)    # plausible gray levels per control unit
GAMMA_DEADBAND = 3             # gamma: mean-vs-median skew
# Safety net for scenes with blown highlights AND crushed shadows at once, which these
# global controls cannot fix: ease contrast/brightness/gamma back toward their defaults
# instead of chasing either side. Counted directly from pixels, so it does not depend on
# the controls it gates.
# User offset (-100..+100, a slider) shifts each loop's target. Units are the metric each
# loop steers, so +100 means: exposure -> mean brightness +50, contrast -> spread +30,
# brightness -> black level +20, gamma -> tone skew +10.
OFFSET_RANGE = (-100, 100)
OFFSET_TARGETS = {          # key: (what the target is, base value, change per offset unit)
    "exposure": ("mean brightness", TARGET_MEAN, 0.5),
    "contrast": ("contrast spread", TARGET_CONTRAST_STD, 0.3),
    "brightness": ("black level", TARGET_BLACK_POINT, 0.2),
    "gamma": ("tone skew", 0.0, 0.1),
}
# Some webcams accept exposure writes but ignore them (they keep their own auto exposure).
# If changing exposure by at least this much moves the picture by less than a quarter of
# what it should, several times running, the loop gives up on it and says so.
NOEFFECT_MIN_CHANGE = 0.2       # exposure change (natural log) accumulated before judging
NOEFFECT_STRIKES = 2
HDR_CLIP_GRAY_LEVEL = 250
HDR_CRUSH_GRAY_LEVEL = 5
HDR_CLIP_FRAC_THRESHOLD = 0.05
HDR_EASE_BACK_FACTOR = 0.3
# Contrast scales around mid-gray, so in a picture far from mid-gray (night) more contrast
# pushes everything toward black or white and the spread shrinks: the loop would then
# chase its target to the end of the range. Outside this band of metered mean it eases
# back toward the default instead.
CONTRAST_MEAN_BAND = (40, 200)
# Webcams' own auto exposure also raises a gain that is not exposed as a control, while
# software exposure is capped at one frame interval. When software exposure sits at that
# cap and the picture is still too dark for EXPOSURE_FALLBACK_STEPS steps, exposure is
# handed back to the camera's auto mode, and retried no sooner than EXPOSURE_RETRY later,
# once the picture is brighter than the target.
EXPOSURE_FALLBACK_STEPS = 3
EXPOSURE_RETRY = 300.0          # seconds


class AutoTuner:
    def __init__(self, logical: dict[str, Logical], values: dict[str, int],
                 write: Callable[[str, int], None],
                 exposure_auto: Callable[[bool], None] = lambda on: None) -> None:
        self._logical = logical
        self.values = values            # current value per logical key (shared with the manager)
        self._write = write
        self._exposure_auto = exposure_auto     # True: camera's auto exposure, False: manual
        self._rotation = 0
        self._last = 0.0
        self.fps = 30.0                 # exposure may not exceed one frame interval...
        self.slow_frames_ok = False     # ...unless the camera may slow its frame rate (below)
        self.offsets: dict[str, int] = {}       # user target offsets per key, see OFFSET_TARGETS
        self.ineffective: set[str] = set()      # controls found to have no effect on the picture
        self._exp_prev: tuple[int, float] | None = None
        self._exp_strikes = 0
        self._slope: float | None = None        # brightness: learned gray levels per unit
        self._last_black: tuple[int, float] | None = None   # (brightness value, black point)
        self.exposure_fallback = False          # exposure handed to the camera's auto mode
        self._fallback_since = 0.0
        self._dark_at_cap = 0

    def _learn_brightness_slope(self, value: int, black: float, span: int) -> None:
        """Update the estimate of gray levels per brightness unit from the last correction."""
        if self._slope is None:
            self._slope = 150.0 / span                  # first guess: about 150 levels over the range
        prev, self._last_black = self._last_black, (value, black)
        if prev is None or prev[0] == value:
            return
        clipped = min(prev[1], black) <= 1 or max(prev[1], black) >= 254
        if clipped:
            return                                      # a clipped black point says nothing
        measured = (black - prev[1]) / (value - prev[0])
        if measured > 0:
            lo, hi = BRIGHTNESS_SLOPE_RANGE
            self._slope = max(lo, min(hi, 0.5 * self._slope + 0.5 * measured))

    def target(self, key: str) -> float:
        _what, base, per = OFFSET_TARGETS[key]
        return base + per * self.offsets.get(key, 0)

    def reset_exposure_check(self) -> None:
        self.ineffective.discard("exposure")
        self._exp_prev, self._exp_strikes = None, 0
        self.exposure_fallback, self._dark_at_cap = False, 0

    def _exposure_cap(self, L: Logical) -> int:
        """exposure_time_absolute is in 100 microsecond units. A longer exposure than the
        frame interval would silently cut the camera's frame rate, so it is capped there,
        unless the camera's "exposure dynamic framerate" is switched on by the user: then
        slowing down in the dark is wanted (the recorder fills the gaps)."""
        return L.max if self.slow_frames_ok else \
            max(max(L.min, 1), min(L.max, int(10000 / self.fps)))

    def _exposure_has_effect(self, v: int, mean: float) -> bool:
        """False once exposure changes have repeatedly not moved the picture. Each loop
        step is only a few percent, so judge against a reference point once the exposure
        has moved far enough from it."""
        ref = self._exp_prev
        if ref is None:
            self._exp_prev = (v, mean)
            return True
        ratio = abs(math.log(max(v, 1) / max(ref[0], 1)))
        if ratio >= NOEFFECT_MIN_CHANGE:
            expected = ref[1] * ratio                   # mean brightness is ~proportional to exposure
            if expected >= 4:
                moved = abs(mean - ref[1])
                self._exp_strikes = self._exp_strikes + 1 if moved < 0.25 * expected else 0
            self._exp_prev = (v, mean)
        return self._exp_strikes < NOEFFECT_STRIKES

    def due(self) -> bool:
        return time.monotonic() - self._last >= ADJUST_INTERVAL

    def step(self, frame: np.ndarray, enabled: set[str]) -> None:
        self._last = time.monotonic()
        keys = [k for k in SOFTWARE_KEYS if k in enabled and k in self._logical]
        if not keys:
            return
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        clip = np.percentile(gray, HIGHLIGHT_IGNORE_PERCENTILE)
        metered = gray[gray <= clip]
        if not metered.size:
            metered = gray.ravel()
        mean, median, std = float(metered.mean()), float(np.median(metered)), float(metered.std())
        black = float(np.percentile(gray, BLACK_POINT_PERCENTILE))
        irreconcilable = ((gray >= HDR_CLIP_GRAY_LEVEL).mean() > HDR_CLIP_FRAC_THRESHOLD
                          and (gray <= HDR_CRUSH_GRAY_LEVEL).mean() > HDR_CLIP_FRAC_THRESHOLD)

        key = keys[self._rotation % len(keys)]
        self._rotation += 1
        L, v = self._logical[key], self.values[key]
        span = max(L.max - L.min, 1)

        if key == "exposure":
            cap = self._exposure_cap(L)
            if self.exposure_fallback:
                if (time.monotonic() - self._fallback_since >= EXPOSURE_RETRY
                        and mean > self.target("exposure") + DEADBAND):
                    log.info("scene is bright again; software exposure takes over from the camera")
                    self.exposure_fallback, self._dark_at_cap, self._exp_prev = False, 0, None
                    self._exposure_auto(False)
                    self.values[key] = cap
                    self._write(L.raw["value"], cap)
                return
            if v > cap:
                self.values[key] = cap
                self._write(L.raw["value"], cap)
                return
            if key in self.ineffective:
                return
            if not self._exposure_has_effect(v, mean):
                self.ineffective.add(key)
                log.warning("exposure has no visible effect on this camera; leaving it alone")
                return
            error = self.target("exposure") - mean
            self._dark_at_cap = self._dark_at_cap + 1 if v >= cap and error > DEADBAND else 0
            if self._dark_at_cap >= EXPOSURE_FALLBACK_STEPS:
                log.info("exposure is at its limit and the picture is still too dark; "
                         "using the camera's auto exposure")
                self.exposure_fallback, self._fallback_since = True, time.monotonic()
                self._exposure_auto(True)
                return
            if abs(error) <= DEADBAND:
                return
            new = int(v * (1 + 0.6 * error / 255))
            if new == v:
                new = v + (1 if error > 0 else -1)      # multiplicative step rounds to nothing
            new = max(max(L.min, 1), min(cap, new))
        elif irreconcilable or (key == "contrast" and not
                                CONTRAST_MEAN_BAND[0] <= mean <= CONTRAST_MEAN_BAND[1]):
            new = int(v + (L.default - v) * HDR_EASE_BACK_FACTOR)
        elif key == "contrast":
            error = self.target("contrast") - std
            if abs(error) <= CONTRAST_DEADBAND:
                return
            new = int(v + 0.5 * error * span / 100)
            if error < 0 and self.offsets.get(key, 0) >= 0:
                # the target is a minimum: a scene with plenty of contrast of its own (a
                # sunny street) is not flattened below the default, which looks hazy.
                # A negative offset asks for less contrast, and then it may go lower
                new = max(new, min(v, L.default))
        elif key == "brightness":
            error = max(self.target("brightness"), 0.0) - black
            self._learn_brightness_slope(v, black, span)
            if abs(error) <= BRIGHTNESS_DEADBAND:
                return
            step = BRIGHTNESS_DAMPING * error / self._slope
            limit = BRIGHTNESS_MAX_STEP * span
            new = int(v + max(-limit, min(limit, step)))
            if new == v:
                new = v + (1 if error > 0 else -1)
        else:                                            # gamma
            # When exposure can do no more (left to the camera, at its cap, or handed back
            # to the camera's auto mode) and the picture is clearly dark, gamma lifts it;
            # the skew below barely moves in a nearly black picture.
            exp = self._logical.get("exposure")
            exposure_done = (not exp or "exposure" not in enabled or self.exposure_fallback
                             or self.values["exposure"] >= self._exposure_cap(exp))
            saturated = exposure_done and mean < 0.5 * self.target("exposure")
            error = (self.target("exposure") - mean) if saturated \
                else (mean - median) - self.target("gamma")
            if abs(error) <= GAMMA_DEADBAND:
                return
            new = int(v + 2.0 * error * span / 400)
            if not saturated and (error > 0) == (mean > self.target("exposure")):
                # a bright sky skews a picture that is already bright: evening out the
                # tones would brighten it further (or darken a dark one). Ease back instead
                new = int(v + (L.default - v) * HDR_EASE_BACK_FACTOR)
        new = max(L.min, min(L.max, new))
        if new != v:
            self.values[key] = new
            self._write(L.raw["value"], new)


# --- per-camera manager ----------------------------------------------------------------

def _manual_menu_value(menu: dict[int, str], fallback: int = 1) -> int:
    return next((k for k, name in menu.items() if "manual" in name.lower()), fallback)


class ControlManager:
    """Applies a camera's configured control modes to its device and runs the software
    loop. Created when the device is opened; close() when it is released."""

    def __init__(self, node: str) -> None:
        self.node = node
        self.writer = ControlWriter(node)
        self._raw = list_controls(node, fresh=True)
        self.logical = {l.key: l for l in logical_controls(node)}
        self.values: dict[str, int] = {k: l.value for k, l in self.logical.items()}
        self._applied: dict[str, tuple] = {}
        self._software: set[str] = set()
        self.tuner = AutoTuner(self.logical, self.values, self._write_value,
                               self._exposure_auto)

    def _write_value(self, raw_name: str, value: int) -> None:
        self.writer.set(raw_name, value)

    def _exposure_auto(self, on: bool) -> None:
        auto = self._raw[self.logical["exposure"].raw["auto"]]
        self.writer.set(auto.name, auto.default if on else _manual_menu_value(auto.menu))

    def sync(self, cfg: dict[str, dict]) -> None:
        """Bring the device in line with the configured modes; only changes are written."""
        software: set[str] = set()
        for key, L in self.logical.items():
            c = cfg.get(key) or {"mode": "camera"}
            mode = c["mode"] if c["mode"] in L.modes else "camera"
            value = c.get("value", L.default) if mode == "manual" else None
            if self._applied.get(key) == (mode, value):
                if mode == "software":
                    software.add(key)
                continue
            self._applied[key] = (mode, value)
            self._apply(L, mode, value)
            if mode == "software":
                software.add(key)
        if "exposure" in software and "exposure" not in self._software:
            self.tuner.reset_exposure_check()           # entering software mode: test afresh
        self._software = software
        self.tuner.offsets = {k: int((cfg.get(k) or {}).get("offset", 0)) for k in software}
        dyn = cfg.get("exposure_dynamic_framerate") or {}
        self.tuner.slow_frames_ok = dyn.get("mode") == "manual" and dyn.get("value") == 1

    def _apply(self, L: Logical, mode: str, value: int | None) -> None:
        w = self.writer.set
        if L.key == "exposure":
            auto = self._raw[L.raw["auto"]]
            if mode == "camera":
                w(auto.name, auto.default)
            else:
                w(auto.name, _manual_menu_value(auto.menu))
                if mode == "manual":
                    w(L.raw["value"], value)
                    self.values[L.key] = value
                else:
                    w(L.raw["value"], max(self.values[L.key], max(L.min, 1)))
        elif L.key == "white_balance":
            auto = self._raw[L.raw["auto"]]
            if mode == "camera":
                w(auto.name, auto.default if auto.default else 1)
            else:
                w(auto.name, 0)
                w(L.raw["value"], value if value is not None else L.default)
        elif mode == "camera":
            w(L.raw["value"], L.default)
            self.values[L.key] = L.default
        elif mode == "manual":
            w(L.raw["value"], value)
            self.values[L.key] = value
        else:                                           # software: start from where it is now
            w(L.raw["value"], self.values[L.key])

    def tick(self, frame: np.ndarray, fps: float) -> None:
        self.tuner.fps = max(fps, 1.0)
        if self._software and self.tuner.due():
            self.tuner.step(frame, self._software)

    def software_values(self) -> dict[str, int]:
        return {k: self.values[k] for k in self._software}

    def ineffective(self) -> list[str]:
        return sorted(self.tuner.ineffective & self._software)

    def close(self) -> None:
        self.writer.close()
