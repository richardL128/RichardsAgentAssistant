#!/bin/sh
set -eu

# Phase 0 has a single API replica, so it is the controlled schema bootstrap
# point. Readiness remains failed until this migration completes.
alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
