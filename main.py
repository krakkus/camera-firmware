"""Camera firmware entry point: python main.py [--config config.json]"""
import argparse
import logging
from pathlib import Path

from firmware.camera_server import CameraServer
from firmware.service import Service
from firmware.web import create_app


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
    try:
        # threaded: each MJPEG viewer holds a request thread open
        create_app(service).run(server.config.host, server.config.port, threaded=True)
    finally:
        service.stop()


if __name__ == "__main__":
    main()
