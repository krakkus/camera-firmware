"""Self-signed HTTPS certificate, one per device (like most network cameras do).

Made with the openssl command on first start, for the host name, <host name>.local,
localhost and the device's current IP addresses. Browsers warn once, since nobody vouches
for it: compare the SHA-256 fingerprint the Config page shows with the one the browser
shows. Delete the files to get a new one (after an IP or name change, say).
"""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
import ssl
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

CERT, KEY = "cert.pem", "key.pem"
DAYS = 3650


def _local_ips() -> list[str]:
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    ips = []
    for word in out.split() + ["127.0.0.1", "::1"]:
        try:
            ips.append(str(ipaddress.ip_address(word.split("%")[0])))
        except ValueError:
            pass
    return list(dict.fromkeys(ips))


def ensure(directory: str | os.PathLike, name: str) -> tuple[Path, Path]:
    """The certificate and key in `directory`, made first if missing."""
    d = Path(directory)
    cert, key = d / CERT, d / KEY
    if cert.exists() and key.exists():
        return cert, key
    d.mkdir(parents=True, exist_ok=True)
    host = socket.gethostname()
    sans = [f"DNS:{h}" for h in dict.fromkeys([host, f"{host}.local", "localhost"])]
    sans += [f"IP:{ip}" for ip in _local_ips()]
    cn = "".join(c for c in name if c.isalnum() or c in " -_.")[:60] or host
    old = os.umask(0o077)                       # the key is readable by this user only
    try:
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec",
                        "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                        "-days", str(DAYS), "-subj", f"/CN={cn}",
                        "-addext", "subjectAltName=" + ",".join(sans),
                        "-keyout", str(key), "-out", str(cert)],
                       check=True, capture_output=True, timeout=30)
    finally:
        os.umask(old)
    log.info("https: made a self-signed certificate for %s", ", ".join(sans))
    return cert, key


def context(cert: Path, key: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


def fingerprint(cert: Path) -> str:
    """SHA-256 fingerprint as browsers show it (AB:CD:...)."""
    try:
        out = subprocess.run(["openssl", "x509", "-noout", "-fingerprint", "-sha256", "-in", str(cert)],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.strip().partition("=")[2]
