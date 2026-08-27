"""Embedded moto S3 server: the demo store.

Runs whenever the demo bucket's endpoint is the built-in loopback address
(config.EMBEDDED_EMULATOR), which is the default and stays true even when a
real object store is configured for everything else — that is what keeps the
built-in demo catalog answering next to a real bucket rather than 404ing
against an account that has never heard of it.

Point CI_DEMO_S3_ENDPOINT at MinIO / LocalStack / a shared bucket to host the
demo data somewhere else, or set CI_DEMO=0 to switch the demo off entirely.
"""
from __future__ import annotations

from urllib.parse import urlparse

from . import config

_server = None


def start_if_embedded() -> bool:
    global _server
    if not config.EMBEDDED_EMULATOR or _server is not None:
        return False
    from moto.server import ThreadedMotoServer

    parsed = urlparse(config.DEMO_S3_ENDPOINT)
    _server = ThreadedMotoServer(ip_address=parsed.hostname, port=parsed.port, verbose=False)
    _server.start()
    return True


def stop() -> None:
    global _server
    if _server is not None:
        _server.stop()
        _server = None
