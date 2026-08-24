"""The DuckDB runtime: one connection, its extensions, its S3 credentials, and
the two caches that decide how many round trips a query costs.

Everything the platform reads goes through a single process-wide connection.
That is not an optimization detail, it is the whole latency design: DuckDB's
parquet-metadata cache, its external file cache and its keep-alive HTTP
connections all live on the *instance*, so a second connection would start
cold and a connection per query would make every one of them useless. Callers
get short-lived cursors off that one instance instead (see `cursor()`), which
share its caches while keeping per-query state separate.

What the polars engine had to build in Python — a listing cache, a whole-frame
cache under a byte cap — DuckDB mostly does itself. Two things it can't know
survive here:

  - **which objects a glob resolves to**, because handing `read_parquet` an
    already-resolved file list is what stops it re-LISTing the prefix on every
    query; and
  - **which sources are small enough to be worth holding locally**, which is a
    policy question (the lookup tables in `joins:` and the datasets behind a
    dimension bundle are read by every query that touches them and are usually
    kilobytes) rather than something a cache-eviction heuristic will get right.
"""
from __future__ import annotations

import fnmatch
import re
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import duckdb

from . import cache, config, s3

# Extensions are loaded from pinned wheels on disk, never downloaded. See
# _extension_path: `INSTALL` would reach extensions.duckdb.org at runtime,
# which is a network dependency in the request path, a version that can drift
# from the pinned duckdb itself, and a hard failure in any deployment without
# egress. The wheels are ordinary requirements.txt entries instead.
#
# Order matters: iceberg's init needs avro already loaded, and both need
# httpfs to reach an s3:// path.
EXTENSIONS = ("httpfs", "avro", "iceberg", "delta")

# Which extension each source format needs, for the error message when one is
# missing — "install duckdb-extension-delta" beats a DuckDB catalog error
# naming a function nobody wrote.
FORMAT_EXTENSION = {"delta": "delta", "iceberg": "iceberg"}

_lock = threading.Lock()
_con: Optional[duckdb.DuckDBPyConnection] = None
_secret_key: Optional[tuple] = None
_loaded: set[str] = set()
# tables pinned by _pin_source, so invalidate() can drop them again
_pinned: set[str] = set()


class DuckError(RuntimeError):
    """A runtime problem with DuckDB itself — a missing extension, an
    unreadable source — as opposed to a bad query."""


# ── extensions ───────────────────────────────────────────────────────────

def _extension_path(name: str) -> Optional[Path]:
    """The installed wheel's copy of `name`, or None if that package isn't
    installed. `duckdb_extension_httpfs` lays its binary out as
    `extensions/<duckdb version>/httpfs.duckdb_extension`; the version
    directory is globbed rather than assumed so a patch-level duckdb bump
    doesn't need a code change here."""
    try:
        import importlib.util

        spec = importlib.util.find_spec(f"duckdb_extension_{name}")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    root = Path(list(spec.submodule_search_locations)[0]) / "extensions"
    for candidate in sorted(root.glob(f"*/{name}.duckdb_extension"), reverse=True):
        return candidate
    return None


def _load_extensions(con: duckdb.DuckDBPyConnection) -> None:
    """Load every available extension, remembering which made it.

    A missing one is not fatal: parquet and csv are compiled into duckdb
    itself, so a deployment that reads only those works with no extension
    wheels at all. What it must not do is fail later with a confusing catalog
    error — see require_format."""
    for name in EXTENSIONS:
        path = _extension_path(name)
        if path is None:
            continue
        try:
            con.execute(f"LOAD '{path}'")
            _loaded.add(name)
        except duckdb.Error:
            pass    # a wheel that won't load is the same case as an absent one


def require_format(fmt: str) -> None:
    """Raise if `fmt` needs an extension this process doesn't have, naming the
    package to install. Called before a scan is built, so the failure arrives
    at the model that asked for it rather than three layers down."""
    needed = FORMAT_EXTENSION.get(fmt)
    if needed and needed not in _loaded:
        raise DuckError(
            f"source format '{fmt}' needs the duckdb '{needed}' extension, which is "
            f"not installed — add `duckdb-extension-{needed}` to requirements.txt"
        )


def loaded_extensions() -> set[str]:
    connection()        # loading is part of opening the connection
    return set(_loaded)


# ── credentials ──────────────────────────────────────────────────────────

