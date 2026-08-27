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
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

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
    double quotes around the value (stripped). Quotes are for readability:
    every setting below reads its value through _env/_int/_float, which trim,
    so they cannot be used to give a setting leading or trailing whitespace.

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


def _env(name: str, default: str = "") -> str:
    """An env setting, read with an *empty* value meaning "unset".

    Empty is what an unset variable looks like after passing through a
    `${VAR:-}` in docker-compose.yml — the idiom that lets one compose file
    forward a setting a developer may or may not have exported. Taking that
    literally is how a container ends up with no credentials, or with an S3
    endpoint of "" it then tries to connect to, when the intent was plainly
    "leave this one alone". Every setting below that has a meaningful default
    goes through here for that reason.
    """
    return (os.environ.get(name) or "").strip() or default


def _int(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip())
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float((os.environ.get(name) or "").strip())
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


# ── object stores ─────────────────────────────────────────────────────────
# Two of them, and keeping them apart is the whole reason the built-in demo
# catalog and a real bucket can be open in the same browser tab:
#
#   the demo store     holds DEMO_BUCKET — the bucket every models/*.yaml and
#                      dimensions/*.yaml path names. Served by the embedded
#                      moto emulator unless something else is pointed at it.
#   the primary store  holds BUCKET, and every other bucket a model, pipeline
#                      or notebook names. This is the one real credentials
#                      open, and the only one this app ever treats as durable.
#
# While BUCKET *is* DEMO_BUCKET — the zero-config demo, the MinIO compose
# profile, the test suite — they are one store and nothing is any different
# from before this split existed. The moment CI_BUCKET names a real bucket
# they separate: the demo bucket keeps its emulator, so the demo models keep
# answering, while every other path goes to the real endpoint with the real
# credential. Neither one can break the other, and no demo byte is ever
# written to (or read from) an account someone pays for.
EMULATOR_ENDPOINT = _env("CI_EMULATOR_ENDPOINT", "http://127.0.0.1:9600")
DEMO_BUCKET = _env("CI_DEMO_BUCKET", "cash-intel")
# Off switch for the whole demo: no emulator, no seeding, and models/ and
# dimensions/ are not loaded (app/registry.py), so a deployment that only
# wants its own data doesn't have to look at a catalog it can't use.
DEMO_ENABLED = _bool("CI_DEMO", True)

BUCKET = _env("CI_BUCKET", DEMO_BUCKET)
# Where in BUCKET this app's data lives. Only dataset *discovery* uses it (a
# model names its own absolute path either way) — but on a real bucket that
# also holds logs, exports and years of someone else's prefixes, browsing the
# whole thing is a listing that never ends. See app/api/datasets.py.
BUCKET_PREFIX = _env("CI_BUCKET_PREFIX").lstrip("/")

# Ceiling on any single bucket walk, wherever one happens: dataset discovery,
# the explorer, and the listing app/duck.py resolves a glob with. A demo
# bucket holds a few dozen objects and a real one can hold millions, and S3
# hands them back 1000 per round trip — so an unbounded walk is not slow, it
# is a page that never finishes loading. Past this many, a caller reports
# what it got as truncated rather than paging on (app/api/datasets.py), and
# duck.py stops resolving the glob itself and lets DuckDB do it per query.
LIST_MAX_KEYS = _int("CI_LIST_MAX_KEYS", 20_000)

# The primary store's endpoint. Empty means real AWS, addressed by region the
# way boto3 and DuckDB both do it on their own — which is the *correct*
# setting for S3 and not merely a shortcut: pinning s3.amazonaws.com sends
# every request to the us-east-1 frontend, and a bucket that lives anywhere
# else answers those with a redirect nobody follows. Set it for MinIO,
# LocalStack, or an S3-compatible vendor; leave it unset for AWS itself.
S3_ENDPOINT = _env("CI_S3_ENDPOINT") or (EMULATOR_ENDPOINT if BUCKET == DEMO_BUCKET else "")
# The demo store's endpoint: the primary one while the two are the same store,
# else the embedded emulator. Explicit override for the rare case of a demo
# bucket hosted somewhere specific (a shared MinIO, say).
DEMO_S3_ENDPOINT = _env("CI_DEMO_S3_ENDPOINT") or (
    S3_ENDPOINT if BUCKET == DEMO_BUCKET else EMULATOR_ENDPOINT)
