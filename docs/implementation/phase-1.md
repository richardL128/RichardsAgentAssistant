# Phase 1 — model gateway and quality baseline

## Contract

Phase 1 establishes one local model boundary for every later workflow. Calls
must use `llm.gateway`, carry a stable model/config identity, respect bounded
input/output budgets and timeouts, serialize physical inference to one call,
request schema-constrained JSON, validate with Pydantic, and make at most one
repair attempt. Evaluation artifacts contain telemetry and validation results,
never prompts or raw responses.

The user directed LifeAgent to use the already-installed local model. Ollama's
authoritative tag inventory names it `qwen3-32gb:latest`; that exact tag and
digest are used instead of the plan's original `qwen3.8:27b` candidate. Ollama
remains host-native and is not a Compose service.

## Implemented

- Added the sole async `LLMGateway` using `ChatOllama`, exact model/config
  fingerprints, timeout and token budgets, process-wide semaphore one,
  per-call JSON Schema constrained decoding, reasoning disabled, and one
  bounded repair attempt.
- Added non-secret telemetry for request ID, attempt, queue/model timing,
  input/output character and token estimates, provider-reported token counts,
  validity, and safe error codes. Prompts and response bodies are excluded from
  reports.
- Added strict domain schemas for code-finding triage, finance
  fact-versus-inference, academic extraction, and generic structured output.
  Cross-field evidence, ambiguity, citation, and no-trade boundaries are
  enforced deterministically.
- Added six versioned fixtures, including malformed JSON and schema-invalid
  negative cases, deterministic semantic assertions, percentile metrics, and
  visible failure diagnostics that contain paths/types but not values.
- Added a reproducible live benchmark that verifies Ollama version, exact
  tag/digest at both ends, context, cold and warm latency, model allocation,
  host memory, swap activity, and two queued tasks with non-overlapping model
  intervals.
- Extended readiness so a configured model or digest mismatch reports a safe
  `attention` state.

## Saved benchmark and pin

Validated on 2026-09-03 with Ollama 0.33.2. The authoritative artifact is
`docs/benchmarks/phase1-baseline.json` with SHA-256
`036799e541a53f3211c3e9ab4a5bfe7d0449ee1dd17c5ca0eb25b68803c3b705`.

- Model: `qwen3-32gb:latest`
- Digest: `d039cde69ac1f5a43d5134182adfefa65bdb533362a625b936e6171a53296eb3`
- Settings: context 2,048; output 384; prompt batch 32; timeout 300 seconds;
  temperature 0; reasoning off; seed 1729; repair attempts 1; model concurrency
  1.
- Cold fixture: passed in 28.65 seconds.
- Warm baseline: 40/40 passed; p50 13.10 seconds; p95 22.52 seconds.
- Model allocation: peak 21,051,018,968 bytes, below the 28 GiB acceptance
  ceiling; the observed context was exactly 2,048.
- Concurrency: both queued markers completed, physical peak concurrency was 1,
  model intervals did not overlap, and outputs were not mixed.
- Identity: the exact tag/digest was present at start and unchanged at finish.

The benchmark passed every Phase 1 acceptance check before the defaults and
digest were pinned.

## Validation

- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: passed.
- `uv run pyright app scripts/phase1_benchmark.py`: passed with zero errors and
  warnings.
- `uv run pytest -q`: 14 tests passed after the final pin.
- The full live benchmark passed all ten acceptance checks and saved its report.
- The rebuilt Compose stack passed readiness with the exact tag/digest verified;
  all five services were running and the API was healthy.

## Decisions and risks

- Ollama clamps this installed model to a minimum 2,048-token context. A lower
  requested value was measured as 2,048 and rejected by the benchmark's context
  check.
- Sustained back-to-back inference on this 32 GiB host remains memory-heavy.
  The full run's advisory checks recorded a 9% minimum free-memory reading and
  12,924,878,848 bytes of swapouts during the warm interval. These are retained
  in the artifact as failed advisories, not omitted from the gate evidence.
- Prompt batch 32, context 2,048, output 384, reasoning off, and physical
  concurrency one are mandatory initial safeguards. Later worker scheduling
  must avoid uncontrolled bursts, and Phase 7 should surface memory-pressure
  warnings. The chosen model should be revisited if ordinary workloads show
  latency or host-pressure degradation.
- The asyncio semaphore coordinates a single process. Phase 2's durable queue
  must preserve one model-consuming job across worker processes rather than
  relying on per-process worker concurrency alone.

## Next phase

Phase 2 adds the shared durable PostgreSQL core, audit/run/delivery/evidence
records, content-addressed redacted artifacts, idempotency, retry
classification, approvals, and real Procrastinate workers.
