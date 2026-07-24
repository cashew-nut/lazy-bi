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

# Pipelines (specs/014-polars-pipeline-module/) — hosted polars transformation
# scripts; a run executes in its own subprocess, killed if it outruns its
# timeout (runs are strictly serialized platform-wide, one at a time).
PIPELINES_DIR = Path(os.environ.get("CI_PIPELINES_DIR", PROJECT_ROOT / "pipelines"))
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

# Sessions — see specs/011-session-auth-rbac/. Idle/absolute lifetimes in
# days; the cookie's Secure flag is off by default because the demo runs on
# plain HTTP (set CI_COOKIE_SECURE=1 behind TLS).
SESSION_IDLE_DAYS = int(os.environ.get("CI_SESSION_IDLE_DAYS", "7"))
SESSION_MAX_DAYS = int(os.environ.get("CI_SESSION_MAX_DAYS", "30"))
COOKIE_SECURE = os.environ.get("CI_COOKIE_SECURE", "0") == "1"

# Conversational analytics (specs/012-conversational-analytics/) — off unless
# an API key is configured, so an unconfigured deployment never sends
# question text/schema/results to a third party (research.md R7).
LLM_API_KEY = os.environ.get("CI_LLM_API_KEY", "")
LLM_MODEL = os.environ.get("CI_LLM_MODEL", "claude-sonnet-5")
LLM_ENABLED = bool(LLM_API_KEY)
# User-selectable per conversation (app/api/chat.py); CI_LLM_MODEL above is
# just the default a new conversation starts with. Keep in sync with the
# id's actually valid for the configured provider.
LLM_MODEL_CHOICES = ["claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5-20251001"]

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
# already parsed, so it defaults to the cheapest/fastest choice rather than
# to whatever the coding agent uses.
SANDBOX_LINEAGE_MODEL = os.environ.get("CI_SANDBOX_LINEAGE_MODEL", "claude-haiku-4-5-20251001")
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