# Run the embedded emulator iff the demo store is the address it serves. Note
# what this no longer keys on: CI_S3_ENDPOINT being set. Pointing the app at a
# real bucket used to switch the emulator off and take the demo catalog down
# with it — the demo models stayed listed, and every one of their queries came
# back 404 NoSuchBucket from an account that had never heard of them.
EMBEDDED_EMULATOR = DEMO_ENABLED and DEMO_S3_ENDPOINT == EMULATOR_ENDPOINT

# Optional: a ~/.aws/config profile name — including one backed by AWS SSO
# (`aws configure sso`) with automatic token refresh, per the AWS CLI's
# "sso-configure-profile-token-auto-sso" guide. Presence, not value, decides:
# set this and resolve_credentials() below asks boto3 for that profile's
# credentials fresh on every call instead of using the static keys, which is
# what makes short-lived SSO credentials keep working without ever sitting
# in .env — boto3 re-derives them from the (much longer-lived) cached SSO
# token itself, the same way `aws s3 ls --profile ...` would. Takes priority
# over AWS_ACCESS_KEY_ID/etc. below when both happen to be set.
AWS_PROFILE = _env("AWS_PROFILE")

# Static fallback, used as-is when AWS_PROFILE is unset. Defaults to the
# demo emulator's dummy credentials only when the primary store *is* that
# emulator; anywhere else an unset key means "let resolve_credentials() below
# ask boto3", so a deployment holding an instance/task role (EC2, ECS, EKS,
# Lambda) needs no credential configuration at all — and so that "testing" is
# never the thing sent to a real endpoint.
_default_creds = "testing" if S3_ENDPOINT == EMULATOR_ENDPOINT and not AWS_PROFILE else ""
AWS_ACCESS_KEY_ID = _env("AWS_ACCESS_KEY_ID", _default_creds)
AWS_SECRET_ACCESS_KEY = _env("AWS_SECRET_ACCESS_KEY", _default_creds)
AWS_REGION = _env("AWS_REGION", "us-east-1")
# Optional: a temporary/STS credential pasted in directly instead of going
# through AWS_PROFILE above — a long-lived IAM user's access key + secret
# work without one. Left out of every options dict below unless non-empty,
# since object_store/boto3 treat an *empty* session token as a real
# (invalid) one rather than "absent", which would break the far more common
# no-token case. Prefer AWS_PROFILE for anything SSO-derived: a value here
# is exactly the kind of copy-pasted, silently-expiring credential
# AWS_PROFILE exists to avoid.
AWS_SESSION_TOKEN = _env("AWS_SESSION_TOKEN")


# The one boto3 credential resolver this process holds (None until the first
# profile/chain resolution needs it). What must NOT be rebuilt per call is the
# resolver, not the credential: constructing a boto3.Session re-parses
# ~/.aws/config and re-runs the whole provider chain — an SSO token read, a
# credential_process exec, an STS AssumeRole round trip — which measures
# 150ms+ on a corp-auth laptop. And resolve_credentials() sits on the hottest
# path there is: every DuckDB cursor checks the store secrets, and every
# secret check resolves credentials, so a per-call Session turns each saved
# model reload (dozens of measure compiles) into tens of seconds of pure
# credential re-resolution. A failed resolution is never cached — the next
# call retries from scratch, so `aws sso login` mid-process is picked up.
# A credential botocore can refresh (SSO, assume-role, an instance role —
# anything carrying an expiry) is held for good: refreshing is its own job.
# One it can't refresh (plain keys in ~/.aws/credentials, a credential_process
# with no Expiration) is re-resolved on a slow cadence instead, so a rotation
# there is still picked up within minutes rather than never.
_boto_credentials = None
_boto_resolved_at = 0.0
_BOTO_STATIC_TTL = 900.0
_boto_lock = None


