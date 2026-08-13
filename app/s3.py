"""Shared boto3 S3 client factory (emulator or real endpoint).

Credentials are re-resolved on every call (config.resolve_credentials), so an
AWS_PROFILE-derived credential gets its chance to refresh rather than going
stale — that part is deliberate and must not be cached away.

The *client object* is reused, though, keyed on those resolved credentials.
Constructing one costs ~130ms the first time (botocore loads and parses the
service model) and ~6ms every time after, which is fine for a one-off admin
call and not fine on the query path — app/engine.py resolves a source's
object listing through here. Keying on the credentials rather than on the
static config is what keeps the two goals compatible: with static keys the
key never changes and the client is built once, while a rotating SSO
credential changes the key and gets a client built for the new one. boto3's
low-level clients are thread-safe, so one instance serves every request.
"""
from __future__ import annotations

import threading

import boto3

from . import config

_lock = threading.Lock()
# one slot, not a dict: only the current credential's client is ever wanted,
# and a process running long enough to rotate an SSO credential many times
# must not accumulate a client per rotation
_cached: tuple | None = None


def client():
    global _cached

    access_key, secret_key, session_token = config.resolve_credentials()
    key = (config.S3_ENDPOINT, config.AWS_REGION, access_key, secret_key, session_token)
    with _lock:
        cached = _cached
    if cached is not None and cached[0] == key:
        return cached[1]
    made = boto3.client(
        "s3",
        endpoint_url=config.S3_ENDPOINT,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        aws_session_token=session_token,
        region_name=config.AWS_REGION,
    )
    with _lock:
        # a concurrent caller may have built one for this key already; the two
        # are equivalent, so keep whichever landed first and hand it back
        if _cached is None or _cached[0] != key:
            _cached = (key, made)
        return _cached[1]
