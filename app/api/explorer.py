"""Data explorer (bucket objects mapped to models) and health."""
from __future__ import annotations

from fastapi import APIRouter

from .. import config, llmclient, s3, semantic
from ..registry import registry

router = APIRouter(tags=["explorer"])


@router.get("/explorer")
def explorer():
    """Every object this app can see, matched against each model's source and
    join globs so the UI can show which files feed which models.

    Walks the same buckets, with the same bound, as GET /datasets — see
    app/s3.py's browsable_buckets and walk."""
    files, per_model, truncated = [], {name: {"files": 0, "bytes": 0} for name in registry.models}, False
    for bucket, prefix in s3.browsable_buckets():
        try:
            objects, cut = s3.walk(bucket, prefix)
        except Exception:
            continue      # an unreachable bucket costs its rows, not the page
        truncated = truncated or cut
        matchers = semantic.model_source_matchers(registry.models.values(), bucket)
        for name, stat in semantic.per_model_stats(objects, matchers, registry.models).items():
            per_model[name]["files"] += stat["files"]
            per_model[name]["bytes"] += stat["bytes"]
        files += [{
            "key": o["key"],
            "bucket": bucket,
            "size": o["size"],
            "modified": o["modified"],
            "models": [{"model": n, "role": r} for n, r, match in matchers if match(o["key"])],
        } for o in objects]
    primary = config.primary_store()
    return {
        "bucket": config.BUCKET,
        "endpoint": primary.label,
        "truncated": truncated,
        "files": files,
        "models": [
            {"name": m.name, "label": m.label,
             "datasets": [{"name": n, "path": ds.source.path, "format": ds.source.format}
                          for n, ds in m.datasets.items()],
             **per_model[m.name]}
            for m in registry.models.values()
        ],
    }


@router.get("/health")
def health():
    return {
        "ok": True, "models": list(registry.models),
        "s3_endpoint": config.primary_store().label,
        "bucket": config.BUCKET, "demo_bucket": config.DEMO_BUCKET if config.DEMO_ENABLED else "",
        "llm_enabled": config.LLM_ENABLED,
        "llm_models": config.LLM_MODEL_CHOICES if config.LLM_ENABLED else [],
        "llm_default_model": config.LLM_MODEL,
        # which of those models the THINKING toggle can be turned on for, and
        # the state it starts in — reported (in picker order) rather than
        # hardcoded in the UI, so the toggle can never offer a model that
        # would 400 on the parameter (app/config.py's LLM_THINKING_MODELS)
        "llm_thinking_models": (
            [m for m in config.LLM_MODEL_CHOICES if m in config.LLM_THINKING_MODELS]
            if config.LLM_ENABLED else []
        ),
        "llm_thinking_default": config.LLM_THINKING_DEFAULT,
        # which wire format CI_LLM_BASE_URL/CI_LLM_PROVIDER resolved to — the
        # first thing to check when a configured endpoint answers oddly. The
        # base URL itself is deliberately not reported: it can name an
        # internal host, and the provider name is what's diagnostic.
        "llm_provider": llmclient.configured_provider() if config.LLM_ENABLED else "",
        # the sandbox coding agent shares the same key, so it is configured
        # in exactly the deployments chat is — reported separately anyway so
        # the sandbox UI never has to reason about the chat feature's flag
        "sandbox_agent_enabled": config.LLM_ENABLED,
        "sandbox_agent_model": config.SANDBOX_AGENT_MODEL if config.LLM_ENABLED else "",
    }
