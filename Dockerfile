# CASH INTELLIGENCE — single-container build.
#
# Demo mode by default: an embedded moto S3 server runs in-process holding the
# demo bucket, seeded on start. Set CI_BUCKET to read a real bucket alongside
# it (credentials via AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, AWS_PROFILE
# with ~/.aws mounted, or an instance/task role) — the demo keeps its own
# emulator, so both answer. CI_S3_ENDPOINT is for MinIO/LocalStack only; real
# AWS is addressed by region. CI_DEMO=0 drops the demo entirely.
# See docker-compose.yml for the full set of variables to pass through.
FROM python:3.12-slim

WORKDIR /srv

# ca-certificates: every read from real S3 is TLS, and a container without a
# trust store fails all of them with an error that names a certificate rather
# than the missing package. Stated rather than inherited so a future base
# image can't quietly drop it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY models/ models/
COPY dimensions/ dimensions/
COPY pipelines/ pipelines/

# DuckDB's extensions ship as pinned wheels (see requirements.txt) and are
# loaded from disk at runtime, never INSTALLed over the network. A wheel that
# won't load is silent at runtime — the format needing it just reports as
# unavailable — so fail the *build* instead. This goes through app/duck.py's
# own loader, so it asserts the real path rather than a lookalike, and it is
# what catches a version skew between duckdb and an extension after a bump.
RUN python -c "import sys; from app import duck; loaded = duck.loaded_extensions(); missing = sorted(set(duck.EXTENSIONS) - loaded); sys.exit('duckdb extensions failed to load: ' + ', '.join(missing)) if missing else print('duckdb extensions ok:', ', '.join(sorted(loaded)))"

# state lives outside the image: sqlite db + uploaded-dataset cache, both in
# the /data volume so they survive a container restart/recreate
ENV CI_DB_PATH=/data/cash_intel.db \
    CI_LOCAL_DATA_DIR=/data/local_data \
    CI_MODELS_DIR=/srv/models \
    CI_DIMENSIONS_DIR=/srv/dimensions \
    CI_PIPELINES_DIR=/srv/pipelines \
    PYTHONUNBUFFERED=1
RUN mkdir /data
VOLUME /data

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)"

# One uvicorn worker per container, and one container by default: demo mode
# runs the S3 emulator in-process, so a second worker would hold a second,
# different demo bucket. Scale by running more *containers* against an
# external S3 endpoint, with CI_CLUSTERED=1 and CI_ROLE splitting them into
# `web` and `worker` — see docs/scaling.md and deploy/. Deliberately not
# `--workers N`: every replica needs its own node identity and its own
# lifespan (the boot lock, the cluster watcher, the pipeline worker), which
# uvicorn's forked workers would each duplicate under one identity.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
