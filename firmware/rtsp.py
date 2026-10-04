"""RTSP server: live H.264 video, plus AAC audio when the camera has a microphone.

    rtsp://<host>:<rtsp_port>/<camera id>?token=<token>
    rtsp://<host>:<rtsp_port>/<token>/<camera id>         (for clients that drop the query)
    rtsp://admin:<token>@<host>:<rtsp_port>/<camera id>   (user "admin" or "root")

Nothing is encoded while nobody watches. The first viewer of a camera starts a
Publisher: one ffmpeg process fed with the camera's frames (the same ones the MJPEG
stream and the recorder get) and its microphone, sending RTP to two local UDP sockets.
Every viewer of that camera gets a copy of those packets, over its RTSP connection (TCP,
interleaved) or over UDP. A viewer joining a running stream starts at the next keyframe
(one per second). The encoder stops LINGER seconds after the last viewer left.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import queue
import re
import secrets
import select
import socket
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

import numpy as np

from .auth import REALM, USERS, failed, same, user_ok
from .devices import resolve_alsa
from .recorder import Encoder

if TYPE_CHECKING:
    from .service import Service

log = logging.getLogger(__name__)

LINGER = 10.0           # seconds the encoder keeps running after the last viewer left
START_TIMEOUT = 8.0     # first viewer: wait this long for the camera and the encoder
STALL_TIMEOUT = 10.0    # no frame from the camera for this long: end the stream
SESSION_TIMEOUT = 60    # announced to clients; UDP viewers must send keep-alives within it
TCP_BACKLOG = 2000      # packets queued for one TCP viewer before it counts as stuck
PKT_SIZE = 1200         # RTP packet size: fits a typical MTU, for UDP viewers
MAX_CONNECTIONS = 64

REASONS = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
           454: "Session Not Found", 455: "Method Not Valid in This State",
           461: "Unsupported Transport", 501: "Not Implemented", 503: "Service Unavailable"}
METHODS = "OPTIONS, DESCRIBE, SETUP, PLAY, PAUSE, TEARDOWN, GET_PARAMETER, SET_PARAMETER"


def new_token() -> str:
    """11 URL-safe characters, like a YouTube video id (64 random bits)."""
    return secrets.token_urlsafe(8)


class Unavailable(Exception):
    pass


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _starts_keyframe(pkt: bytes) -> bool:
    """Does this H.264 RTP packet start a keyframe (SPS or IDR slice)?"""
    if len(pkt) < 13:
        return False
    off = 12 + 4 * (pkt[0] & 0x0F)
    if pkt[0] & 0x10:                                   # header extension
        if len(pkt) < off + 4:
            return False
        off += 4 + 4 * int.from_bytes(pkt[off + 2:off + 4], "big")
    if len(pkt) <= off + 1:
        return False
    nal = pkt[off] & 0x1F
    if nal == 24:                                       # STAP-A: first aggregated unit
        return len(pkt) > off + 3 and (pkt[off + 3] & 0x1F) in (5, 7)
    if nal == 28:                                       # FU-A: start fragment
        return bool(pkt[off + 1] & 0x80) and (pkt[off + 1] & 0x1F) in (5, 7)
    return nal in (5, 7)


def _parse_sdp(text: str) -> list[list[str]]:
    """Media sections of ffmpeg's SDP, reduced to what viewers need."""
    tracks: list[list[str]] = []
    for line in text.splitlines():
        if line.startswith("m="):
            kind, _port, _proto, *fmts = line[2:].split()
            tracks.append([f"m={kind} 0 RTP/AVP {' '.join(fmts)}"])
        elif tracks and line.startswith(("a=rtpmap:", "a=fmtp:", "b=")):
            tracks[-1].append(line)
    return tracks


