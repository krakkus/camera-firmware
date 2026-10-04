# Documentation

| Page | What is in it |
|---|---|
| [Installation](install.md) | Requirements, installing, running as a service, updating, the optional GPU chart and object detection |
| [Access and security](access.md) | The token, logging in, HTTPS and its certificate, what is and is not encrypted |
| [Web pages](pages.md) | The Live, Config, Recordings and Performance pages |
| [Cameras and microphones](cameras.md) | How cameras are found and recognised, new devices, URL sources, audio |
| [Streams](streams.md) | Snapshot, MJPEG and RTSP URLs, and how to use them in VLC, OBS, Home Assistant or an NVR |
| [Recording](recording.md) | Record modes, motion and object detection, files, storage and pruning |
| [Image controls](image-controls.md) | Camera, manual and software modes, the light and dark sets, frame rates, day/night switching |
| [Configuration reference](configuration.md) | Every setting in `config.json`, global and per camera |
| [HTTP API](api.md) | All endpoints, with examples |
| [Development](development.md) | Code layout, how the pieces fit together, known limits |

The firmware is one Python process. It finds the cameras and microphones attached to the
machine, captures each camera in its own thread, and from those frames serves the live
view and streams, records, detects motion or objects, and steers the image controls.
Everything is configured in the browser and saved to `config.json`.
