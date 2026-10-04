# Cameras and microphones

[Documentation](index.md) › Cameras and microphones

## Finding cameras

Every few seconds the firmware looks for video devices and microphones. Anything that no
saved camera uses shows up as a **new device**, with default settings. A webcam and its
own microphone (the same USB device) come as one. Nothing is saved until you change
something on it and press Save; from then on it is a saved camera with an id (used in
URLs, e.g. `cam0`).

Deleting a saved camera keeps its recordings. If its device is still attached, it comes
back as a new device.

## Recognising a camera

A USB camera is identified by its **port** (where it is plugged in, e.g.
`pci-0000:00:14.0-usb-0:2:1.0`) and its **device id** (model, and serial number if it
has one), not by `/dev/videoN`, which can change. Every time the camera is opened these are
looked up again, so unplugging and plugging back in works, also into another port.

| Saved | Then the camera is |
|---|---|
| port and device id | that device in that port |
| only device id | that device, in any port |
| only port | whatever is plugged into that port |

An unplugged camera is tried again every 3 seconds; the pages show it as not connected.

## Other sources

A camera can also be an **RTSP or HTTP URL** (another network camera) or a plain
**`/dev/videoN`** path. These stream, record and detect like a USB camera, but are not
recognised on replugging, and URL sources have no image controls and no microphone of
their own (a local microphone can still be chosen).

Add one at the bottom of the Config page, or with `POST /api/cameras`.

## Resolution and frame rate

The Config page offers the resolutions the camera reports, and for each the frame rates
it reports in any of its pixel formats. Many webcams do their top frame rate only as
compressed MJPEG and lower ones only uncompressed (YUYV); the firmware picks the format
that has the chosen mode, MJPEG when both do.

The frame rate belongs to the Light and Dark sets of [image controls](image-controls.md),
since a lower rate allows longer exposures at night.

## Microphones and audio

A camera's **audio source** is a microphone (recognised by port and device id, like
cameras) or an ALSA device name (`alsa:plughw:CARD=...`). Several cameras can share one
microphone: it is opened once, while any camera records or any RTSP viewer watches.

Audio goes into recordings and RTSP streams as AAC, aligned with the video. If the
microphone stops delivering, silence fills the gap so the video carries on.

A camera with a microphone but no video is an **audio-only camera**: it records `.m4a`
files, always or not at all.

## Orientation

Rotation (90°, 180°, 270°) and horizontal and vertical flips are applied to the frames
themselves, so the live view, recordings, snapshots, streams and detection all see the
turned picture.
