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

# Hard cap on rows returned to the browser
MAX_ROWS = 10_000


# ── Measure-code sandbox ──────────────────────────────────────────
# Measure `expr`/`frame` are arbitrary python, eval'd/exec'd against the scan.
# By default that evaluation runs in a hardened, throwaway subprocess (resource
# limits + dropped privileges + minimal environment, optionally under
# bubblewrap/nsjail) so a hostile expression can't reach the API process, its
# secrets, or the host. Set CI_SANDBOX=off to eval in-process — only for a
# single-user, fully-trusted deployment (or the fast test suites).
def _flag(name: str, default: bool) -> bool:
    return os.environ.get(name, "on" if default else "off").strip().lower() not in ("off", "0", "false", "no", "")

SANDBOX_ENABLED = _flag("CI_SANDBOX", True)
# auto = bubblewrap → nsjail → bare subprocess, whichever is available first;
# or pin one of: bwrap | nsjail | subprocess
SANDBOX_BACKEND = os.environ.get("CI_SANDBOX_BACKEND", "auto").strip().lower()
SANDBOX_CPU_SECONDS = int(os.environ.get("CI_SANDBOX_CPU_SECONDS", "20"))       # RLIMIT_CPU (SIGKILL)
SANDBOX_TIMEOUT_SECONDS = float(os.environ.get("CI_SANDBOX_TIMEOUT_SECONDS", "45"))  # wall-clock kill
SANDBOX_MEM_MB = int(os.environ.get("CI_SANDBOX_MEM_MB", "0"))                  # RLIMIT_AS; 0 = unset (polars-friendly)
SANDBOX_FSIZE_MB = int(os.environ.get("CI_SANDBOX_FSIZE_MB", "256"))            # RLIMIT_FSIZE
SANDBOX_NPROC = int(os.environ.get("CI_SANDBOX_NPROC", "64"))                   # RLIMIT_NPROC (fork-bomb cap)
SANDBOX_NOFILE = int(os.environ.get("CI_SANDBOX_NOFILE", "1024"))              # RLIMIT_NOFILE (fd-exhaustion cap)
# Pre-warmed one-shot worker pool: keeps the ~250ms polars import off the
# request path (each worker still handles exactly one job, then exits).
SANDBOX_POOL = _flag("CI_SANDBOX_POOL", True)
SANDBOX_POOL_SIZE = int(os.environ.get("CI_SANDBOX_POOL_SIZE", "2"))
SANDBOX_POOL_WARM_TIMEOUT = float(os.environ.get("CI_SANDBOX_POOL_WARM_TIMEOUT", "30"))


def sandbox_child_env() -> dict:
    """The *entire* environment handed to a sandbox worker — deliberately tiny.
    Only what polars needs to scan the (single, ideally read-only) data bucket
    plus locate the `app` package; every other host secret is withheld. The
    child never re-sandboxes (CI_SANDBOX=off) and is marked as a child."""
    import tempfile
    tmp = tempfile.gettempdir()
    keep = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", ""),
        "PYTHONPATH": str(PROJECT_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        # polars needs a writable temp dir + HOME to place its scratch space
        "HOME": tmp,
        "TMPDIR": tmp,
        "POLARS_TEMP_DIR": tmp,
        # S3 read access — scope these credentials down to read-only + one bucket
        "AWS_ACCESS_KEY_ID": AWS_ACCESS_KEY_ID,
        "AWS_SECRET_ACCESS_KEY": AWS_SECRET_ACCESS_KEY,
        "AWS_REGION": AWS_REGION,
        "CI_S3_ENDPOINT": S3_ENDPOINT,
        "CI_BUCKET": BUCKET,
        "CI_SANDBOX": "off",
        "CI_SANDBOX_CHILD": "1",
    }
    return {k: v for k, v in keep.items() if v != ""}


def storage_options() -> dict:
    """storage_options passed to polars scan_* for the S3 object store."""
    return {
        "aws_access_key_id": AWS_ACCESS_KEY_ID,
        "aws_secret_access_key": AWS_SECRET_ACCESS_KEY,
        "aws_region": AWS_REGION,
        "aws_endpoint_url": S3_ENDPOINT,
        "aws_allow_http": "true",
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
