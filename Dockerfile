# CASH INTELLIGENCE — single-container build.
# Demo mode by default: an embedded moto S3 server runs in-process and the
# bucket is seeded on start. Point CI_S3_ENDPOINT at MinIO/real S3 to disable.
FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY models/ models/

# state lives outside the image: sqlite db (+ optional big-file cache)
ENV CI_DB_PATH=/data/cash_intel.db \
    CI_MODELS_DIR=/srv/models \
    PYTHONUNBUFFERED=1
RUN mkdir /data
VOLUME /data

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)"

# single worker by design: demo mode runs the S3 emulator in-process, and the
# sqlite store expects one writer. Scale out only with an external S3 endpoint.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
