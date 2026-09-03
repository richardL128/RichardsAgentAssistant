#!/bin/sh
# Phase 0 worker placeholder. It keeps each named service alive until the
# durable Procrastinate app is added in Phase 2. The background wait lets the
# shell receive and forward container stop signals promptly.
set -eu

worker_name=${1:-unnamed}
printf '%s\n' "LifeAgent ${worker_name} worker is idle (Phase 0)"

stop() {
  exit 0
}

trap stop INT TERM

while :; do
  sleep 3600 &
  wait "$!"
done
