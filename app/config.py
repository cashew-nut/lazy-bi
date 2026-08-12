"""Runtime configuration.

Everything defaults to a fully local demo: an embedded moto S3 emulator,
a bundled semantic-model directory and a sqlite db in the project root.
Point CI_S3_ENDPOINT at a real (or external emulator) endpoint to skip
the embedded server.
"""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# S3 / emulator
S3_ENDPOINT = os.environ.get("CI_S3_ENDPOINT", "http://127.0.0.1:9600")
EMBEDDED_EMULATOR = "CI_S3_ENDPOINT" not in os.environ
BUCKET = os.environ.get("CI_BUCKET", "cash-intel")
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "testing")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "testing")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Semantic models + persistence
MODELS_DIR = Path(os.environ.get("CI_MODELS_DIR", PROJECT_ROOT / "models"))
DIMENSIONS_DIR = Path(os.environ.get("CI_DIMENSIONS_DIR", PROJECT_ROOT / "dimensions"))
DB_PATH = Path(os.environ.get("CI_DB_PATH", PROJECT_ROOT / "cash_intel.db"))

# Disk cache for files uploaded through the app (POST /api/datasets/local,
# app/api/datasets.py) — gitignored, unlike models/dimensions/pipelines
# above. The embedded S3 emulator is in-memory and starts empty on every
# process restart (app/seed.py reseeds the whole demo bucket from scratch
# each time); an upload written only to that bucket would vanish with it.
# Mirrors data_cache/'s role for app/load_taxi.py: the durable copy lives on
# local disk, and app/seed.py re-uploads it to the fresh emulator on every
# start.
LOCAL_DATA_DIR = Path(os.environ.get("CI_LOCAL_DATA_DIR", PROJECT_ROOT / "local_data"))

# Pipelines (specs/014-polars-pipeline-module/) — hosted polars transformation
# scripts; a run executes in its own subprocess, killed if it outruns its
# timeout (runs are strictly serialized platform-wide, one at a time).
PIPELINES_DIR = Path(os.environ.get("CI_PIPELINES_DIR", PROJECT_ROOT / "pipelines"))

# Agents (specs/017-agent-skills-mcp-server/) — declared Agent/Skill bundles,
# same "editable YAML directory" shape as MODELS_DIR/PIPELINES_DIR above.
AGENTS_DIR = Path(os.environ.get("CI_AGENTS_DIR", PROJECT_ROOT / "agents"))
PIPELINE_TIMEOUT_DEFAULT = 600
PIPELINE_TIMEOUT_MAX = 3600

# Sandbox notebooks — ad hoc polars/python scratch scripts (app/sandbox.py).
# A run answers its HTTP request directly (no queue: read-only previews, so
# concurrent runs are safe) but still gets a hard, killable timeout like a
# pipeline run, just a much shorter default given its interactive purpose.
SANDBOX_TIMEOUT_DEFAULT = 30
SANDBOX_TIMEOUT_MAX = 120
SANDBOX_ROW_LIMIT = 200

# Hard cap on rows returned to the browser
MAX_ROWS = 10_000

# Read-only S3 lookup cache (app/cache.py) — schema introspection and
# spine-dimension bounds are re-derived from S3 far more often than the
# backing data actually changes (every query re-resolves its own schema,
# every editor keystroke re-checks a source's columns). Entries self-expire
# rather than being trusted indefinitely, since this process has no way to
# know when new data lands; registry.reload_all() also clears the cache
# outright on every model/bundle edit, since that changes what a path
# resolves to, not just its data.
SCHEMA_CACHE_TTL = float(os.environ.get("CI_SCHEMA_CACHE_TTL", "30"))
BOUNDS_CACHE_TTL = float(os.environ.get("CI_BOUNDS_CACHE_TTL", "30"))

# Instant cross-filter extracts (specs/016-instant-cross-filter/) — the
# per-tile size cap that decides whether a dashboard tile gets the
# round-trip-free treatment or silently stays on today's live query path.
# An extract is a client-side *cache*, not a render payload, so it is allowed
# past MAX_ROWS above; both limits are checked against the real response
# (FR-009) and either one tripping sends the tile back to live mode.
# research.md R5's proposed starting defaults, benchmarked against the 13M-row
# NYC taxi dataset before shipping (SC-002).
EXTRACT_MAX_ROWS = int(os.environ.get("CI_EXTRACT_MAX_ROWS", 150_000))
EXTRACT_MAX_BYTES = int(os.environ.get("CI_EXTRACT_MAX_BYTES", 25 * 1024 * 1024))

