"""Flask UI, JSON API, and stream/frame endpoints."""
from __future__ import annotations

import re
from dataclasses import asdict
from datetime import datetime
from urllib.parse import urlencode, urlsplit

from flask import (Flask, Response, abort, jsonify, redirect, render_template, request,
                   send_from_directory)

from . import auth, tls
from .camera import Camera
from .daynight import location
from .camera_server import SEGMENT_CHOICES
from .config_form import (GROUPS, MANUAL_MODE, audio_options, control_rows, resolution_options,
                          video_options)
from .recordings import NAME, list_recordings
from .service import Service

BOUNDARY = "frame"
PAGES = {"index", "config_page", "recordings_page", "performance_page"}   # login redirects here
OPEN = {"login", "static"}          # reachable without the token
FOLDER = re.compile(r"^[A-Za-z0-9_-]+$")       # same characters as a camera id


def create_app(service: Service) -> Flask:
    app = Flask(__name__)

    @app.context_processor
    def common():
        return {"device_name": service.server.config.device_name}

    def camera_json(cam: Camera) -> dict:
        d = cam.to_dict()
        d["virtual"] = service.is_virtual(cam.id)
        d["has_video"] = cam.has_video
        d["status"] = service.worker(cam.id).status()
        d["status"]["rtsp_viewers"] = service.rtsp.viewers(cam.id) if service.rtsp else 0
        return d

    def recordings_folder(cid: str):
        if not FOLDER.match(cid):
            abort(404, "no such recordings folder")
        folder = service.recordings_dir / cid
        if not folder.is_dir():
            abort(404, f"no recordings for {cid}")
        return folder.resolve()

    def video_camera(cid: str) -> Camera:
        cam = service.camera(cid)
        if not cam.has_video:
            abort(404, "this camera has no video (audio only)")
        return cam

    # --- token -------------------------------------------------------------

    def token() -> str:
        return service.server.config.token

    def with_session(resp: Response) -> Response:
        resp.set_cookie(auth.COOKIE, auth.session_value(token()), max_age=auth.COOKIE_MAX_AGE,
                        httponly=True, samesite="Lax", secure=request.is_secure)
        return resp

    def host_name() -> str:
        host = urlsplit(request.host_url).hostname or "localhost"
        return f"[{host}]" if ":" in host else host

    def without_token(url_args) -> str:
        """The current page's URL minus ?token= (so it does not linger in the address bar)."""
        rest = [(k, v) for k, v in url_args.items(multi=True) if k != "token"]
        return request.path + ("?" + urlencode(rest) if rest else "")

    @app.before_request
    def to_https():
        """Browsers opening a page over plain HTTP go to HTTPS; programs using streams and
        the API over HTTP are left alone (they may not accept a self-signed certificate)."""
        port = app.config.get("HTTPS_PORT")
        if (port and service.server.config.http_redirect and not request.is_secure
                and request.method == "GET" and request.endpoint in PAGES | {"login"}):
            return redirect(f"https://{host_name()}:{port}{request.full_path.rstrip('?')}")
        return None

    @app.before_request
    def require_token():
        if request.endpoint in OPEN:
            return None
        t = token()
        given = request.args.get("token")
        basic = request.authorization
        if auth.same(given, t):
            if request.method == "GET" and request.endpoint in PAGES:
                return with_session(redirect(without_token(request.args)))   # log the browser in
            return None
        if basic and basic.type == "basic" and auth.user_ok(basic.username, basic.password, t):
            return None
        if auth.same(request.cookies.get(auth.COOKIE), auth.session_value(t)):
            return None
        if given or basic:
            auth.failed()
        if request.method == "GET" and request.endpoint in PAGES:
            return redirect("/login?" + urlencode({"next": request.full_path.rstrip("?")}))
        return Response("token required: ?token=..., or user admin with the token as password\n",
                        401, {"WWW-Authenticate": f'Basic realm="{auth.REALM}"'})

    def safe_next(url: str | None) -> str:
        return url if url and url.startswith("/") and not url.startswith("//") else "/"

    @app.route("/login", methods=["GET", "POST"])
    def login():
        nxt = safe_next(request.values.get("next"))
        if request.method == "POST":
            if auth.same(request.form.get("token", "").strip(), token()):
                return with_session(redirect(nxt))
            auth.failed()
            return render_template("login.html", next=nxt, error=True), 401
        return render_template("login.html", next=nxt, error=False)

    @app.get("/logout")
    def logout():
        resp = redirect("/login")
        resp.delete_cookie(auth.COOKIE)
        return resp

    @app.errorhandler(KeyError)
    def not_found(e):
        return jsonify(error=str(e.args[0])), 404

    @app.errorhandler(ValueError)
    def bad_request(e):
        return jsonify(error=str(e)), 400

    # --- UI ---------------------------------------------------------------

    def rtsp_base() -> str | None:
        """rtsp://<host this page was loaded from>:<port>, or None while RTSP is off."""
        if service.rtsp is None:
            return None
        return f"rtsp://{host_name()}:{service.server.config.rtsp_port}"

    @app.get("/")
    def index():
        cameras = service.cameras()
        return render_template("index.html", cameras=cameras, active="live",
                               virtual={c.id for c in cameras if service.is_virtual(c.id)},
                               rtsp_base=rtsp_base(), token=service.server.config.token)

    def https_info() -> dict | None:
        cert = app.config.get("TLS_CERT")
        if not cert:
            return None
        port = app.config["HTTPS_PORT"]
        return {"url": f"https://{host_name()}:{port}/", "http_port": service.server.config.port,
                "fingerprint": tls.fingerprint(cert), "secure": request.is_secure}

    @app.get("/config")
    def config_page():
        devices = service.devices()
        cameras = service.cameras()
        return render_template(
            "config.html", active="config", groups=GROUPS, devices=devices,
            manual_mode=MANUAL_MODE, segment_choices=SEGMENT_CHOICES,
            device=service.server.config, storage_candidates=service.storage.candidates(),
            location=location(service.server.config), https=https_info(),
            cameras=[dict(cam=c, settings=asdict(c.settings), virtual=service.is_virtual(c.id),
                          video_options=video_options(c, devices),
                          resolutions=resolution_options(c, service.modes_for(c)),
                          h264=service.h264_modes_for(c),
                          controls=control_rows(c, service.logical_controls(c)),
                          controls_dark=control_rows(c, service.logical_controls(c), dark=True),
                          audio_options=audio_options(c, devices)) for c in cameras])

    @app.get("/performance")
    def performance_page():
        return render_template("performance.html", active="performance",
                               sample_interval=service.metrics.interval,
                               has_gpu=service.metrics.has_gpu)

    @app.get("/metrics/history")
    def metrics_history():
        return jsonify(service.metrics.history())

    @app.get("/metrics/latest")
    def metrics_latest():
        return jsonify(service.metrics.history(since=request.args.get("since", type=float)))

    @app.get("/recordings")
    def recordings_page():
        selected = request.args.get("camera", "")
        limit = min(max(request.args.get("limit", 500, type=int), 1), 20000)
        cameras = service.cameras()
        recs, truncated = list_recordings(
            service.recordings_dir, {c.id: c.name for c in cameras}, selected, limit)
        return render_template("recordings.html", recordings=recs, truncated=truncated,
                               limit=limit, cameras=cameras, selected=selected,
                               tz=datetime.now().astimezone().tzname(), today=datetime.now().date(),
                               active="recordings")

    # --- API --------------------------------------------------------------

    @app.get("/api/device")
    def device():
        return jsonify({**asdict(service.server.config), "segment_choices": SEGMENT_CHOICES,
                        "location": location(service.server.config)})

    @app.patch("/api/device")
    def patch_device():
        data = request.get_json(force=True)
        unknown = set(data) - {"device_name", "segment_minutes", "storage_dir",
                               "prune_enabled", "min_free_percent", "rtsp_enabled", "token",
                               "latitude", "longitude"}
        if unknown:
            raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
        service.update_device(**data)
        resp = jsonify(asdict(service.server.config))
        if "token" in data:
            with_session(resp)          # the old session cookie no longer matches
        return resp

    @app.get("/api/storage")
    def storage():
        return jsonify({**service.storage.usage(), "candidates": service.storage.candidates()})

    @app.post("/api/storage/prune")
    def prune_now():
        result = service.storage.prune(force=True)
        return jsonify({**result.to_dict(), **service.storage.usage()})

    @app.get("/api/devices")
    def devices():
        return jsonify(service.devices())

    @app.get("/api/cameras")
    def list_cameras():
        return jsonify([camera_json(c) for c in service.cameras()])

    @app.post("/api/cameras")
    def add_camera():
        data = request.get_json(force=True)
        service.add_camera(Camera.from_dict(data))
        return jsonify(camera_json(service.camera(data["id"]))), 201

    @app.get("/api/cameras/<cid>")
    def get_camera(cid):
        return jsonify(camera_json(service.camera(cid)))

    @app.patch("/api/cameras/<cid>")
    def patch_camera(cid):
        data = request.get_json(force=True)
        settings = data.pop("settings", {})
        unknown = set(data) - {"name", "source", "enabled", "port", "device_id", "audio_source"}
        if unknown:
            raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
        return jsonify(camera_json(service.update_camera(cid, **data, **settings)))

    @app.post("/api/cameras/<cid>/reset")
    def reset_camera(cid):
        return jsonify(camera_json(service.reset_camera(cid)))

    @app.delete("/api/cameras/<cid>")
    def delete_camera(cid):
        service.remove_camera(cid)
        return "", 204

    # --- frame + stream -----------------------------------------------------

    @app.get("/frame/<cid>.jpg")
    def frame(cid):
        video_camera(cid)
        jpeg = service.worker(cid).latest_jpeg()
        if jpeg is None:
            abort(503, "no frame available yet")
        return Response(jpeg, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/stream/<cid>.mjpg")
    def stream(cid):
        video_camera(cid)
        worker = service.worker(cid)

        def gen():
            for jpeg in worker.stream_jpegs():
                yield (b"--" + BOUNDARY.encode() + b"\r\nContent-Type: image/jpeg\r\n"
                       b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")

        return Response(gen(), mimetype=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
                        headers={"Cache-Control": "no-store"})

    # --- recordings ---------------------------------------------------------

    @app.get("/api/cameras/<cid>/controls")
    def camera_controls(cid):
        cam = service.camera(cid)
        return jsonify(available=[l.to_dict() for l in service.logical_controls(cam)],
                       config=cam.settings.controls,
                       software=service.worker(cid).status()["controls"])

    @app.get("/api/cameras/<cid>/recordings")
    def recordings(cid):
        d = recordings_folder(cid)          # also works for a camera that no longer exists
        files = sorted([*d.glob("*.mp4"), *d.glob("*.m4a")], reverse=True)
        return jsonify([{"name": f.name, "size": f.stat().st_size,
                         "url": f"/recordings/{cid}/{f.name}"} for f in files])

    @app.get("/recordings/<cid>/<path:filename>")
    def recording_file(cid, filename):
        # Recordings outlive their camera (deleted, or re-added under another id), so this
        # must not require the camera to exist. Only plain folder names and files named
        # like our own recordings are served.
        folder = recordings_folder(cid)
        if not NAME.match(filename):
            abort(404, "not a recording")
        return send_from_directory(folder, filename,
                                   as_attachment=bool(request.args.get("download")))

    return app
