# Configuration reference

[Documentation](index.md) › Configuration reference

All settings live in `config.json` (path set with `main.py --config`). The web pages and
the API change it; it is rewritten in one go on every change, so it is never half
written. Editing it by hand works while the service is stopped. It holds this machine's
device serials and the token, so it is not in git; `config.example.json` shows the format.

```json
{
  "config": { "device_name": "camera-server", "port": 5000, "...": "..." },
  "cameras": [ { "id": "cam0", "name": "Front door", "settings": { "...": "..." } } ]
}
```

Settings from older versions are converted when loaded.

## Global (`config`)

| Key | Default | Meaning |
|---|---|---|
| `device_name` | `camera-server` | Shown in the page header |
| `host` | `0.0.0.0` | Address the web server listens on; `127.0.0.1` for this machine only. Restart to change |
| `port` | `5000` | HTTP port. Restart to change |
| `https_port` | `8443` | HTTPS port; `0` turns HTTPS off. Restart to change |
| `http_redirect` | `true` | Send browsers opening a page over HTTP to HTTPS |
| `tls_dir` | `tls` | Folder for the HTTPS certificate and key |
| `token` | made on first start | The access token, 4-64 of `A-Z a-z 0-9 - _`. See [Access](access.md) |
| `rtsp_enabled` | `true` | RTSP server on or off |
| `rtsp_port` | `8554` | RTSP port (TCP); UDP viewers use it and the next one. Restart to change |
| `storage_dir` | `recordings` | Where recordings go, `<storage_dir>/<camera id>/`. Relative to the working directory |
| `segment_minutes` | `5` | Longest recording file, in minutes (the page offers 1, 3, 5, 10) |
| `prune_enabled` | `true` | Delete the oldest recordings when the disk gets full |
| `min_free_percent` | `10` | Free space (1-50 %) to keep when pruning |
| `latitude`, `longitude` | empty | Location for sunrise and sunset; empty: a city in the system time zone |

## Per camera (`cameras[]`)

| Key | Meaning |
|---|---|
| `id` | Letters, digits, `-` and `_`; used in URLs. Fixed once saved |
| `name` | Shown on the pages |
| `enabled` | Off: not opened, not recorded |
| `port`, `device_id` | A USB camera's identity, see [Cameras](cameras.md#recognising-a-camera) |
| `source` | Instead of port/device id: an `rtsp://` or `http://` URL, or `/dev/videoN` |
| `audio_source` | A microphone (`<port>+<device id>`), `alsa:<device>`, or empty for none |
| `settings` | Everything below |

### Picture

| Setting | Default | Meaning |
|---|---|---|
| `width`, `height` | `1280`, `720` | Resolution asked of the camera |
| `fps` | `30` | Frame rate of the Light set |
| `fps_dark` | `0` | Frame rate of the Dark set; `0`: the same as `fps` |
| `rotation` | `0` | `0`, `90`, `180` or `270` degrees, clockwise |
| `flip_horizontal`, `flip_vertical` | `false` | Mirror the picture |
| `jpeg_quality` | `80` | 1-100, for the live view, MJPEG and snapshots |

### Image controls

| Setting | Default | Meaning |
|---|---|---|
| `controls` | `{}` | The Light set: per control `{"mode": "camera" \| "manual" \| "software", "value": n, "offset": -100..100}`. `value` is for manual, `offset` for software. Controls not listed are on camera |
| `controls_dark` | `{}` | The Dark set, same format; empty: the same as `controls` |
| `profile_switch` | `off` | When the Dark set applies: `off`, `light` (by light level) or `sun` (by sunset and sunrise) |
| `dark_below`, `light_above` | `30`, `80` | Light levels (0-255) for `light` switching |
| `sun_offset` | `0` | Minutes (-180 to 180) the night starts after sunset and ends before sunrise |

Control names are the ones the camera reports, e.g. `exposure`, `white_balance`,
`brightness`, `contrast`, `saturation`, `sharpness`, `gamma`, `hue`, `gain`,
`backlight_compensation`, `power_line_frequency`, `exposure_dynamic_framerate`. Manual
values are checked against the camera's range when saved. See
[Image controls](image-controls.md).

### Recording

| Setting | Default | Meaning |
|---|---|---|
| `record_mode` | `off` | `off`, `continuous`, `motion` or `object` |
| `record_codec` | `x264` | `x264`: the firmware encodes the frames (software). `copy`: record the camera's own H.264 as it is (hardware), see [Recording](recording.md#hardware-copy-mode) |
| `record_crf` | `23` | H.264 quality, 0-51: lower is better and bigger. Software encoding only; also used for RTSP |
| `pre_roll_seconds` | `3` | Motion/object: seconds kept from before the trigger (0-30) |
| `post_roll_seconds` | `5` | Motion/object: seconds recorded after the last trigger (0-300) |
| `motion_threshold` | `25` | Per-pixel difference that counts as change (1-255) |
| `motion_min_area` | `500` | Changed pixels, at 320 px wide, that count as motion |
| `object_confidence` | `0.5` | Minimum detection confidence (0.05-0.95) |

See [Recording](recording.md).
