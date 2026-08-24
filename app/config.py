"""Runtime configuration.

Everything defaults to a fully local demo: an embedded moto S3 emulator,
a bundled semantic-model directory and a sqlite db in the project root.
Point CI_S3_ENDPOINT at a real (or external emulator) endpoint to skip
the embedded server.

Settings come from the environment, optionally seeded from a `.env` file in
the project root (_load_env_file below) so secrets — CI_LLM_API_KEY above
all — can live in a gitignored file instead of a shell profile or a command
line. Docker Compose already loads that same file on its own; this makes
`./run.sh` and a bare `uvicorn` behave the same way. The test suite opts out
(tests/conftest.py), so a real key sitting in a developer's `.env` can't
change what a test run sees.

The load happens before anything below reads os.environ, so a `.env` entry
is indistinguishable from an exported variable — including for settings whose
meaning depends on mere presence, like CI_S3_ENDPOINT disabling the embedded
emulator.
"""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_env_file() -> list[str]:
    """Seed os.environ from a `.env` file, without overriding anything the
    real environment already sets — an explicit `export` (or a `-e` on a
    container) always wins over the file, which is what makes a one-off
    override possible without editing it.

    Returns the keys the file set that were ignored for that reason. A
    forgotten `export CI_LLM_API_KEY=...` from an earlier experiment beats
    the `.env` written to replace it, and the only symptom is the old value
    being used — so the names of the shadowed settings get reported at
    startup (app/main.py) rather than left to be discovered from a 401.

    Deliberately a literal parser, not a shell: a value is taken exactly as
    written, so an API key containing `$`, `#`, backticks or spaces needs no
    escaping and can never be mangled by expansion. The accepted subset is
    the one `.env.example` documents — `KEY=value`, `#` comment lines, blank
    lines, an optional `export ` prefix, and optional matching single or
    double quotes around the value (stripped; useful only for preserving
    leading/trailing whitespace).

    Set CI_ENV_FILE to point somewhere else, or to an empty value to skip
    file loading entirely (what the tests do, so a developer's own `.env`
    can't change what a test run sees).
    """
    configured = os.environ.get("CI_ENV_FILE")
    if configured is not None and not configured.strip():
        return []
    path = Path(configured.strip()) if configured else PROJECT_ROOT / ".env"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []       # absent or unreadable: the environment alone decides
    shadowed = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        if not key:
            continue
        if key in os.environ:
            shadowed.append(key)
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ[key] = value
    return shadowed


# names only — the values are the point of keeping them out of a log
ENV_FILE_SHADOWED = _load_env_file()

# S3 / emulator
S3_ENDPOINT = os.environ.get("CI_S3_ENDPOINT", "http://127.0.0.1:9600")
EMBEDDED_EMULATOR = "CI_S3_ENDPOINT" not in os.environ
BUCKET = os.environ.get("CI_BUCKET", "cash-intel")

# Optional: a ~/.aws/config profile name — including one backed by AWS SSO
# (`aws configure sso`) with automatic token refresh, per the AWS CLI's
# "sso-configure-profile-token-auto-sso" guide. Presence, not value, decides:
# set this and resolve_credentials() below asks boto3 for that profile's
# credentials fresh on every call instead of using the static keys, which is
# what makes short-lived SSO credentials keep working without ever sitting
# in .env — boto3 re-derives them from the (much longer-lived) cached SSO
# token itself, the same way `aws s3 ls --profile ...` would. Takes priority
# over AWS_ACCESS_KEY_ID/etc. below when both happen to be set.
AWS_PROFILE = os.environ.get("AWS_PROFILE", "")

# Static fallback, used as-is when AWS_PROFILE is unset. Defaults to the
# demo emulator's dummy credentials unless a profile is configured, in which
# case an unset key here means "let resolve_credentials() below ask boto3"
# rather than silently sending "testing" to a real endpoint.
_default_creds = "" if AWS_PROFILE else "testing"
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", _default_creds)
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", _default_creds)
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
# Optional: a temporary/STS credential pasted in directly instead of going
# through AWS_PROFILE above — a long-lived IAM user's access key + secret
# work without one. Left out of every options dict below unless non-empty,
# since object_store/boto3 treat an *empty* session token as a real
# (invalid) one rather than "absent", which would break the far more common
# no-token case. Prefer AWS_PROFILE for anything SSO-derived: a value here
# is exactly the kind of copy-pasted, silently-expiring credential
# AWS_PROFILE exists to avoid.
AWS_SESSION_TOKEN = os.environ.get("AWS_SESSION_TOKEN", "")


def resolve_credentials() -> tuple[str, str, str | None]:
    """(access_key, secret_key, session_token) for this call.

    Re-resolved every time rather than cached at import time: an
    AWS_PROFILE credential (an SSO one especially) can expire, and asking
    boto3 again — not caching it ourselves — is how it gets refreshed. The
    static keys above never expire, so re-reading them costs nothing extra.
    """
    if not AWS_PROFILE:
        return AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN or None
    import boto3  # local: the static-keys path above never needs this

    creds = boto3.Session(profile_name=AWS_PROFILE).get_credentials()
    if creds is None:
        raise RuntimeError(
            f"AWS_PROFILE={AWS_PROFILE!r} has no resolvable credentials — "
            f"check `aws sso login --profile {AWS_PROFILE}` (or the profile "
            f"name itself) against ~/.aws/config"
        )
    frozen = creds.get_frozen_credentials()
    return frozen.access_key, frozen.secret_key, frozen.token

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