def resolve_credentials() -> tuple[str, str, str | None]:
    """(access_key, secret_key, session_token) for this call.

    Still fresh on every call in the sense that matters: the held botocore
    credential object refreshes *itself* when it nears expiry (that is what
    RefreshableCredentials is), the same way a long-running AWS CLI process
    stays signed in — so an AWS_PROFILE/SSO credential keeps working without
    ever paying the full provider-chain walk per call. The static keys above
    never touch boto3 at all.
    """
    if not AWS_PROFILE and AWS_ACCESS_KEY_ID:
        return AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN or None
    import time as _time

    global _boto_credentials, _boto_resolved_at, _boto_lock
    if _boto_lock is None:
        import threading

        _boto_lock = threading.Lock()
    with _boto_lock:
        creds = _boto_credentials
        # a refreshable credential manages its own lifetime; a static one is
        # re-resolved once its TTL lapses, in case the file behind it changed
        if (creds is not None and not hasattr(creds, "refresh_needed")
                and _time.monotonic() - _boto_resolved_at > _BOTO_STATIC_TTL):
            creds = None
    if creds is None:
        import boto3  # local: the static-keys path above never needs this

        # No profile and no static key: boto3's own default chain, which is
        # what already holds the credential on anything running with an
        # instance or task role. Named profile when there is one.
        session = boto3.Session(profile_name=AWS_PROFILE) if AWS_PROFILE else boto3.Session()
        creds = session.get_credentials()
        if creds is None:
            where = (f"`aws sso login --profile {AWS_PROFILE}` (or the profile name "
                     f"itself) against ~/.aws/config" if AWS_PROFILE else
                     "AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, AWS_PROFILE, or an "
                     "instance/task role")
            subject = f"AWS_PROFILE={AWS_PROFILE!r}" if AWS_PROFILE else "this environment"
            raise RuntimeError(f"{subject} has no resolvable AWS credentials — check {where}")
        with _boto_lock:
            _boto_credentials = creds
            _boto_resolved_at = _time.monotonic()
    frozen = creds.get_frozen_credentials()
    return frozen.access_key, frozen.secret_key, frozen.token


# The dummy credential the embedded emulator is opened with. Never a real
# one: moto accepts anything, and resolving a real (possibly SSO) credential
# to talk to a loopback server is both wasted work and one more thing that
# can fail on a laptop with an expired session.
DEMO_CREDENTIALS = ("testing", "testing", None)


@dataclass(frozen=True)
class Store:
    """One object store — an endpoint, a region and a credential — and the
    addressing rules that follow from them.

    Resolved per call rather than frozen at import (see store_for below) so
    the settings above stay the single source of truth, and so a test can
    move an endpoint without rebuilding the module."""
    name: str           # "demo" | "primary"; appears in logs and errors
    endpoint: str       # "" = AWS's own regional endpoint
    region: str
    demo: bool

    @property
    def aws(self) -> bool:
        """Real S3, addressed the way AWS expects rather than by a pinned
        host. The distinction decides URL style, TLS, and whether a bucket's
        region is worth looking up (app/s3.py's bucket_region)."""
        return not self.endpoint

    @property
    def host(self) -> str:
        """The endpoint as DuckDB's `ENDPOINT` wants it: host[:port], no
        scheme. Empty for AWS, where DuckDB derives it from the region."""
        if self.aws:
            return ""
        parsed = urlparse(self.endpoint)
        return parsed.netloc or self.endpoint

    @property
    def use_ssl(self) -> bool:
        return self.aws or urlparse(self.endpoint).scheme == "https"

    @property
    def path_style(self) -> bool:
        """Path-style addressing for anything that isn't real S3. Virtual-host
        style needs a wildcard DNS entry per bucket, which moto, MinIO and
        LocalStack don't have; AWS prefers vhost and is the only thing here
        that gets it."""
        return not self.aws and not self.host.split(":")[0].endswith("amazonaws.com")

    @property
    def label(self) -> str:
        """This store's endpoint as something to show a person. AWS has no
        endpoint to state, so name the host it derives — the one a failing
        read would print, which is the point of showing it at all."""
        return self.endpoint or f"s3.{self.region}.amazonaws.com"

    @property
    def ephemeral(self) -> bool:
        """This store forgets everything on restart — the embedded emulator
        holds its bucket in memory. What gets re-uploaded at start time
        (app/seed.py) turns on this and nothing else."""
        return EMBEDDED_EMULATOR and self.endpoint == EMULATOR_ENDPOINT

    def credentials(self) -> tuple[str, str, str | None]:
        return DEMO_CREDENTIALS if self.demo else resolve_credentials()


