# Phase 0 — repository and local platform

> Historical implementation record. The current replacement architecture runs
> only `postgres` and `api`; the named model workers recorded below are no
> longer configured or executable runtime services.

## Contract

Phase 0 establishes the shared Python 3.12 platform only: reproducible `uv`
dependencies, quality tooling, typed non-secret configuration, PostgreSQL and
Procrastinate schema bootstrap, writable artifact storage, health endpoints,
one Docker image, three named idle workers, and build-time Tailwind CSS. It
contains no agent-specific behavior.

## Implemented

- Added `pyproject.toml`, `uv.lock`, Ruff, Pyright, pytest, and local pre-commit
  hooks. The project and runtime both use uv 0.12.6.
- Added validated Pydantic settings and redacted diagnostics.
- Added Alembic bootstrap for Procrastinate 3.9.0's complete shipped schema.
- Added `/health/live` and `/health/ready`. Readiness checks PostgreSQL, the
  queue schema, artifact-volume writability, and Ollama's non-secret
  `/api/tags` endpoint. Ollama failure is an HTTP 200 `attention` state; a core
  dependency failure is HTTP 503.
- Added `compose.yaml` with PostgreSQL, API, code-review worker, academic
  planner worker, and finance worker. Only the API is published, on
  `127.0.0.1`; Ollama remains host-native.
- Added a pinned Tailwind 3.4.17 build stage that emits minified
  `app/static/app.css` into the runtime image without shipping a browser-side
  Tailwind runtime.

## Acceptance evidence

Validated on 2026-09-03:

- `uv lock --check`: passed with uv 0.12.6 and Python 3.12.14.
- `uv sync --locked`: passed; 67 packages resolved.
- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: passed.
- `uv run pyright app`: passed with zero errors and warnings.
- `uv run pytest -q`: 3 tests passed.
- `docker compose config --quiet`: passed.
- `docker compose up -d --build`: built the shared image and started all five
  services.
- `curl --fail http://127.0.0.1:8000/health/ready`: HTTP 200; PostgreSQL,
  Procrastinate, artifacts, and Ollama reported healthy.
- `docker compose ps`: PostgreSQL and API healthy; all three named workers up.

The Ollama-unavailable degraded response and diagnostic redaction are covered
by unit fixtures, so normal bootstrap does not require Ollama to be running.

## Decisions and risks

- The single local API replica is the controlled Alembic bootstrap point in
  Phase 0. Phase 2 must move schema coordination to the durable queue/runtime
  design before any horizontal API scaling.
- Workers intentionally idle until Phase 2 adds the durable Procrastinate app.
- The local Ollama HTTP endpoint is available to containers, but the host CLI
  is not currently on `PATH`. Phase 1 must identify the installed model and
  complete the required benchmark before pinning a model digest or settings.

## Next phase

Phase 1 adds the sole model gateway, versioned evaluations, telemetry,
single-request concurrency gate, bounded structured-output repair, and the
benchmark/digest gate.