# ── DuckDB runtime (app/duck.py) ──────────────────────────────────────────
# One connection per process holds every cache that makes a second query
# cheap, so where it lives matters. In-memory by default: the caches are
# rebuilt from the bucket on start, and nothing the platform must not lose is
# ever written here (visuals, dashboards and run history live in
# cash_intel.db). Point CI_DUCKDB_PATH at a file to keep pinned sources and
# the external file cache across restarts — worth it against a slow endpoint
# with data that lands nightly, and a staleness risk otherwise.
DUCKDB_PATH = os.environ.get("CI_DUCKDB_PATH", ":memory:")
# Left unset, DuckDB sizes both from the host. Set them in a container, where
# the host's core count and RAM are not the container's.
DUCKDB_THREADS = int(os.environ.get("CI_DUCKDB_THREADS", "0")) or None
DUCKDB_MEMORY_LIMIT = os.environ.get("CI_DUCKDB_MEMORY_LIMIT", "")

# HTTP behaviour against the object store. DuckDB's defaults (3 retries, 30s)
# are tuned for a well-behaved endpoint; both are worth raising against a
# throttling bucket and lowering in a test suite that would rather fail fast.
HTTP_RETRIES = int(os.environ.get("CI_HTTP_RETRIES", "3"))
HTTP_TIMEOUT_MS = int(os.environ.get("CI_HTTP_TIMEOUT_MS", "30000"))

# Read-only S3 lookup cache (app/cache.py) — schema introspection and
# spine-dimension bounds are re-derived from S3 far more often than the
# backing data actually changes (every query re-resolves its own schema,
# every editor keystroke re-checks a source's columns). Entries self-expire
# rather than being trusted indefinitely, since this process has no way to
# know when new data lands; registry.reload_all() also clears the cache
# outright on every model/bundle edit, since that changes what a path
# resolves to, not just its data.
#
# A schema is the shape of a source, not its contents: it only changes when
# someone rewrites the data with different columns, which is exactly the
# case reload_all()'s clear() already covers for anything the platform did
# itself. So this TTL is only protecting against a schema change made
# *outside* the app, and 30s of that protection cost a full cold metadata
# walk on every editor interaction slower than half a minute — i.e. on every
# one with a human thinking in it. Minutes is the honest setting.
SCHEMA_CACHE_TTL = float(os.environ.get("CI_SCHEMA_CACHE_TTL", "300"))
# Bounds are min/max over the data itself, so unlike a schema they move when
# rows land. Raised much more modestly for that reason.
BOUNDS_CACHE_TTL = float(os.environ.get("CI_BOUNDS_CACHE_TTL", "120"))

# ── source read cache (app/engine.py's _scan_source) ──────────────────────
# The one that matters against a real object store. polars is lazy about
# *bytes* but not about *round trips*: every collect() re-lists a glob,
# re-reads every parquet footer and re-reads every joined lookup file from
# scratch, so one visual costs tens of sequential S3 requests and a real
# endpoint's 30-80ms RTT turns that into seconds. The two caches above never
# helped there — they memoize derived Python objects (a pl.Schema, a bounds
# tuple), not the I/O inside collect().
#
# So: resolve each source's object listing once (SOURCE_CACHE_TTL), and hold
# small sources in memory as whole frames instead of re-reading them per
# query. Small is the common case for exactly the sources that hurt most —
# the lookup tables in `joins:` and the datasets behind a dimension bundle
# are read on every query that touches them and are usually kilobytes.
#
# TTL is the staleness contract: until an entry expires, a query can answer
# from bytes read up to SOURCE_CACHE_TTL seconds ago. 60s keeps that well
# inside "a person clicking around gets consistent answers" while still
# collapsing an editing session's many queries onto one read. Raise it (600+)
# when data lands hourly or nightly — that is where the biggest wins are —
# and drop it to 0 to disable source caching entirely and get exactly the
# old read-everything-every-time behaviour back.
SOURCE_CACHE_TTL = float(os.environ.get("CI_SOURCE_CACHE_TTL", "60"))
# Gate on the source's total size *in the object store*, which one LIST
# already tells us, so an oversized source is never downloaded to discover
# it was oversized.
SOURCE_CACHE_MAX_BYTES = int(os.environ.get("CI_SOURCE_CACHE_MAX_BYTES", 16 * 1024 * 1024))
# Second guard, applied after materializing: columnar data on disk is
# compressed and dictionary-encoded, so an under-the-gate source can still
# expand several-fold in memory. Anything past this is dropped again rather
# than held (the read still happened — the gate above is what prevents the
# expensive case, this only prevents *retaining* it).
SOURCE_CACHE_MAX_RESIDENT_BYTES = int(
    os.environ.get("CI_SOURCE_CACHE_MAX_RESIDENT_BYTES", 256 * 1024 * 1024))

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
# .strip() because a key that picked up a trailing newline or space on its
# way here (a CRLF-edited .env, a `$(cat key.txt)`, a copy-paste) is sent
# verbatim otherwise, and every provider answers that with a flat 401 — which
# reads as "wrong key" rather than "right key, wrong bytes". Whitespace is
# never part of a real key, so trimming it can only help.
LLM_API_KEY = os.environ.get("CI_LLM_API_KEY", "").strip()
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


