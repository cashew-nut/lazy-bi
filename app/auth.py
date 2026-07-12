"""Minimal, pluggable authoring credential for mutating model-measure routes.

This is deliberately not a real identity system: a single shared secret
(config.API_KEY) gates the ability to create/update/delete saved model
measures, paired with a self-declared author label used for provenance. It
is a placeholder for a real auth system later, not a claim of strong
per-user attribution — see specs/008-safe-measure-compilation/spec.md.
"""
from __future__ import annotations


def require_measure_author(x_api_key: str, author: str) -> str:
    raise NotImplementedError
