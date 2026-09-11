#!/bin/sh
# Durable Phase 2 Procrastinate worker entrypoint. Each service listens only to
# its own queue and defaults to one concurrent job so the shared Ollama host is
# not over-subscribed.
set -eu

worker_name=${1:-academic_planner}
case "$worker_name" in
  academic_planner) ;;
  *)
    printf '%s\n' "Unknown LifeAgent worker queue" >&2
    exit 64
    ;;
esac

exec python -m procrastinate \
  --app app.queue.worker.procrastinate_app \
  worker \
  --name "lifeagent-$worker_name" \
  --queues "$worker_name" \
  --concurrency "${WORKER_CONCURRENCY:-1}" \
  --wait \
  --listen-notify \
  --delete-jobs successful