# Sessions — see specs/011-session-auth-rbac/. Idle/absolute lifetimes in
# days; the cookie's Secure flag is off by default because the demo runs on
# plain HTTP (set CI_COOKIE_SECURE=1 behind TLS).
SESSION_IDLE_DAYS = int(os.environ.get("CI_SESSION_IDLE_DAYS", "7"))
SESSION_MAX_DAYS = int(os.environ.get("CI_SESSION_MAX_DAYS", "30"))
COOKIE_SECURE = os.environ.get("CI_COOKIE_SECURE", "0") == "1"

# ── LLM provider ──────────────────────────────────────────────────────────
# Conversational analytics (specs/012-conversational-analytics/), the
# Composer and the sandbox coding agent all share one provider config. Off
# unless it is configured, so an unconfigured deployment never sends question
# text/schema/results to a third party (research.md R7).
#
# The intended way to point at anything other than Anthropic's own API is a
# URL and a key: CI_LLM_BASE_URL decides the wire format (app/llmclient.py's
# resolve_provider), CI_LLM_API_KEY authenticates, CI_LLM_MODEL names a model
# that endpoint actually serves. CI_LLM_PROVIDER overrides the detection for
# the one case a URL can't express — a self-hosted gateway speaking the
# Anthropic format on a neutral host — and selects AWS SigV4 auth for native
# Bedrock, which has no API key to give.
LLM_API_KEY = os.environ.get("CI_LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("CI_LLM_BASE_URL", "").strip()
LLM_PROVIDER = os.environ.get("CI_LLM_PROVIDER", "auto").strip().lower()
LLM_MODEL = os.environ.get("CI_LLM_MODEL", "claude-sonnet-5")

# Azure's dated api-version surface (deployment name in the path). Leave
# unset for Azure's newer /openai/v1/ endpoint, which needs only URL + key.
LLM_API_VERSION = os.environ.get("CI_LLM_API_VERSION", "").strip()
# Native Bedrock (CI_LLM_PROVIDER=bedrock) signs with the standard AWS
# credential chain; only the region has to be stated, and it defaults to the
# one the object store already uses.
LLM_AWS_REGION = os.environ.get("CI_LLM_AWS_REGION", "").strip() or AWS_REGION
# Reasoning models renamed max_tokens -> max_completion_tokens on the OpenAI
# wire. "auto" guesses from the model id and self-corrects on the provider's
# first 400 (app/llmclient.py); pin it if a gateway's error text is unclear.
LLM_MAX_TOKENS_PARAM = os.environ.get("CI_LLM_MAX_TOKENS_PARAM", "auto").strip()

# Native Bedrock authenticates by IAM role rather than by key, so an empty
# CI_LLM_API_KEY does not mean "unconfigured" there.
LLM_ENABLED = bool(LLM_API_KEY) or LLM_PROVIDER == "bedrock"


def _csv(name: str, default: list[str]) -> list[str]:
    """A comma-separated env override for a list setting, empty entries
    dropped. An explicitly empty value means the empty list, not the default
    — that's how CI_LLM_THINKING_MODELS turns extended thinking off."""
    raw = os.environ.get(name)
    if raw is None:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# User-selectable per conversation (app/api/chat.py); CI_LLM_MODEL above is
# just the default a new conversation starts with. The Claude defaults only
# apply while CI_LLM_MODEL is one of them — point CI_LLM_MODEL at another
# provider's model and the picker narrows to that model alone unless
# CI_LLM_MODEL_CHOICES names the rest, since no list of model ids is valid
# across providers.
_DEFAULT_MODEL_CHOICES = ["claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5-20251001"]
LLM_MODEL_CHOICES = _csv(
    "CI_LLM_MODEL_CHOICES",
    _DEFAULT_MODEL_CHOICES if LLM_MODEL in _DEFAULT_MODEL_CHOICES else [LLM_MODEL],
)
if LLM_MODEL and LLM_MODEL not in LLM_MODEL_CHOICES:
    # the default must always be selectable, or every request that echoes it
    # back (app/api/chat.py's _validate_llm_model) would 400
    LLM_MODEL_CHOICES = [LLM_MODEL, *LLM_MODEL_CHOICES]

# Models to request extended thinking/reasoning from. Sent as Anthropic's
# adaptive thinking or as OpenAI's reasoning_effort depending on the wire;
# a model that doesn't support it rejects the whole request rather than
# ignoring the parameter, which is why this is a declared list and not a
# blanket flag. Same discipline as LLM_MODEL_CHOICES: only the Claude
# defaults are assumed, anything else has to be named.
LLM_THINKING_MODELS = set(_csv(
    "CI_LLM_THINKING_MODELS",
    [m for m in ("claude-opus-4-8", "claude-sonnet-5") if m in LLM_MODEL_CHOICES],
))
LLM_REASONING_EFFORT = os.environ.get("CI_LLM_REASONING_EFFORT", "medium").strip()

# Sandbox coding agent (app/sandbox_agent.py) — writes polars for the open
# notebook, and fills in a converted pipeline's lineage. Shares CI_LLM_API_KEY
# above, so it is off in exactly the deployments conversational analytics is
# off in: an unconfigured deployment never sends notebook code to a third
# party. Everything below is a cost/latency dial, and every default is set
# for a fast interactive loop rather than maximum thoroughness — the notebook
# itself is the feedback channel (run the cell, feed the error back), so the
# agent makes exactly one model call per request and never runs or tests
# anything itself.
SANDBOX_AGENT_MODEL = os.environ.get("CI_SANDBOX_AGENT_MODEL", LLM_MODEL)
# Lineage generation is mechanical summarization of a script the platform
# already parsed, so it defaults to the cheapest/fastest choice available —
# but only when that choice is actually selectable, since a hardcoded Claude
# id is not a valid model on any other provider.
_CHEAPEST_MODEL = "claude-haiku-4-5-20251001"
SANDBOX_LINEAGE_MODEL = os.environ.get(
    "CI_SANDBOX_LINEAGE_MODEL",
    _CHEAPEST_MODEL if _CHEAPEST_MODEL in LLM_MODEL_CHOICES else LLM_MODEL,
)
SANDBOX_AGENT_MAX_TOKENS = 2048
SANDBOX_LINEAGE_MAX_TOKENS = 1024
# Context budget: how much of the live notebook is sent per request. Cell
# sources are the signal; a run's stdout/traceback is trimmed to its tail
# (where the error actually is) and result *rows* are never sent at all —
# only column names and dtypes.
SANDBOX_AGENT_CELL_CHARS = 4000
SANDBOX_AGENT_OUTPUT_CHARS = 800
SANDBOX_AGENT_FILES = 150
SANDBOX_AGENT_HISTORY_TURNS = 6

# Agent/Skills MCP server (specs/017-agent-skills-mcp-server/) — per-identity
# rate limit on skill invocations that call the LLM backend (ask_question),
# so a single session/token can't drive unbounded LLM API cost/load
# through the external /mcp surface (research.md R3). In-process only — no
# shared store needed, matching the single-uvicorn-worker deployment model.
MCP_RATE_LIMIT_PER_MIN = int(os.environ.get("CI_MCP_RATE_LIMIT_PER_MIN", "20"))


def storage_options() -> dict:
    """storage_options passed to polars scan_* for the S3 object store."""
    return {
        "aws_access_key_id": AWS_ACCESS_KEY_ID,
        "aws_secret_access_key": AWS_SECRET_ACCESS_KEY,
        "aws_region": AWS_REGION,
        "aws_endpoint_url": S3_ENDPOINT,
        "aws_allow_http": "true",
    }


def iceberg_storage_options() -> dict:
    """storage_options for polars scan_iceberg / pyiceberg's S3 FileIO — same
    credentials as storage_options() above, translated to the `s3.*` key
    names pyiceberg expects (see https://py.iceberg.apache.org/configuration/
    #fileio). Path-style addressing is required against the moto/MinIO
    emulator and works fine against real S3 too."""
    return {
        "s3.access-key-id": AWS_ACCESS_KEY_ID,
        "s3.secret-access-key": AWS_SECRET_ACCESS_KEY,
        "s3.region": AWS_REGION,
        "s3.endpoint": S3_ENDPOINT,
        "s3.path-style-access": "true",
    }


def delta_write_options() -> dict:
    """storage_options for deltalake writes (seeding). The unsafe-rename flag is
    fine here: single writer, emulated bucket."""
    return {
        "AWS_ACCESS_KEY_ID": AWS_ACCESS_KEY_ID,
        "AWS_SECRET_ACCESS_KEY": AWS_SECRET_ACCESS_KEY,
        "AWS_REGION": AWS_REGION,
        "AWS_ENDPOINT_URL": S3_ENDPOINT,
        "AWS_ALLOW_HTTP": "true",
        "AWS_S3_ALLOW_UNSAFE_RENAME": "true",
    }