def _install_secret(con: duckdb.DuckDBPyConnection) -> None:
    """(Re)create the S3 secret from the currently resolved credentials.

    Re-resolved rather than cached for the same reason app/s3.py re-resolves:
    an AWS_PROFILE credential, an SSO one above all, expires and gets refreshed
    by asking boto3 again. The secret is only rewritten when the resolved
    values actually change, so the common static-key case costs one dict
    comparison per query rather than a DDL statement."""
    global _secret_key
    access_key, secret_key, session_token = config.resolve_credentials()
    endpoint = urlparse(config.S3_ENDPOINT)
    key = (access_key, secret_key, session_token, config.S3_ENDPOINT, config.AWS_REGION)
    if key == _secret_key:
        return
    host = endpoint.netloc or config.S3_ENDPOINT
    parts = [
        "TYPE s3",
        f"KEY_ID {_sql_str(access_key)}",
        f"SECRET {_sql_str(secret_key)}",
        f"REGION {_sql_str(config.AWS_REGION)}",
        f"ENDPOINT {_sql_str(host)}",
        # path style and plain http for the emulator (and for MinIO, which is
        # the same shape); a real endpoint over https ignores both because its
        # url style is vhost and its scheme says https.
        f"URL_STYLE {_sql_str('path' if _is_local_endpoint(host) else 'vhost')}",
        f"USE_SSL {'true' if endpoint.scheme == 'https' else 'false'}",
    ]
    if session_token:
        parts.append(f"SESSION_TOKEN {_sql_str(session_token)}")
    con.execute(f"CREATE OR REPLACE SECRET cash_intel_s3 ({', '.join(parts)})")
    _secret_key = key


def _is_local_endpoint(host: str) -> bool:
    """Path-style addressing for anything that isn't real S3. Virtual-host
    style needs a wildcard DNS entry per bucket, which moto, MinIO and
    LocalStack don't have; real S3 prefers vhost and is the only thing here
    that gets it."""
    name = host.split(":")[0]
    return not name.endswith("amazonaws.com")


def _sql_str(value: str) -> str:
    """A SQL string literal. Used only for values this module owns
    (credentials, paths it resolved itself) — never for anything a request
    body supplied, which is bound as a parameter instead."""
    return "'" + str(value).replace("'", "''") + "'"


# ── the connection ───────────────────────────────────────────────────────

# Settings that decide what a query costs against a real endpoint. Set once,
# on the one connection, because that is where the caches they turn on live.
_SETTINGS = {
    # parquet footers, cached process-wide: the single biggest win, since a
    # footer read is a round trip that returns no data
    "enable_object_cache": "true",
    # file *bytes*, cached block-level with an LRU: what replaces the polars
    # engine's whole-frame cache for everything above the pin threshold
    "enable_external_file_cache": "true",
    "enable_http_metadata_cache": "true",
    # no TCP+TLS handshake per range read
    "http_keep_alive": "true",
}


def _apply_settings(con: duckdb.DuckDBPyConnection) -> None:
    for name, value in _SETTINGS.items():
        try:
            con.execute(f"SET {name} = {value}")
        except duckdb.Error:
            pass    # a setting a future duckdb renamed: not worth failing over
    for name, value in (
        ("http_retries", config.HTTP_RETRIES),
        ("http_timeout", config.HTTP_TIMEOUT_MS),
        ("threads", config.DUCKDB_THREADS),
        ("memory_limit", config.DUCKDB_MEMORY_LIMIT),
    ):
        if value:
            try:
                con.execute(f"SET {name} = {_sql_str(value) if isinstance(value, str) else value}")
            except duckdb.Error:
                pass


def connection() -> duckdb.DuckDBPyConnection:
    """The process's DuckDB connection, opened on first use.

    One per process by design — see the module docstring. Callers that run a
    query want cursor() below instead; this is for the setup a cursor
    inherits."""
    global _con
    with _lock:
        if _con is None:
            con = duckdb.connect(config.DUCKDB_PATH)
            _apply_settings(con)
            _load_extensions(con)
            _con = con
        _install_secret(_con)
        return _con


def cursor() -> duckdb.DuckDBPyConnection:
    """A short-lived cursor over the process connection.

    DuckDB connections aren't safe to use concurrently, but cursors off one
    connection are — and they share the instance's caches, which is the entire
    reason there is only one instance. Every query path takes one of these."""
    return connection().cursor()


