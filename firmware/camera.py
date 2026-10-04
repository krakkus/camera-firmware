"""Camera: one physical camera and its settings."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any

from .coco import COCO_CLASSES

MODES = ("camera", "manual", "software")      # per hardware control, see controls.py
PROFILE_SWITCHES = ("off", "light", "sun")     # see CameraSettings.profile_switch
RECORD_MODES = ("off", "motion", "object", "continuous")
NEEDS_VIDEO = ("motion", "object")


@dataclass
class CameraSettings:
    width: int = 1280
    height: int = 720
    fps: int = 30              # frame rate requested from the camera (light set, see below)
    rotation: int = 0          # degrees: 0, 90, 180, 270
    flip_horizontal: bool = False
    flip_vertical: bool = False
    jpeg_quality: int = 80     # 1-100, used by the stream server

    # Hardware image controls (see controls.py), by name, e.g.
    #   {"exposure": {"mode": "software"}, "contrast": {"mode": "manual", "value": 140}}
    # mode is "camera", "manual" or "software"; controls not listed are left to the camera.
    controls: dict[str, dict] = field(default_factory=dict)
    # A second set for the dark, same format. Empty: the same as `controls`. The frame rate
    # belongs to the sets too: a lower one allows longer exposures. 0: the same as fps.
    controls_dark: dict[str, dict] = field(default_factory=dict)
    fps_dark: int = 0
    # When controls_dark is used instead of controls (see daynight.py):
    #   "off"    never: one set, day and night
    #   "light"  by the picture's light level: dark below dark_below, light again above
    #            light_above (0-255 mean gray), each after it held for a while
    #   "sun"    from sunset until sunrise, each moved sun_offset minutes into the night
    #            (negative: dark starts before sunset), for the device's location
    profile_switch: str = "off"
    dark_below: int = 30
    light_above: int = 80
    sun_offset: int = 0

    # recording
    # "off", "continuous" (always), "motion" or "object" (the last two need video).
    # File length comes from the global config (DeviceConfig.segment_minutes).
    record_mode: str = "off"
    pre_roll_seconds: int = 3          # motion/object: footage kept from before the trigger
    post_roll_seconds: int = 5         # motion/object: keep recording this long afterwards

    # object detection (record_mode == "object"): COCO class names, see coco.py
    object_classes: list[str] = field(default_factory=lambda: ["person"])
    object_confidence: float = 0.5

    record_crf: int = 23               # H.264 quality, 0-51, lower = better/bigger

    # motion detection (runs whenever record_mode == "motion")
    motion_threshold: int = 25         # per-pixel difference, 1-255
    motion_min_area: int = 500         # changed pixels (at 320px-wide analysis size) to count

    def validate(self) -> None:
        if self.record_mode not in RECORD_MODES:
            raise ValueError(f"record_mode must be one of {', '.join(RECORD_MODES)}")
        unknown = [c for c in self.object_classes if c not in COCO_CLASSES]
        if unknown:
            raise ValueError(f"unknown object classes: {', '.join(unknown)}")
        if self.record_mode == "object" and not self.object_classes:
            raise ValueError("object recording needs at least one object class")
        if not 0.05 <= self.object_confidence <= 0.95:
            raise ValueError("object_confidence must be 0.05-0.95")
        if not 0 <= self.record_crf <= 51:
            raise ValueError("record_crf must be 0-51")
        if not 0 <= self.pre_roll_seconds <= 30:
            raise ValueError("pre_roll_seconds must be 0-30")
        if not 0 <= self.post_roll_seconds <= 300:
            raise ValueError("post_roll_seconds must be 0-300")
        if not 1 <= self.motion_threshold <= 255:
            raise ValueError("motion_threshold must be 1-255")
        if self.motion_min_area < 1:
            raise ValueError("motion_min_area must be positive")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("width and height must be positive")
        if not 1 <= self.fps <= 120:
            raise ValueError("fps must be 1-120")
        if self.fps_dark and not 1 <= self.fps_dark <= 120:
            raise ValueError("fps_dark must be 1-120 (or 0: the same as fps)")
        if self.rotation not in (0, 90, 180, 270):
            raise ValueError("rotation must be 0, 90, 180 or 270")
        _check_controls("controls", self.controls)
        _check_controls("controls_dark", self.controls_dark)
        if self.profile_switch not in PROFILE_SWITCHES:
            raise ValueError(f"profile_switch must be one of {', '.join(PROFILE_SWITCHES)}")
        if not 0 <= self.dark_below < self.light_above <= 255:
            raise ValueError("light levels: dark below must be lower than light above (0-255)")
        if isinstance(self.sun_offset, bool) or not isinstance(self.sun_offset, int) \
                or not -180 <= self.sun_offset <= 180:
            raise ValueError("sun_offset must be a whole number of minutes from -180 to 180")
        if not 1 <= self.jpeg_quality <= 100:
            raise ValueError("jpeg_quality must be 1-100")


def _check_controls(name: str, controls) -> None:
    if not isinstance(controls, dict):
        raise ValueError(f"{name} must be an object")
    for key, c in controls.items():
        if not isinstance(c, dict) or c.get("mode") not in MODES:
            raise ValueError(f"{name}.{key}: mode must be one of {', '.join(MODES)}")
        if "value" in c and (isinstance(c["value"], bool) or not isinstance(c["value"], int)):
            raise ValueError(f"{name}.{key}: value must be a whole number")
        if c["mode"] == "manual" and "value" not in c:
            raise ValueError(f"{name}.{key}: manual mode needs a value")
        off = c.get("offset", 0)
        if isinstance(off, bool) or not isinstance(off, int) or not -100 <= off <= 100:
            raise ValueError(f"{name}.{key}: offset must be a whole number from -100 to 100")


@dataclass
class Camera:
    """Any camera: a video source and/or an audio source. With no video it is an
    audio-only camera; with no audio it is a silent one."""

    id: str                    # stable identifier used in URLs, e.g. "cam0"
    name: str                  # human-readable label
    source: str = ""           # device index, /dev/videoN, or rtsp/http URL; ignored for
                               # USB cameras that set port/device_id below
    enabled: bool = True
    # hot-pluggable video identity (see devices.py); set either or both. The camera is
    # found wherever it is currently attached, and waited for while unplugged.
    port: str = ""
    device_id: str = ""
    # audio: "<port>+<device_id>" of a microphone, or "alsa:<name>"; empty = no audio.
    # Several cameras may use the same microphone.
    audio_source: str = ""
    settings: CameraSettings = field(default_factory=CameraSettings)

    def __post_init__(self) -> None:
        if not self.id or not self.id.replace("-", "").replace("_", "").isalnum():
            raise ValueError("camera id must be alphanumeric (plus - and _)")
        self.settings.validate()
        self._check(self.settings)

    @property
    def has_video(self) -> bool:
        return bool(self.source or self.port or self.device_id)

    def _check(self, settings: CameraSettings) -> None:
        if settings.record_mode in NEEDS_VIDEO and not self.has_video:
            raise ValueError(f"{settings.record_mode} recording needs a video source")

    def update_settings(self, **changes: Any) -> None:
        """Apply setting changes atomically: invalid values leave settings untouched."""
        valid = {f.name for f in fields(CameraSettings)}
        unknown = set(changes) - valid
        if unknown:
            raise ValueError(f"unknown settings: {', '.join(sorted(unknown))}")
        new = CameraSettings(**{**asdict(self.settings), **changes})
        new.validate()
        self._check(new)
        self.settings = new

    def reset_settings(self) -> None:
        """Back to factory settings. Name, sources and enabled state are kept."""
        self.settings = CameraSettings()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Camera:
        data = dict(data)
        settings = dict(data.get("settings", {}))
        # older configs kept audio in the settings
        settings.pop("segment_seconds", None)     # now a global setting
        settings.pop("dark_from", None)           # fixed clock times, replaced by the sun
        settings.pop("dark_until", None)
        if settings.get("profile_switch") == "clock":
            settings["profile_switch"] = "sun"
        for old in ("brightness", "contrast", "saturation"):   # were software post-processing,
            settings.pop(old, None)                            # replaced by hardware controls
        enabled, device = settings.pop("audio_enabled", None), settings.pop("audio_device", None)
        if enabled and not data.get("audio_source"):
            data["audio_source"] = f"alsa:{device or 'default'}"
        data["settings"] = CameraSettings(**settings)
        return cls(**data)