class Publisher:
    """The live encoder of one camera, while anyone watches it."""

    def __init__(self, server: RtspServer, cid: str) -> None:
        self.server, self.cid = server, cid
        self.worker = server.service.worker(cid)
        cam = server.service.camera(cid)
        src = cam.audio_source
        self.audio_source = src if src and resolve_alsa(src) else None
        self.lock = threading.Lock()
        self.users = 1                  # connections holding it; the creator is the first
        self.idle_since = time.monotonic()
        self.stopping = False
        self.sessions: set[Session] = set()
        self.tracks: list[list[str]] = []
        self.ready = threading.Event()
        self.dead = threading.Event()
        self._frames: queue.Queue[tuple[np.ndarray, float]] = queue.Queue(maxsize=2)
        self._enc: Encoder | None = None
        self._socks = []
        for _ in range(2 if self.audio_source else 1):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", 0))
            self._socks.append(s)
        self._tmp = tempfile.TemporaryDirectory(prefix="rtsp-")
        self._sdp = Path(self._tmp.name) / "stream.sdp"
        self._rx = threading.Thread(target=self._receive, name=f"rtsp-rx-{cid}", daemon=True)
        self._feeder = threading.Thread(target=self._feed, name=f"rtsp-{cid}", daemon=True)

    def start(self) -> None:
        self._rx.start()
        self._feeder.start()

    def stop(self) -> None:
        with self.lock:
            self.stopping = True

    def sdp(self, title: str) -> bytes:
        lines = ["v=0", f"o=- {int(time.time())} 1 IN IP4 0.0.0.0",
                 "s=" + (re.sub(r"[\r\n]", " ", title) or self.cid),
                 "c=IN IP4 0.0.0.0", "t=0 0", "a=control:*", "a=range:npt=0-"]
        for i, t in enumerate(self.tracks):
            lines += t + [f"a=control:trackID={i}"]
        return ("\r\n".join(lines) + "\r\n").encode()

    # -- threads ---------------------------------------------------------------

    def _on_frame(self, frame: np.ndarray, t: float) -> None:
        """Capture thread: hand over without ever blocking it; drop the oldest if behind."""
        try:
            self._frames.put_nowait((frame, t))
        except queue.Full:
            try:
                self._frames.get_nowait()
            except queue.Empty:
                pass
            self._frames.put_nowait((frame, t))

    def _on_audio(self, t: float, pcm: bytes) -> None:
        enc = self._enc
        if enc is not None:
            enc.write_audio(t, pcm)

    def _start_encoder(self, frame: np.ndarray, t: float) -> Encoder:
        s = self.server.service.camera(self.cid).settings
        target = self.worker.target_fps
        fps = max(1, round(min(self.worker.measured_fps or target, target)))
        out = ["-map", "0:v", "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
               "-crf", str(s.record_crf), "-pix_fmt", "yuv420p", "-g", str(fps),
               # SPS/PPS in the SDP and again before every keyframe, for late joiners
               "-flags:v", "+global_header", "-bsf:v", "dump_extra=freq=keyframe",
               "-f", "rtp", self._rtp_url(0)]
        if self.audio_source:
            out += ["-map", "1:a", "-c:a", "aac", "-b:a", "96k", "-f", "rtp", self._rtp_url(1)]
        out += ["-sdp_file", str(self._sdp)]
        enc = Encoder(f"rtsp {self.cid}", frame.shape[:2], fps, t, bool(self.audio_source), out)
        if self.audio_source:
            self.server.service.hub.subscribe(self.audio_source, self._on_audio)
        log.info("rtsp: %s encoder started (%dx%d, %d fps%s)", self.cid, frame.shape[1],
                 frame.shape[0], fps, ", audio" if self.audio_source else "")
        return enc

    def _rtp_url(self, track: int) -> str:
        port = self._socks[track].getsockname()[1]
        return f"rtp://127.0.0.1:{port}?rtcpport={port}&pkt_size={PKT_SIZE}"

    def _feed(self) -> None:
        reason = "no viewers"
        last_frame = time.monotonic()
        self.worker.add_frame_listener(self._on_frame)
        try:
            while True:
                try:
                    frame, t = self._frames.get(timeout=0.5)
                except queue.Empty:
                    frame = None
                now = time.monotonic()
                if frame is not None:
                    last_frame = now
                    if self._enc is None:
                        self._enc = self._start_encoder(frame, t)
                    elif frame.shape[:2] != self._enc.size:
                        reason = "resolution changed"
                        break
                    self._enc.write_video(frame, t)
                    if self._enc.failed:
                        reason = "encoder stopped"
                        break
                elif now - last_frame > STALL_TIMEOUT:
                    reason = "camera stopped delivering frames"
                    break
                if not self.ready.is_set() and self._sdp.exists():
                    tracks = _parse_sdp(self._sdp.read_text())
                    if len(tracks) == len(self._socks):
                        self.tracks = tracks
                        self.ready.set()
                with self.lock:
                    if self.stopping:
                        break
                    if self.users == 0 and now - self.idle_since > LINGER:
                        self.stopping = True
                        break
        except Exception:
            log.exception("rtsp: %s stream failed", self.cid)
            reason = "error"
        finally:
            with self.lock:
                self.stopping = True
            self.dead.set()
            self.worker.remove_frame_listener(self._on_frame)
            if self.audio_source and self._enc is not None:
                self.server.service.hub.unsubscribe(self.audio_source, self._on_audio)
            enc, self._enc = self._enc, None
            if enc is not None:
                enc.close()
                log.info("rtsp: %s encoder stopped (%s)", self.cid, reason)
            self._rx.join(timeout=2)
            for s in self._socks:
                s.close()
            self._tmp.cleanup()
            self.server._ended(self)

    def _receive(self) -> None:
        while not self.dead.is_set():
            ready, _, _ = select.select(self._socks, [], [], 0.5)
            for sock in ready:
                try:
                    pkt = sock.recv(65536)
                except OSError:
                    continue
                track = self._socks.index(sock)
                rtcp = len(pkt) > 1 and 200 <= pkt[1] <= 204
                key = track == 0 and not rtcp and _starts_keyframe(pkt)
                with self.lock:
                    sessions = list(self.sessions)
                for sess in sessions:
                    sess.deliver(track, rtcp, pkt, key)