def primary_store() -> Store:
    """Where BUCKET — and every bucket that isn't the demo one — lives."""
    return Store(name="primary", endpoint=S3_ENDPOINT, region=AWS_REGION, demo=False)


def demo_store() -> Store:
    """Where DEMO_BUCKET lives. The same object as primary_store() whenever
    the two share an endpoint — or when there is no demo at all — so
    `stores_split()` below stays a one-line check and nothing downstream has
    to special-case the undivided case."""
    if not DEMO_ENABLED or DEMO_S3_ENDPOINT == S3_ENDPOINT:
        return primary_store()
    return Store(name="demo", endpoint=DEMO_S3_ENDPOINT, region="us-east-1", demo=True)


def stores_split() -> bool:
    """Are the demo and primary stores actually two different places? False
    for the zero-config demo, the MinIO profile and the test suite, where one
    store holds everything."""
    return demo_store() != primary_store()


def store_for(bucket: str) -> Store:
    """The store that owns `bucket`. One rule, applied everywhere a bucket is
    read or written: the demo bucket belongs to the demo store, everything
    else to the primary one."""
    if bucket == DEMO_BUCKET and stores_split():
        return demo_store()
    return primary_store()


def store_for_path(path: str) -> Store:
    """store_for(), given an `s3://bucket/key` url instead of a bare bucket.
    Anything that isn't an s3 url (a local path in a test) belongs to the
    primary store, which is also what a caller that doesn't care will get."""
    if path.startswith("s3://"):
        return store_for(path[len("s3://"):].partition("/")[0])
    return primary_store()

# Semantic models + persistence
MODELS_DIR = Path(_env("CI_MODELS_DIR") or PROJECT_ROOT / "models")
DIMENSIONS_DIR = Path(_env("CI_DIMENSIONS_DIR") or PROJECT_ROOT / "dimensions")
DB_PATH = Path(_env("CI_DB_PATH") or PROJECT_ROOT / "cash_intel.db")

# Disk cache for files uploaded through the app (POST /api/datasets/local,
# app/api/datasets.py) — gitignored, unlike models/dimensions/pipelines
# above. The embedded S3 emulator is in-memory and starts empty on every
# process restart (app/seed.py reseeds the whole demo bucket from scratch
# each time); an upload written only to that bucket would vanish with it.
# Mirrors data_cache/'s role for app/load_taxi.py: the durable copy lives on
# local disk, and app/seed.py re-uploads it to the fresh emulator on every
# start.
LOCAL_DATA_DIR = Path(_env("CI_LOCAL_DATA_DIR") or PROJECT_ROOT / "local_data")

# Pipelines (specs/018-duckdb-sql-engine/) — hosted SQL transformations; a run
# executes in its own subprocess, killed if it outruns its timeout (runs are
# strictly serialized platform-wide, one at a time).
PIPELINES_DIR = Path(_env("CI_PIPELINES_DIR") or PROJECT_ROOT / "pipelines")

# Agents (specs/017-agent-skills-mcp-server/) — declared Agent/Skill bundles,
# same "editable YAML directory" shape as MODELS_DIR/PIPELINES_DIR above.
AGENTS_DIR = Path(_env("CI_AGENTS_DIR") or PROJECT_ROOT / "agents")
PIPELINE_TIMEOUT_DEFAULT = 600
PIPELINE_TIMEOUT_MAX = 3600

# Sandbox notebooks — ad hoc SQL scratch scripts (app/sandbox.py).
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
DUCKDB_PATH = _env("CI_DUCKDB_PATH", ":memory:")
# Left unset, DuckDB sizes both from the host. Set them in a container, where
# the host's core count and RAM are not the container's.
DUCKDB_THREADS = _int("CI_DUCKDB_THREADS", 0) or None
DUCKDB_MEMORY_LIMIT = _env("CI_DUCKDB_MEMORY_LIMIT")

# HTTP behaviour against the object store. DuckDB's defaults (3 retries, 30s)
# are tuned for a well-behaved endpoint; both are worth raising against a
# throttling bucket and lowering in a test suite that would rather fail fast.
HTTP_RETRIES = _int("CI_HTTP_RETRIES", 3)
HTTP_TIMEOUT_MS = _int("CI_HTTP_TIMEOUT_MS", 30000)

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
SCHEMA_CACHE_TTL = _float("CI_SCHEMA_CACHE_TTL", 300)
# Bounds are min/max over the data itself, so unlike a schema they move when
# rows land. Raised much more modestly for that reason.
BOUNDS_CACHE_TTL = _float("CI_BOUNDS_CACHE_TTL", 120)

