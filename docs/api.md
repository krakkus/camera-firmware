# HTTP API

[Documentation](index.md) › HTTP API

JSON in, JSON out. Every request needs the token (see [Access](access.md)); without it
the answer is 401. Errors are `{"error": "..."}` with status 400 (invalid) or 404 (not
found). Changes are checked completely before anything is applied, so a rejected request
changes nothing.

## Cameras

| Request | Purpose |
|---|---|
| `GET /api/cameras` | All cameras, saved and new devices, with settings and live status |
| `GET /api/cameras/<id>` | One camera |
| `POST /api/cameras` | Add a camera: `{"id", "name", "source" or "port"/"device_id", "audio_source", "settings"}` |
| `PATCH /api/cameras/<id>` | Change `name`, `enabled`, `source`, `port`, `device_id`, `audio_source` and/or `settings` (any subset). Changing a new device saves it |
| `POST /api/cameras/<id>/reset` | Settings back to the defaults (name and sources are kept) |
| `DELETE /api/cameras/<id>` | Delete a saved camera; recordings stay |
| `GET /api/cameras/<id>/controls` | Controls the camera offers (ranges, modes), the saved Light set, and the values the software loop has set now |
| `GET /api/cameras/<id>/recordings` | The camera's recording files |

The `status` of a camera includes: `connected`, `problem`, `fps` (measured), `recording`,
`motion`, `objects`, `controls` (software values now), `controls_ineffective`, `profile`
(`light` or `dark`), `light_level`, `sun` (today's switching times), `rtsp_viewers`, and
`audio`.

## Device

| Request | Purpose |
|---|---|
| `GET /api/device` | Global settings, plus the location used for the sun |
| `PATCH /api/device` | Change `device_name`, `segment_minutes`, `storage_dir`, `prune_enabled`, `min_free_percent`, `token`, `rtsp_enabled`, `latitude`/`longitude` (together; `null` for both: from the time zone) |
| `GET /api/devices` | Attached video devices and microphones, and which camera uses each |
| `GET /api/storage` | Disk usage of the storage location, and candidate locations |
| `POST /api/storage/prune` | Prune now |
| `GET /metrics/history` | Performance samples of the last ~24 h |
| `GET /metrics/latest?since=<time>` | Samples newer than a time |

## Media

| Request | Purpose |
|---|---|
| `GET /frame/<id>.jpg` | Current frame; 503 while there is none |
| `GET /stream/<id>.mjpg` | MJPEG stream |
| `GET /recordings/<id>/<file>` | A recording; `?download=1` to download |

See [Streams](streams.md) for RTSP.

## Examples

```sh
T=your-token
# all cameras
curl -u admin:$T http://camera1:5000/api/cameras

# record when a person or a cat is seen
curl -u admin:$T -X PATCH http://camera1:5000/api/cameras/cam0 \
  -H 'content-type: application/json' \
  -d '{"settings": {"record_mode": "object", "object_classes": ["person", "cat"]}}'

# night set: 10 fps, brightness by software aiming higher, switched by the sun
curl -u admin:$T -X PATCH http://camera1:5000/api/cameras/cam0 \
  -H 'content-type: application/json' \
  -d '{"settings": {"fps_dark": 10, "profile_switch": "sun",
       "controls_dark": {"brightness": {"mode": "software", "offset": 100}}}}'

# a snapshot
curl -o now.jpg "http://camera1:5000/frame/cam0.jpg?token=$T"
```