class Session:
    """One viewer's RTSP session: which tracks go where, and whether it plays."""

    def __init__(self, conn: Connection, pub: Publisher) -> None:
        self.id = secrets.token_hex(8)
        self.conn, self.pub = conn, pub
        self.tracks: dict[int, tuple] = {}   # track: ("tcp", rtp ch, rtcp ch) | ("udp", rtp addr, rtcp addr)
        self.playing = False
        self.started = False                 # past the first keyframe

    def deliver(self, track: int, rtcp: bool, pkt: bytes, key: bool) -> None:
        if not self.playing:
            return
        if not self.started:
            if 0 in self.tracks and not key:
                return
            self.started = True
        dest = self.tracks.get(track)
        if dest is None:
            return
        if dest[0] == "tcp":
            self.conn.send_interleaved(dest[2] if rtcp else dest[1], pkt)
        else:
            self.conn.server.send_udp(rtcp, pkt, dest[2] if rtcp else dest[1])


class Connection:
    """One RTSP client connection: requests in, responses and interleaved RTP out."""

    def __init__(self, server: RtspServer, sock: socket.socket, addr) -> None:
        self.server, self.sock, self.ip = server, sock, addr[0]
        self._out: queue.Queue[bytes | None] = queue.Queue()
        self._closed = threading.Event()
        self.authed = False
        self._auth_warned = False
        self.pub: Publisher | None = None
        self.session: Session | None = None

    def run(self) -> None:
        writer = threading.Thread(target=self._write, name="rtsp-out", daemon=True)
        writer.start()
        rf = self.sock.makefile("rb")
        try:
            while not self._closed.is_set():
                req = self._read_request(rf)
                if req is None:
                    break
                self._handle(*req)
        except (OSError, ValueError):
            pass
        finally:
            self.close()
            self._end_session()
            if self.pub is not None:
                self.server.release(self.pub)
                self.pub = None
            writer.join(timeout=2)
            try:
                self.sock.close()
            except OSError:
                pass
            self.server._forget(self)

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._out.put(None)
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    # -- output ------------------------------------------------------------------

    def send_interleaved(self, channel: int, pkt: bytes) -> None:
        if self._closed.is_set():
            return
        if self._out.qsize() > TCP_BACKLOG:
            log.warning("rtsp: %s cannot keep up, disconnecting", self.ip)
            self.close()
            return
        self._out.put(b"$" + bytes([channel]) + len(pkt).to_bytes(2, "big") + pkt)

    def _write(self) -> None:
        try:
            while True:
                data = self._out.get()
                if data is None:
                    return
                self.sock.sendall(data)
        except OSError:
            self.close()

    def _reply(self, cseq: str, code: int, headers: list[tuple[str, str]] = (),
               body: bytes = b"") -> None:
        lines = [f"RTSP/1.0 {code} {REASONS[code]}", f"CSeq: {cseq}", "Server: camera-firmware"]
        lines += [f"{k}: {v}" for k, v in headers]
        if body:
            lines.append(f"Content-Length: {len(body)}")
        self._out.put(("\r\n".join(lines) + "\r\n\r\n").encode() + body)

    # -- input -------------------------------------------------------------------

    @staticmethod
    def _read_request(rf):
        while True:
            first = rf.read(1)
            if not first:
                return None
            if first == b"$":                   # interleaved packet from the client (RTCP)
                hdr = rf.read(3)
                if len(hdr) < 3:
                    return None
                rf.read(int.from_bytes(hdr[1:3], "big"))
                continue
            line = first + rf.readline(8192)
            if line.strip():
                break
        parts = line.decode(errors="replace").split()
        if len(parts) != 3:
            raise ValueError("bad request line")
        method, url, _version = parts
        headers: dict[str, str] = {}
        while True:
            raw = rf.readline(8192)
            if not raw:
                return None
            text = raw.decode(errors="replace").rstrip("\r\n")
            if not text:
                break
            k, _, v = text.partition(":")
            headers[k.strip().lower()] = v.strip()
            if len(headers) > 100:
                raise ValueError("too many headers")
        n = int(headers.get("content-length") or 0)
        if n > 65536:
            raise ValueError("body too large")
        if n:
            rf.read(n)
        return method.upper(), url, headers

    # -- requests ----------------------------------------------------------------

    def _handle(self, method: str, url: str, headers: dict[str, str]) -> None:
        cseq = headers.get("cseq", "0")
        if method == "OPTIONS":
            return self._reply(cseq, 200, [("Public", METHODS)])
        if method not in METHODS.split(", "):
            return self._reply(cseq, 501)
        cid, token, track = _parse_url(url)
        if not self._authorize(method, headers, token):
            nonce = secrets.token_hex(16)
            self.server.nonces.append(nonce)
            return self._reply(cseq, 401, [
                ("WWW-Authenticate", f'Digest realm="{REALM}", nonce="{nonce}"'),
                ("WWW-Authenticate", f'Basic realm="{REALM}"')])
        sess = self.session
        if sess is not None and headers.get("session", sess.id).split(";")[0] != sess.id:
            return self._reply(cseq, 454)
        sess_hdr = [("Session", f"{sess.id};timeout={SESSION_TIMEOUT}")] if sess else []

        if method in ("GET_PARAMETER", "SET_PARAMETER"):        # keep-alives
            return self._reply(cseq, 200, sess_hdr)
        if method == "DESCRIBE":
            try:
                pub = self._publisher(cid)
            except KeyError:
                return self._reply(cseq, 404)
            except Unavailable:
                return self._reply(cseq, 503)
            base = urlsplit(url)
            base = f"{base.scheme}://{base.netloc}{base.path.rstrip('/')}/"
            return self._reply(cseq, 200, [("Content-Base", base),
                                           ("Content-Type", "application/sdp")],
                               pub.sdp(self.server.service.camera(pub.cid).name))
        if method == "SETUP":
            if sess is not None and cid and cid != sess.pub.cid:
                return self._reply(cseq, 455)
            try:
                pub = sess.pub if sess else self._publisher(cid)
            except KeyError:
                return self._reply(cseq, 404)
            except Unavailable:
                return self._reply(cseq, 503)
            track = 0 if track is None else track
            if track >= len(pub.tracks):
                return self._reply(cseq, 404)
            dest = self._transport(headers.get("transport", ""), track)
            if dest is None:
                return self._reply(cseq, 461)
            if sess is None:
                sess = self.session = Session(self, pub)
                with pub.lock:
                    pub.sessions.add(sess)
            sess.tracks[track] = dest[0]
            if dest[0][0] == "udp":         # nothing else would notice a vanished viewer
                self.sock.settimeout(SESSION_TIMEOUT * 2)
            return self._reply(cseq, 200, [("Transport", dest[1]),
                                           ("Session", f"{sess.id};timeout={SESSION_TIMEOUT}")])
        if sess is None:
            return self._reply(cseq, 454)
        if method == "PLAY":
            if not sess.playing:
                sess.started, sess.playing = False, True
                log.info("rtsp: %s watching %s (%s)", self.ip, sess.pub.cid,
                         "/".join(sorted({d[0] for d in sess.tracks.values()})))
            return self._reply(cseq, 200, sess_hdr + [("Range", "npt=0.000-")])
        if method == "PAUSE":
            sess.playing = False
            return self._reply(cseq, 200, sess_hdr)
        if method == "TEARDOWN":
            self._end_session()
            return self._reply(cseq, 200, sess_hdr)

    def _publisher(self, cid: str | None) -> Publisher:
        if not cid:
            raise KeyError(cid)
        if self.pub is not None and self.pub.cid == cid and not self.pub.dead.is_set():
            return self.pub
        cam = self.server.service.camera(cid)          # KeyError: no such camera
        if not cam.has_video:
            raise KeyError(cid)
        if self.pub is not None:
            self.server.release(self.pub)
            self.pub = None
        self.pub = self.server.acquire(cid)
        return self.pub

    def _end_session(self) -> None:
        sess, self.session = self.session, None
        if sess is None:
            return
        sess.playing = False
        with sess.pub.lock:
            sess.pub.sessions.discard(sess)
        log.info("rtsp: %s stopped watching %s", self.ip, sess.pub.cid)

    def _transport(self, header: str, track: int):
        """((kind, rtp dest, rtcp dest), Transport reply) for the first option we support."""
        for spec in header.split(","):
            parts = [p.strip() for p in spec.split(";")]
            proto = parts[0].upper()
            params = dict(p.split("=", 1) if "=" in p else (p.lower(), "") for p in parts[1:])
            if "multicast" in params:
                continue
            if proto == "RTP/AVP/TCP":
                a, b = _pair(params.get("interleaved"), 2 * track)
                return ("tcp", a, b), f"RTP/AVP/TCP;unicast;interleaved={a}-{b}"
            if proto in ("RTP/AVP", "RTP/AVP/UDP") and "client_port" in params \
                    and self.server.udp is not None:
                a, b = _pair(params["client_port"], 0)
                if not a:
                    continue
                p = self.server.port
                return (("udp", (self.ip, a), (self.ip, b)),
                        f"RTP/AVP;unicast;client_port={a}-{b};server_port={p}-{p + 1}")
        return None

    def _authorize(self, method: str, headers: dict[str, str], url_token: str | None) -> bool:
        if self.authed:
            return True
        token = self.server.token
        ok = False
        auth = headers.get("authorization", "")
        scheme, _, rest = auth.partition(" ")
        if url_token:
            ok = same(url_token, token)
        if not ok and token and scheme.lower() == "basic":
            try:
                user, _, pw = base64.b64decode(rest.strip()).decode().partition(":")
            except (ValueError, UnicodeDecodeError):
                user, pw = "", ""
            ok = user_ok(user, pw, token)
        elif not ok and token and scheme.lower() == "digest":
            p = {m[0].lower(): m[1] if m[1] else m[2]
                 for m in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]*))', rest)}
            if p.get("username") in USERS and p.get("realm") == REALM \
                    and p.get("nonce") in self.server.nonces:
                ha1 = _md5(f"{p['username']}:{REALM}:{token}")
                ha2 = _md5(f"{method}:{p.get('uri', '')}")
                if p.get("qop"):
                    want = _md5(f"{ha1}:{p['nonce']}:{p.get('nc', '')}:{p.get('cnonce', '')}:"
                                f"{p['qop']}:{ha2}")
                else:
                    want = _md5(f"{ha1}:{p['nonce']}:{ha2}")
                ok = hmac.compare_digest(want, p.get("response", ""))
        if ok:
            self.authed = True
            return True
        if url_token or auth:
            if not self._auth_warned:
                log.warning("rtsp: %s: wrong token", self.ip)
                self._auth_warned = True
            failed()
        return False