# ── source read cache (app/engine.py's _scan_source) ──────────────────────
# The two things DuckDB's own caches can't decide for themselves. Its object
# cache holds parquet footers and its external file cache holds file bytes,
# both process-wide — what it cannot know is which objects a *glob* resolves
# to (so it re-LISTs the prefix on every query unless handed a file list), or
# which sources are worth holding as local tables rather than streaming.
#
# So: resolve each source's object listing once (SOURCE_CACHE_TTL), and pin
# small sources as local DuckDB tables instead of re-reading them per query.
# Small is the common case for exactly the sources that hurt most — the lookup
# tables in `joins:` and the datasets behind a dimension bundle are read on
# every query that touches them and are usually kilobytes.
#
# TTL is the staleness contract: until an entry expires, a query can answer
# from bytes read up to SOURCE_CACHE_TTL seconds ago. 60s keeps that well
# inside "a person clicking around gets consistent answers" while still
# collapsing an editing session's many queries onto one read. Raise it (600+)
# when data lands hourly or nightly — that is where the biggest wins are —
# and drop it to 0 to disable listing and pinning entirely, leaving only
# DuckDB's own caches.
SOURCE_CACHE_TTL = _float("CI_SOURCE_CACHE_TTL", 60)
# Gate on the source's total size *in the object store*, which one LIST
# already tells us, so an oversized source is never downloaded to discover
# it was oversized.
SOURCE_CACHE_MAX_BYTES = _int("CI_SOURCE_CACHE_MAX_BYTES", 16 * 1024 * 1024)
# Second guard, applied after the read: columnar data on disk is compressed
# and dictionary-encoded, so an under-the-gate source can still expand
# several-fold in memory. Anything past this is dropped again rather than held
# (the read still happened — the gate above is what prevents the expensive
# case, this only prevents *retaining* it).
SOURCE_CACHE_MAX_RESIDENT_BYTES = _int(
    "CI_SOURCE_CACHE_MAX_RESIDENT_BYTES", 256 * 1024 * 1024)

# Instant cross-filter extracts (specs/016-instant-cross-filter/) — the
# per-tile size cap that decides whether a dashboard tile gets the
# round-trip-free treatment or silently stays on today's live query path.
# An extract is a client-side *cache*, not a render payload, so it is allowed
# past MAX_ROWS above; both limits are checked against the real response
# (FR-009) and either one tripping sends the tile back to live mode.
# research.md R5's proposed starting defaults, benchmarked against the 13M-row
# NYC taxi dataset before shipping (SC-002).
EXTRACT_MAX_ROWS = _int("CI_EXTRACT_MAX_ROWS", 150_000)
EXTRACT_MAX_BYTES = _int("CI_EXTRACT_MAX_BYTES", 25 * 1024 * 1024)

# Sessions — see specs/011-session-auth-rbac/. Idle/absolute lifetimes in
# days; the cookie's Secure flag is off by default because the demo runs on
# plain HTTP (set CI_COOKIE_SECURE=1 behind TLS).
SESSION_IDLE_DAYS = _int("CI_SESSION_IDLE_DAYS", 7)
SESSION_MAX_DAYS = _int("CI_SESSION_MAX_DAYS", 30)
COOKIE_SECURE = _bool("CI_COOKIE_SECURE", False)

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
LLM_API_KEY = _env("CI_LLM_API_KEY")
LLM_BASE_URL = _env("CI_LLM_BASE_URL")
LLM_PROVIDER = _env("CI_LLM_PROVIDER", "auto").lower()
LLM_MODEL = _env("CI_LLM_MODEL", "claude-sonnet-5")