def _int(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip())
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    """A yes/no env override for a flag setting.

    Empty falls back to the default for the same reason _csv's does: empty
    is what an unset variable looks like after a `${VAR:-}` in
    docker-compose.yml. An unrecognized value falls back too rather than
    reading as false, so a typo can't quietly turn a feature off.
    """
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


# Extra allowance on the OpenAI wire only, where max_completion_tokens is a
# single budget for reasoning tokens *and* the answer. The per-feature limits
# below (and app/llm.py's, app/composer.py's) size the answer alone, the way
# Anthropic's max_tokens does, so a reasoning model needs room on top or it
# spends the lot thinking and never reaches the tool call.
LLM_REASONING_TOKENS = _int("CI_LLM_REASONING_TOKENS", 8192)

# Native Bedrock authenticates by IAM role rather than by key, so an empty
# CI_LLM_API_KEY does not mean "unconfigured" there.
LLM_ENABLED = bool(LLM_API_KEY) or LLM_PROVIDER == "bedrock"


def _csv(name: str, default: list[str]) -> list[str]:
    """A comma-separated env override for a list setting, empty entries
    dropped.

    An *empty* value falls back to the default rather than meaning the empty
    list, because empty is what an unset variable looks like after passing
    through a `${VAR:-}` in docker-compose.yml or a bare `KEY=` in a .env —
    neither of which is someone asking to turn a feature off. To actually
    mean "none", write `none`: explicit, and impossible to produce by
    accident.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return list(default)
    if raw.lower() == "none":
        return []
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

# Models that *can* be asked for extended thinking/reasoning — a capability
# declaration, not a preference. Sent as Anthropic's adaptive thinking or as
# OpenAI's reasoning_effort depending on the wire; a model that doesn't
# support it rejects the whole request rather than ignoring the parameter,
# which is why this stays a declared list and not a blanket flag. Same
# discipline as LLM_MODEL_CHOICES: only the Claude defaults are assumed,
# anything else has to be named.
#
# *Whether* to ask is the UI's THINKING toggle (per conversation in chat,
# per session in the modelling panel and the Composer) — this list decides
# where that toggle is offered at all, and a request for thinking on a model
# that isn't listed is dropped rather than sent (app/llmclient.py).
LLM_THINKING_MODELS = set(_csv(
    "CI_LLM_THINKING_MODELS",
    [m for m in ("claude-opus-4-8", "claude-sonnet-5") if m in LLM_MODEL_CHOICES],
))
# The state that toggle starts in: a new conversation's default, and what
# any caller that never touches the toggle (the MCP skills seam, an API
# client that omits the field) gets.
LLM_THINKING_DEFAULT = _bool("CI_LLM_THINKING_DEFAULT", True)
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
    access_key, secret_key, session_token = resolve_credentials()
    opts = {
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret_key,
        "aws_region": AWS_REGION,
        "aws_endpoint_url": S3_ENDPOINT,
        "aws_allow_http": "true",
    }
    if session_token:
        opts["aws_session_token"] = session_token
    return opts


def iceberg_storage_options() -> dict:
    """storage_options for polars scan_iceberg / pyiceberg's S3 FileIO — same
    credentials as storage_options() above, translated to the `s3.*` key
    names pyiceberg expects (see https://py.iceberg.apache.org/configuration/
    #fileio). Path-style addressing is required against the moto/MinIO
    emulator and works fine against real S3 too."""
    access_key, secret_key, session_token = resolve_credentials()
    opts = {
        "s3.access-key-id": access_key,
        "s3.secret-access-key": secret_key,
        "s3.region": AWS_REGION,
        "s3.endpoint": S3_ENDPOINT,
        "s3.path-style-access": "true",
    }
    if session_token:
        opts["s3.session-token"] = session_token
    return opts


def delta_write_options() -> dict:
    """storage_options for deltalake writes (seeding). The unsafe-rename flag is
    fine here: single writer, emulated bucket."""
    access_key, secret_key, session_token = resolve_credentials()
    opts = {
        "AWS_ACCESS_KEY_ID": access_key,
        "AWS_SECRET_ACCESS_KEY": secret_key,
        "AWS_REGION": AWS_REGION,
        "AWS_ENDPOINT_URL": S3_ENDPOINT,
        "AWS_ALLOW_HTTP": "true",
        "AWS_S3_ALLOW_UNSAFE_RENAME": "true",
    }
    if session_token:
        opts["AWS_SESSION_TOKEN"] = session_token
    return opts
