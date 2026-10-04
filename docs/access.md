# Access and security

[Documentation](index.md) › Access and security

## The token

One token protects everything: the pages, the API, snapshots, MJPEG and RTSP streams, and
recordings. It is made on first start: 11 random URL-safe characters, like a YouTube
video id. It is in `config.json` as `"token"`, and on the Config page under Global, where
**New** makes another one (it takes effect on Save).

Give it in whichever way the program at hand supports:

| How | Example | For |
|---|---|---|
| `?token=` on the URL | `https://camera1:8443/stream/cam0.mjpg?token=Ab3dE-fG_hI` | other programs, links |
| User name and password | user `admin` (or `root`), password = the token | programs that ask for both; `curl -u admin:<token>` |
| RTSP path | `rtsp://camera1:8554/<token>/cam0` | RTSP clients that drop `?query` |
| Login page | enter the token once | browsers |

In a browser, logging in (or opening any page once with `?token=`) sets a session cookie
that lasts a year. "Log out" in the top bar removes it.

- **Changing the token** logs out every other browser at once, and every URL with the old
  token stops working. The browser that made the change stays logged in.
- **A wrong token** costs a second before the answer, which makes guessing slow.
- The copy-ready URLs on the Live page already include the token.

## HTTPS

Next to plain HTTP the firmware serves HTTPS, on port 8443 (`https_port`), with a
**self-signed certificate**: one per device, made on first start in `tls/`, valid for ten
years, for the host name, `<host name>.local`, `localhost` and the IP addresses the machine
had at that moment. This is how most network cameras do it.

- Browsers warn the first time, because nobody vouches for the certificate. To be sure it
  is yours, compare the SHA-256 fingerprint the browser shows with the one on the Config
  page (Global › HTTPS), then accept it.
- Opening a page over HTTP sends the browser to HTTPS (`http_redirect`).
- Snapshots, streams and the API stay available over plain HTTP too, for programs that do
  not accept a self-signed certificate. Most can be told to: `curl -k`, ffmpeg and OBS
  accept it as is.
- After a change of host name or address, delete `tls/` and restart for a new certificate.

## What is encrypted

| Traffic | Encrypted |
|---|---|
| Pages, API, snapshots, MJPEG over HTTPS (port 8443) | yes |
| The same over HTTP (port of `port`) | no: the token can be read on the network |
| RTSP (port 8554) | no |

On your own network that is usually acceptable. **Do not forward these ports to the
internet.** For access from outside use a VPN (WireGuard, Tailscale and the like): the
camera stays unreachable from the internet and everything, RTSP included, travels
encrypted.

The web server is Flask's built-in one: fine for a handful of viewers on a local network,
not built to face the internet.