def reset() -> None:
    """Drop the connection and everything cached in it. Tests use this between
    bucket fixtures; nothing in the request path does."""
    global _con, _secret_key
    with _lock:
        if _con is not None:
            _con.close()
        _con, _secret_key = None, None
        _loaded.clear()
        _pinned.clear()


# ── object listings ──────────────────────────────────────────────────────

def split_s3(path: str) -> Optional[tuple[str, str]]:
    """(bucket, key) for an s3:// url, or None for anything else — a local
    path in a test, or a form this module shouldn't be second-guessing."""
    if not path.startswith("s3://"):
        return None
    bucket, _, key = path[len("s3://"):].partition("/")
    return (bucket, key) if bucket else None


def glob_match(pattern: str, key: str) -> bool:
    """Does object key `key` match glob `pattern`?

    Matched one `/`-delimited segment at a time, so a `*` never crosses a
    separator and `**` matches any run of segments. Getting this wrong is not
    cosmetic: plain fnmatch would let `sales/*.parquet` swallow
    `sales/archive/old.parquet`, and this listing decides which files a query
    reads, so the extra rows would land in someone's revenue number.
    fnmatchcase because S3 keys are case-sensitive and fnmatch honours the
    *host* filesystem's rules."""
    def match(pat: list[str], seg: list[str]) -> bool:
        if not pat:
            return not seg
        if pat[0] == "**":
            return any(match(pat[1:], seg[i:]) for i in range(len(seg) + 1))
        return bool(seg) and fnmatch.fnmatchcase(seg[0], pat[0]) and match(pat[1:], seg[1:])

    return match(pattern.split("/"), key.split("/"))


def _list_objects(path: str) -> Optional[tuple[list[str], int]]:
    """The objects `path` resolves to as (s3:// urls, total bytes), or None if
    that can't be determined cheaply.

    `path` may be a glob, a single object, or a table root (delta/iceberg), and
    one LIST answers all three. The byte total is the point as much as the file
    list: it is what lets _pin_source decide whether a source is worth holding
    locally *before* reading it.

    A table root's listing includes its log/metadata objects, so the total
    over-counts there. That errs toward not pinning, which is the safe way to
    be wrong."""
    split = split_s3(path)
    if split is None:
        return None
    bucket, key = split
    prefix = re.split(r"[*?\[]", key, maxsplit=1)[0]
    try:
        pages = s3.client().get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
        contents = [obj for page in pages for obj in page.get("Contents", [])]
    except Exception:
        return None    # no listing permission, wrong endpoint, ... — just scan
    matches = (
        [o for o in contents if glob_match(key, o["Key"])] if prefix != key
        else [o for o in contents if o["Key"] == key or o["Key"].startswith(key.rstrip("/") + "/")]
    )
    return ([f"s3://{bucket}/{o['Key']}" for o in sorted(matches, key=lambda o: o["Key"])],
            sum(o["Size"] for o in matches))


def objects(path: str) -> Optional[tuple[list[str], int]]:
    """_list_objects, once per path per SOURCE_CACHE_TTL rather than on every
    query that scans it."""
    if config.SOURCE_CACHE_TTL <= 0:
        return _list_objects(path)
    return cache.get_or_set(("duck_objects", path), config.SOURCE_CACHE_TTL,
                            lambda: _list_objects(path))


# ── relations ────────────────────────────────────────────────────────────

def _scan_sql(fmt: str, path: str) -> str:
    """The table-function call that reads one source.

    Parquet is handed an already-resolved file list when a listing is
    available: DuckDB would otherwise re-glob the prefix itself on every
    query, and that listing is a thing we just cached. Iceberg is handed its
    resolved metadata.json for the same reason and one more — it is what lets
    the read stay on the catalog-free convention rather than needing
    `unsafe_enable_version_guessing`, which globs the metadata directory and
    can pick up an uncommitted snapshot."""
    require_format(fmt)
    if fmt == "csv":
        return f"read_csv({_sql_str(path)})"
    if fmt == "delta":
        return f"delta_scan({_sql_str(path)})"
    if fmt == "iceberg":
        from . import iceberg_util

        return f"iceberg_scan({_sql_str(iceberg_util.resolve_metadata_path(path))})"
    listing = objects(path)
    if listing and listing[0]:
        files = ", ".join(_sql_str(url) for url in listing[0])
        return f"read_parquet([{files}])"
    return f"read_parquet({_sql_str(path)})"


