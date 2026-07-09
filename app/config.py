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
DB_PATH = Path(os.environ.get("CI_DB_PATH", PROJECT_ROOT / "cash_intel.db"))

# Hard cap on rows returned to the browser
MAX_ROWS = 10_000


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
