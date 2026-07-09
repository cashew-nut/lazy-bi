#!/bin/sh
# Start CASH_INTELLIGENCE with the embedded S3 emulator on http://127.0.0.1:8080
cd "$(dirname "$0")"
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080