def _pin_name(path: str, fmt: str) -> str:
    """A stable, collision-free table name for a pinned source. Hashed rather
    than derived from the path so an s3 key's slashes, dots and unicode can't
    produce something that needs quoting or that collides after sanitizing."""
    import hashlib

    digest = hashlib.sha1(f"{fmt}:{path}".encode()).hexdigest()[:16]
    return f"__pin_{digest}"


def _pin_source(path: str, fmt: str) -> Optional[str]:
    """`path` held as a local DuckDB table if it is small enough to be worth
    keeping, else None — meaning "read it from the object store like anything
    else".

    Two gates, guarding different things. The object-store byte total decides
    whether to read at all, so an oversized source is never downloaded just to
    discover it was oversized. The resident-size check afterwards decides
    whether to *keep* what was read, since columnar data expands when it
    decompresses and a source can pass the first gate and still be too big to
    hold.

    This is aimed at the sources that hurt most: a `joins:` lookup table and
    the datasets behind a dimension bundle are read by every query that touches
    them and are usually kilobytes. Large fact tables stay streamed, with
    pushdown intact."""
    listing = objects(path)
    if listing is None or not listing[0] or listing[1] > config.SOURCE_CACHE_MAX_BYTES:
        return None
    name = _pin_name(path, fmt)
    con = cursor()
    try:
        con.execute(f'CREATE OR REPLACE TABLE "{name}" AS SELECT * FROM {_scan_sql(fmt, path)}')
        resident = con.execute(
            "SELECT estimated_size FROM duckdb_tables() WHERE table_name = ?", [name]
        ).fetchone()
    except duckdb.Error:
        return None
    if resident and resident[0] and resident[0] > config.SOURCE_CACHE_MAX_RESIDENT_BYTES:
        con.execute(f'DROP TABLE IF EXISTS "{name}"')
        return None
    with _lock:
        _pinned.add(name)
    return name


def relation(path: str, fmt: str) -> str:
    """SQL naming one source's rows: a pinned local table when the source is
    small enough to have been held, otherwise the table-function call that
    reads it from the object store. Either way it is something the caller can
    select, filter and join exactly the same way."""
    if config.SOURCE_CACHE_TTL <= 0:
        return _scan_sql(fmt, path)
    name = cache.get_or_set(("duck_pin", path, fmt), config.SOURCE_CACHE_TTL,
                            lambda: _pin_source(path, fmt))
    return f'"{name}"' if name else _scan_sql(fmt, path)


def invalidate() -> None:
    """Drop everything derived from bucket contents: the listing cache, the
    pin decisions, and the pinned tables themselves.

    Called wherever the platform changes the bucket in a way this process can
    see — a model or bundle reload (a path can resolve somewhere new), a
    successful pipeline run, a dataset upload or delete. The TTL is only the
    backstop for changes made from outside the app."""
    with _lock:
        names = list(_pinned)
        _pinned.clear()
    if names and _con is not None:
        cur = _con.cursor()
        for name in names:
            try:
                cur.execute(f'DROP TABLE IF EXISTS "{name}"')
            except duckdb.Error:
                pass
    # the external file cache holds bytes read from paths that may now hold
    # different data; DuckDB has no per-path eviction, so drop the lot
    if _con is not None:
        for statement in ("PRAGMA clear_external_file_cache", "PRAGMA clear_cache"):
            try:
                _con.execute(statement)
                break
            except duckdb.Error:
                continue


# ── schema ───────────────────────────────────────────────────────────────

def relation_schema(sql: str) -> dict[str, str]:
    """Column name -> DuckDB type name for a relation, read from its metadata
    alone. `LIMIT 0` is what keeps this a footer read rather than a scan."""
    rel = cursor().sql(f"SELECT * FROM {sql} LIMIT 0")
    return dict(zip(rel.columns, [str(t) for t in rel.types]))


def source_schema(path: str, fmt: str) -> dict[str, str]:
    """Cached column schema for one source (its own, unjoined) — the
    footer-only read source introspection needs (dataset picker, dimension
    bundle editor), without re-hitting S3 for it on every keystroke. Keyed on
    the source's own path+format, exactly what determines the answer, so it is
    safe to share across every caller, mid-edit or not — unlike a model or
    bundle name, a source path never means two different things at once."""
    return cache.get_or_set(("duck_source_schema", path, fmt), config.SCHEMA_CACHE_TTL,
                            lambda: relation_schema(_scan_sql(fmt, path)))
