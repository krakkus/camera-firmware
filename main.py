"""Camera firmware entry point: python main.py [--config config.json]"""
import argparse
import logging
import ssl
import subprocess
import threading
from pathlib import Path

from werkzeug.serving import make_server

from firmware import tls
from firmware.camera_server import CameraServer
from firmware.service import Service
from firmware.web import create_app

log = logging.getLogger("main")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.json")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    server = CameraServer.load(args.config)
    if not Path(args.config).exists():
        server.save()          # write defaults so there's a file to edit
    service = Service(server)
    service.start()
    cfg = server.config
    app = create_app(service)
    # threaded: each MJPEG viewer holds a request thread open
    servers = [make_server(cfg.host, cfg.port, app, threaded=True)]
    log.info("http on %s:%d", cfg.host, cfg.port)
    if cfg.https_port:
        try:
            cert, key = tls.ensure(cfg.tls_dir, cfg.device_name)
            servers.append(make_server(cfg.host, cfg.https_port, app, threaded=True,
                                       ssl_context=tls.context(cert, key)))
            app.config["HTTPS_PORT"], app.config["TLS_CERT"] = cfg.https_port, cert
            log.info("https on %s:%d", cfg.host, cfg.https_port)
        except (OSError, subprocess.SubprocessError, ssl.SSLError) as e:
            log.error("https not available: %s", e)
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, name="https", daemon=True).start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for s in servers[1:]:
            s.shutdown()
        service.stop()


if __name__ == "__main__":
    main()
