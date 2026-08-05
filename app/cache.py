"""Tiny in-process TTL cache for read-only S3 lookups (schema introspection,
spine-dimension bounds) — see the callers in app/engine.py for why: polars
re-resolves a source's schema (and, for an unbounded spine query, its min/
max) from S3 on every call, and several independent code paths (editor
introspection, the measure editor, every query a dashboard tile runs)
routinely ask the same question moments apart.

A short TTL, not an invalidation-tracked cache: the backing data can change
(new rows land in a parquet file) without this process finding out, so
entries expire on their own rather than being trusted indefinitely.
app/registry.py's reload_all() also calls clear() directly on every model/
bundle edit, since that changes what a path *resolves to*, not just its
data, and shouldn't have to wait out a TTL.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Hashable, TypeVar

_T = TypeVar("_T")

_lock = threading.Lock()
_store: dict[Hashable, tuple[float, object]] = {}


def get_or_set(key: Hashable, ttl: float, compute: Callable[[], _T]) -> _T:
    """`key`'s cached value if it was stored within the last `ttl` seconds,
    else call `compute()`, store the result under `key`, and return that.

    Concurrent misses on the same key each just run `compute()` on their
    own — the lock never spans it, so one slow S3 round trip can't stall
    unrelated lookups, at the cost of occasionally doing that round trip
    twice. A miss also sweeps already-expired entries, so a cache fed a
    steady stream of one-off keys (e.g. distinct ad hoc filter combinations)
    stays bounded to roughly what's been touched in the last `ttl` seconds
    rather than growing forever.
    """
    now = time.monotonic()
    with _lock:
        hit = _store.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
    value = compute()
    with _lock:
        _store[key] = (now + ttl, value)
        for k in [k for k, (expires_at, _) in _store.items() if expires_at <= now]:
            del _store[k]
    return value


def clear() -> None:
    with _lock:
        _store.clear()
