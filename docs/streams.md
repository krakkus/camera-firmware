# Streams

[Documentation](index.md) › Streams

Every camera with video is available in three forms. All need the token (see
[Access and security](access.md)); the Live page shows each URL ready to copy.

| What | URL | Format |
|---|---|---|
| Snapshot | `http(s)://<host>:<port>/frame/<id>.jpg?token=<token>` | the current frame, JPEG |
| MJPEG stream | `http(s)://<host>:<port>/stream/<id>.mjpg?token=<token>` | a JPEG per frame, no audio |
| RTSP stream | `rtsp://<host>:8554/<id>?token=<token>` | H.264, plus AAC audio with a microphone |

All three show the same picture as the live view, with rotation and image controls
applied. A snapshot answers 503 while the camera is not delivering.

## MJPEG

Works in any browser (an `<img>` tag) and in most programs that take an HTTP video URL.
It uses more bandwidth than RTSP. The JPEG quality is a per-camera setting (`jpeg_quality`).
When the camera stalls, the last frame is sent again every 2 seconds to keep the connection
open.

## RTSP

H.264 video and, when the camera has a microphone, AAC audio. The usual choice for VLC,
OBS, Home Assistant, Frigate, Blue Iris and other NVR software.

```
rtsp://<host>:8554/<id>?token=<token>
rtsp://<host>:8554/<token>/<id>              for clients that drop the ?query
rtsp://admin:<token>@<host>:8554/<id>        for clients that want a user name (or root)
```

- **On demand.** Nothing is encoded while nobody watches. The first viewer of a camera
  starts its encoder (about a second); every other viewer gets a copy of the same stream.
  The encoder stops 10 seconds after the last viewer leaves.
- **Joining.** A viewer joining a running stream starts at the next keyframe; there is one
  every second.
- **Transport.** TCP (over the RTSP connection) or UDP (ports 8554 and 8555), as the client
  prefers.
- **Reconnects.** When the camera is unplugged or its resolution changes, the stream ends
  and clients reconnect.
- Quality follows the camera's recording quality setting (`record_crf`).
- RTSP is not encrypted; see [what is encrypted](access.md#what-is-encrypted).

Turn RTSP off, or change its port, in `config.json` (`rtsp_enabled`, `rtsp_port`); the
switch is also on the Config page.

## In other programs

| Program | How |
|---|---|
| VLC | Media › Open Network Stream, the RTSP URL |
| OBS | Sources › Media Source, untick "Local file", the RTSP URL (or the MJPEG URL) |
| Home Assistant | the Generic Camera integration: still image = snapshot URL, stream = RTSP URL |
| ffmpeg | `ffmpeg -i "rtsp://<host>:8554/cam0?token=<token>" -c copy out.mp4` |
| A web page | `<img src="https://<host>:8443/stream/cam0.mjpg?token=<token>">` |

Anyone who has a URL with the token has access to everything, not just that stream.