# Azure's dated api-version surface (deployment name in the path). Leave
# unset for Azure's newer /openai/v1/ endpoint, which needs only URL + key.
LLM_API_VERSION = _env("CI_LLM_API_VERSION")
# Native Bedrock (CI_LLM_PROVIDER=bedrock) signs with the standard AWS
# credential chain; only the region has to be stated, and it defaults to the
# one the object store already uses.
LLM_AWS_REGION = _env("CI_LLM_AWS_REGION", AWS_REGION)
# Reasoning models renamed max_tokens -> max_completion_tokens on the OpenAI
# wire. "auto" guesses from the model id and self-corrects on the provider's
# first 400 (app/llmclient.py); pin it if a gateway's error text is unclear.
LLM_MAX_TOKENS_PARAM = _env("CI_LLM_MAX_TOKENS_PARAM", "auto")


# Extra allowance on the OpenAI wire only, where max_completion_tokens is a
# single budget for reasoning tokens *and* the answer. The per-feature limits
# below (and app/llm.py's, app/composer.py's) size the answer alone, the way
# Anthropic's max_tokens does, so a reasoning model needs room on top or it
# spends the lot thinking and never reaches the tool call.
LLM_REASONING_TOKENS = _int("CI_LLM_REASONING_TOKENS", 8192)

# Native Bedrock authenticates by IAM role rather than by key, so an empty
# CI_LLM_API_KEY does not mean "unconfigured" there.
LLM_ENABLED = bool(LLM_API_KEY) or LLM_PROVIDER == "bedrock"


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
LLM_REASONING_EFFORT = _env("CI_LLM_REASONING_EFFORT", "medium")

# Sandbox coding agent (app/sandbox_agent.py) — writes SQL for the open
# notebook, and fills in a converted pipeline's lineage. Shares CI_LLM_API_KEY
# above, so it is off in exactly the deployments conversational analytics is
# off in: an unconfigured deployment never sends notebook code to a third
# party. Everything below is a cost/latency dial, and every default is set
# for a fast interactive loop rather than maximum thoroughness — the notebook
# itself is the feedback channel (run the cell, feed the error back), so the
# agent makes exactly one model call per request and never runs or tests
# anything itself.
SANDBOX_AGENT_MODEL = _env("CI_SANDBOX_AGENT_MODEL", LLM_MODEL)
# Lineage generation is mechanical summarization of a script the platform
# already parsed, so it defaults to the cheapest/fastest choice available —
# but only when that choice is actually selectable, since a hardcoded Claude
# id is not a valid model on any other provider.
_CHEAPEST_MODEL = "claude-haiku-4-5-20251001"
SANDBOX_LINEAGE_MODEL = _env(
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
MCP_RATE_LIMIT_PER_MIN = _int("CI_MCP_RATE_LIMIT_PER_MIN", 20)


def iceberg_storage_options(bucket: str = "") -> dict:
    """Credentials for pyiceberg's S3 FileIO, in the `s3.*` key names it
    expects (see https://py.iceberg.apache.org/configuration/#fileio), for
    the store that owns `bucket`. Only the demo seeder's throwaway catalog
    needs these — every *read* goes through DuckDB's own S3 secrets
    (app/duck.py)."""
    store = store_for(bucket) if bucket else primary_store()
    access_key, secret_key, session_token = store.credentials()
    opts = {
        "s3.access-key-id": access_key,
        "s3.secret-access-key": secret_key,
        "s3.region": store.region,
        # Path-style addressing is required against the moto/MinIO emulator
        # and works fine against real S3 too; the endpoint is only stated
        # when there is one to state, so AWS keeps its own regional default.
        "s3.path-style-access": "true",
    }
    if store.endpoint:
        opts["s3.endpoint"] = store.endpoint
    if session_token:
        opts["s3.session-token"] = session_token
    return opts


def delta_write_options(bucket: str = "") -> dict:
    """storage_options for deltalake writes (seeding, and a pipeline's Delta
    target), for the store that owns `bucket`. The unsafe-rename flag is fine
    here: single writer, and every write this app makes is one."""
    store = store_for(bucket) if bucket else primary_store()
    access_key, secret_key, session_token = store.credentials()
    opts = {
        "AWS_ACCESS_KEY_ID": access_key,
        "AWS_SECRET_ACCESS_KEY": secret_key,
        "AWS_REGION": store.region,
        "AWS_S3_ALLOW_UNSAFE_RENAME": "true",
    }
    if store.endpoint:
        opts["AWS_ENDPOINT_URL"] = store.endpoint
        opts["AWS_ALLOW_HTTP"] = "true"
    if session_token:
        opts["AWS_SESSION_TOKEN"] = session_token
    return opts
