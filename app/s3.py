"""Shared boto3 S3 client factory, one client per object store.

Which store a bucket belongs to is config's rule, not this module's (see
config.store_for): the demo bucket is served by the demo store — normally the
embedded emulator — and everything else by the primary one. Callers name a
bucket, or name nothing and get the primary store's client.

Credentials are re-resolved on every call (Store.credentials), so an
AWS_PROFILE-derived credential gets its chance to refresh rather than going
stale — that part is deliberate and must not be cached away.

The *client object* is reused, though, keyed on the store plus those resolved
credentials. Constructing one costs ~130ms the first time (botocore loads and
parses the service model) and ~6ms every time after, which is fine for a
one-off admin call and not fine on the query path — app/duck.py resolves a
source's object listing through here. Keying on the credentials rather than
on the static config is what keeps the two goals compatible: with static keys
the key never changes and the client is built once, while a rotating SSO
credential changes the key and gets a client built for the new one. boto3's
low-level clients are thread-safe, so one instance serves every request.
"""
from __future__ import annotations

import threading
from typing import Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from . import config

_lock = threading.Lock()
# keyed by store+credential rather than a single slot: with two stores in play
# there are two live clients, and a process running long enough to rotate an
# SSO credential many times must still not accumulate one per rotation
_clients: dict[tuple, object] = {}
_regions: dict[str, str] = {}


def client(bucket: Optional[str] = None):
    """A boto3 S3 client for the store that owns `bucket` (the primary store
    when no bucket is named)."""
    return client_for(config.store_for(bucket) if bucket else config.primary_store(), bucket)


def client_for(store: config.Store, bucket: Optional[str] = None):
    """A boto3 S3 client for `store`, signed for `bucket`'s own region when
    that is knowable and different from the store's default — the thing that
    keeps a bucket outside AWS_REGION from answering every request with a
    redirect nobody follows."""
    access_key, secret_key, session_token = store.credentials()
    region = bucket_region(store, bucket) if bucket else store.region
    key = (store, region, access_key, secret_key, session_token)
    with _lock:
        cached = _clients.get(key)
    if cached is not None:
        return cached
    made = boto3.client(
        "s3",
        endpoint_url=store.endpoint or None,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        aws_session_token=session_token,
        region_name=region,
    )
    with _lock:
        # a concurrent caller may have built one for this key already; the two
        # are equivalent, so keep whichever landed first and hand it back
        return _clients.setdefault(key, made)


def bucket_region(store: config.Store, bucket: str) -> str:
    """`bucket`'s actual AWS region, or the store's configured one if that
    can't be determined.

    Worth one API call, once per process, because getting it wrong is not a
    warning: SigV4 puts the region in the credential scope, so a bucket in
    eu-west-2 signed for us-east-1 is refused outright, and a request sent to
    the wrong regional endpoint comes back as a redirect DuckDB reports as a
    flat HTTP error. Configuring AWS_REGION correctly avoids all of that —
    but nobody finds out they haven't until the first read fails, and this
    removes the whole class of failure instead.

    Only asked of real AWS. An emulator or a MinIO has one endpoint serving
    every bucket, so its configured region is the answer by construction.
    """
    if not store.aws:
        return store.region
    with _lock:
        known = _regions.get(bucket)
    if known is not None:
        return known
    region = store.region
    try:
        access_key, secret_key, session_token = store.credentials()
        probe = boto3.client("s3", aws_access_key_id=access_key,
                             aws_secret_access_key=secret_key,
                             aws_session_token=session_token,
                             region_name=store.region)
        # GetBucketLocation answers from any region, and returns null for
        # us-east-1 — the one region S3 represents as "no constraint".
        located = probe.get_bucket_location(Bucket=bucket).get("LocationConstraint")
        region = located or "us-east-1"
    except (ClientError, BotoCoreError) as exc:
        # No s3:GetBucketLocation is normal for a scoped, read-only identity.
        # The header on the error response carries the answer anyway in the
        # cases that matter (a redirect names the right region), so take it
        # when it's there and otherwise trust what was configured.
        response = getattr(exc, "response", None) or {}
        headers = response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
        region = headers.get("x-amz-bucket-region") or store.region
    with _lock:
        _regions[bucket] = region
    return region


def browsable_buckets() -> list[tuple[str, str]]:
    """(bucket, prefix) pairs that dataset discovery walks, in display order.

    Normally one. Two when a real store is configured next to the demo one —
    and listing both is what makes the demo datasets *visible* alongside real
    ones rather than merely readable, so the built-in models still show what
    they read."""
    pairs = [(config.BUCKET, config.BUCKET_PREFIX)]
    if config.stores_split():      # false when the demo is off
        pairs.append((config.DEMO_BUCKET, ""))
    return pairs


def walk(bucket: str, prefix: str = "", limit: Optional[int] = None) -> tuple[list[dict], bool]:
    """Objects under `bucket`/`prefix` as [{"key", "size", "modified"}], plus
    whether the walk stopped early.

    Bounded on purpose. S3 returns 1000 keys per round trip, so an unbounded
    walk of a real bucket is not a slow page, it is a page that never
    finishes — and dataset discovery runs it on every visit to the Modelling
    workspace. Past config.LIST_MAX_KEYS the caller reports what it has and
    says so; CI_BUCKET_PREFIX is how a deployment points the walk at the part
    of a shared bucket it actually owns.
    """
    cap = config.LIST_MAX_KEYS if limit is None else limit
    pages = client(bucket).get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=prefix, PaginationConfig={"MaxItems": cap + 1})
    objects: list[dict] = []
    for page in pages:
        for obj in page.get("Contents", []):
            if len(objects) >= cap:
                return objects, True
            objects.append({
                "key": obj["Key"],
                "size": obj["Size"],
                "modified": obj["LastModified"].isoformat(timespec="seconds"),
            })
    return objects, False


def reset() -> None:
    """Drop every cached client and region. Tests use this when they move an
    endpoint; nothing in the request path does."""
    with _lock:
        _clients.clear()
        _regions.clear()
