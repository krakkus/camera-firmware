"""View-model for the Config page: field layout and device pickers."""
from __future__ import annotations

from .camera import Camera

# kind: int | float | bool | select (options are (value, label) pairs; t = value type).
# Ranges mirror CameraSettings.validate.
GROUPS: list[tuple[str, list[dict]]] = [
    ("Image", [
        dict(name="jpeg_quality", label="Stream JPEG quality", kind="int", min=1, max=100),
    ]),
    ("Orientation", [
        dict(name="rotation", label="Rotation", kind="select", t="int",
             options=[(0, "None"), (90, "90\u00b0 clockwise"), (180, "180\u00b0"),
                      (270, "90\u00b0 counter-clockwise")]),
        dict(name="flip_horizontal", label="Flip horizontal", kind="bool"),
        dict(name="flip_vertical", label="Flip vertical", kind="bool"),
    ]),
    ("Recording", [
        dict(name="record_mode", label="Record", kind="select",
             options=[("off", "No"), ("motion", "Motion detect"),
                      ("object", "Person detect"), ("continuous", "Always")],
             help="motion and object need a video source"),
        dict(name="record_crf", label="H.264 quality (CRF)", kind="int", min=0, max=51,
             help="lower = better and bigger"),
        dict(name="pre_roll_seconds", label="Pre-roll (s)", kind="int", min=0, max=30,
             help="motion/object"),
        dict(name="post_roll_seconds", label="Post-roll (s)", kind="int", min=0, max=300,
             help="motion/object"),
    ]),
    ("Motion detection", [
        dict(name="motion_threshold", label="Pixel threshold", kind="int", min=1, max=255),
        dict(name="motion_min_area", label="Minimum changed area", kind="int", min=1, max=1000000),
    ]),
    ("Person detection", [
        dict(name="object_confidence", label="Strictness", kind="float",
             min=0.05, max=0.95, step=0.05,
             help="higher = fewer false alarms, more misses"),
    ]),
]

# shown instead of the mode dropdown when the device can't tell us what it supports
MANUAL_MODE = [
    dict(name="width", label="Width", kind="int", min=16, max=7680),
    dict(name="height", label="Height", kind="int", min=16, max=4320),
]


def resolution_options(cam: Camera, modes: list[tuple[int, int, int, str]]):
    """(resolution choices, {"WxH": [frame rates, highest first]}); (None, None) when the
    device cannot tell (unplugged, or not a V4L2 camera). Rates are those the device lists,
    in any pixel format (a camera may do 30 fps only compressed and 10 fps uncompressed)."""
    if not modes:
        return None, None
    s = cam.settings
    rates: dict[str, set[int]] = {}
    for w, h, fps, _fourcc in modes:
        rates.setdefault(f"{w}x{h}", set()).add(fps)
    current = f"{s.width}x{s.height}"
    opts = [dict(value=k, label=k, selected=k == current) for k in rates]
    if current not in rates:
        opts.insert(0, dict(value=current, selected=True, label=f"{current} (not offered by camera)"))
        rates[current] = set()
    rates[current].update(r for r in (s.fps, s.fps_dark) if r)     # keep saved values selectable
    return opts, {k: sorted(v, reverse=True) for k, v in rates.items()}


def control_rows(cam: Camera, logical: list, dark: bool = False) -> list[dict]:
    """Rows for the "Camera controls" section: what the device offers plus the saved mode.
    dark: the dark set, which starts as a copy of the light one until it is saved."""
    saved_set = (cam.settings.controls_dark or cam.settings.controls) if dark else cam.settings.controls
    rows = []
    for L in logical:
        saved = saved_set.get(L.key, {})
        mode = saved.get("mode") if saved.get("mode") in L.modes else "camera"
        value = saved["value"] if mode == "manual" and "value" in saved else L.value
        rows.append({**L.to_dict(), "mode": mode, "value": value,
                     "offset_value": saved.get("offset", 0) if mode == "software" else 0})
    return rows


def video_options(cam: Camera, devices: dict) -> list[dict]:
    has_identity = bool(cam.port or cam.device_id)
    opts = [dict(value="none", label="None (audio only)", selected=not cam.has_video)]
    matched = False
    for d in devices["video"]:
        sel = has_identity and (not cam.port or cam.port == d["port"]) \
            and (not cam.device_id or cam.device_id == d["device_id"])
        matched = matched or sel
        label = f"{d['name'] or d['node']} ({d['node']})"
        if d["camera"] and d["camera"] != cam.id:
            label += f" - used by {d['camera']}"
        opts.append(dict(value=f"dev:{d['port']}|{d['device_id']}", label=label, selected=sel))
    if has_identity and not matched:
        opts.append(dict(value=f"dev:{cam.port}|{cam.device_id}", selected=True,
                         label=f"Unplugged: {cam.device_id or cam.port}"))
    opts.append(dict(value="src", label="URL / device path...",
                     selected=bool(cam.source) and not has_identity))
    return opts


def audio_options(cam: Camera, devices: dict) -> list[dict]:
    opts = [dict(value="none", label="None", selected=not cam.audio_source)]
    matched = False
    for d in devices["audio"]:
        sel = cam.audio_source == d["key"]
        matched = matched or sel
        label = d["name"]
        if d["camera"] and d["camera"] != cam.id:
            label += f" - shared with {d['camera']}"
        opts.append(dict(value=d["key"], label=label, selected=sel))
    custom = cam.audio_source.startswith("alsa:")
    if cam.audio_source and not matched and not custom:
        opts.append(dict(value=cam.audio_source, selected=True,
                         label=f"Unplugged: {cam.audio_source.partition('+')[2] or cam.audio_source}"))
    opts.append(dict(value="alsa", label="ALSA device name...", selected=custom))
    return opts