def _pair(value: str | None, default: int) -> tuple[int, int]:
    """'5000-5001' or '5000' -> (5000, 5001)."""
    try:
        a, _, b = (value or "").partition("-")
        a = int(a) if a else default
        return a, int(b) if b else a + 1
    except ValueError:
        return default, default + 1


def _parse_url(url: str) -> tuple[str | None, str | None, int | None]:
    """(camera id, token, track) from a request URL. Some clients append the track's
    control to the original URL, query and all ("...?token=x/trackID=1")."""
    m = re.search(r"trackID=(\d+)", url)
    track = int(m.group(1)) if m else None
    u = urlsplit(url)
    token = (parse_qs(u.query).get("token") or [None])[0]
    if token:
        token = token.split("/")[0]
    segs = [s for s in u.path.split("/") if s and not s.startswith("trackID=")]
    if len(segs) == 2:                  # /<token>/<camera id>
        return segs[1], token or segs[0], track
    return (segs[0] if len(segs) == 1 else None), token, track


class RtspServer:
    def __init__(self, service: Service) -> None:
        self.service = service
        cfg = service.server.config
        self.host, self.port = cfg.host, cfg.rtsp_port
        self.nonces: deque[str] = deque(maxlen=1000)
        self.udp: tuple[socket.socket, socket.socket] | None = None
        self._lock = threading.Lock()
        self._pubs: dict[str, Publisher] = {}
        self._conns: set[Connection] = set()
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None

    @property
    def token(self) -> str:
        return self.service.server.config.token     # read live: a new token applies at once

    def start(self) -> None:
        ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ls.bind((self.host, self.port))
        ls.listen(16)
        self._listener = ls
        try:
            pair = []
            for p in (self.port, self.port + 1):
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                pair.append(s)
                s.bind((self.host, p))
            self.udp = (pair[0], pair[1])
        except OSError as e:
            for s in pair:
                s.close()
            log.warning("rtsp: UDP ports %d-%d unavailable (%s); viewers must use TCP",
                        self.port, self.port + 1, e)
        self._thread = threading.Thread(target=self._accept, name="rtsp-accept", daemon=True)
        self._thread.start()
        log.info("rtsp: listening on %s:%d", self.host, self.port)

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        with self._lock:
            conns, pubs = list(self._conns), list(self._pubs.values())
        for c in conns:
            c.close()
        for p in pubs:
            p.stop()
        for p in pubs:
            p._feeder.join(timeout=5)
        if self.udp:
            for s in self.udp:
                s.close()
            self.udp = None

    def viewers(self, cid: str) -> int:
        with self._lock:
            pub = self._pubs.get(cid)
        if pub is None:
            return 0
        with pub.lock:
            return sum(1 for s in pub.sessions if s.playing)

    def _accept(self) -> None:
        ls = self._listener
        while True:
            try:
                sock, addr = ls.accept()
            except OSError:
                return              # closed by stop()
            with self._lock:
                if len(self._conns) >= MAX_CONNECTIONS:
                    sock.close()
                    continue
                conn = Connection(self, sock, addr)
                self._conns.add(conn)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            threading.Thread(target=conn.run, name=f"rtsp-{addr[0]}", daemon=True).start()

    def _forget(self, conn: Connection) -> None:
        with self._lock:
            self._conns.discard(conn)

    # -- publishers ----------------------------------------------------------------

    def acquire(self, cid: str) -> Publisher:
        """The running publisher of a camera (started if needed), once it is streaming.
        Every acquire needs a release."""
        with self._lock:
            pub = self._pubs.get(cid)
            if pub is not None:
                with pub.lock:
                    if pub.stopping:
                        pub = None
                    else:
                        pub.users += 1
            if pub is None:
                pub = self._pubs[cid] = Publisher(self, cid)
                pub.start()
        if not pub.ready.wait(START_TIMEOUT) or pub.dead.is_set():
            self.release(pub)
            raise Unavailable(cid)
        return pub

    def release(self, pub: Publisher) -> None:
        with pub.lock:
            pub.users -= 1
            if pub.users <= 0:
                pub.idle_since = time.monotonic()

    def _ended(self, pub: Publisher) -> None:
        """A publisher stopped: its viewers have to reconnect (and get a fresh one)."""
        with self._lock:
            if self._pubs.get(pub.cid) is pub:
                del self._pubs[pub.cid]
        with pub.lock:
            sessions = list(pub.sessions)
        for s in sessions:
            s.conn.close()

    def send_udp(self, rtcp: bool, pkt: bytes, addr) -> None:
        udp = self.udp
        if udp is None:
            return
        try:
            udp[1 if rtcp else 0].sendto(pkt, addr)
        except OSError:
            pass
