"""Minimal, pluggable authoring credential for mutating model-measure routes.

This is deliberately not a real identity system: a single shared secret
(config.API_KEY) gates the ability to create/update/delete saved model
measures, paired with a self-declared author label used for provenance. It
is a placeholder for a real auth system later, not a claim of strong
per-user attribution — see specs/008-safe-measure-compilation/spec.md.
"""
from __future__ import annotations

import secrets

from fastapi import Header, HTTPException

from . import config


def require_measure_author(
    x_api_key: str = Header(default=""),
    x_author: str = Header(default=""),
) -> str:
    """FastAPI dependency: 401 if config.API_KEY is unset or the header
    doesn't match it (fail closed when unconfigured); 400 if the key is
    valid but no author label was given. Returns the author label."""
    if not config.API_KEY or not secrets.compare_digest(x_api_key, config.API_KEY):
        raise HTTPException(status_code=401, detail="missing or invalid X-API-Key")
    if not x_author.strip():
        raise HTTPException(status_code=400, detail="X-Author header is required")
    return x_author
