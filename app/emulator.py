"""Embedded moto S3 server for the fully-local demo mode.

Started only when CI_S3_ENDPOINT is not set (see config.EMBEDDED_EMULATOR);
point CI_S3_ENDPOINT at MinIO / LocalStack / real S3 to skip this entirely.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from . import config

_server = None


def start_if_embedded() -> bool:
    global _server
    if not config.EMBEDDED_EMULATOR or _server is not None:
        return False
    from moto.server import ThreadedMotoServer

    parsed = urlparse(config.S3_ENDPOINT)
    _server = ThreadedMotoServer(ip_address=parsed.hostname, port=parsed.port, verbose=False)
    _server.start()
    return True


def stop() -> None:
    global _server
    if _server is not None:
        _server.stop()
        _server = None
