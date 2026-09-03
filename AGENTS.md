# LifeAgent — orchestration instructions

## Agent roles

- The main Codex thread is the **Sol orchestrator**: it owns user intent,
  architecture, task breakdown, conflict resolution, and final verification.
- Use **Luna workers** for bounded implementation, codebase exploration, test
  execution, and focused review tasks. Use the `luna_worker` custom agent when
  assigning implementation work.

## Multi-agent delegation

- Delegate when a task has two or more independent workstreams that would
  benefit from parallel execution. State each worker's goal, file ownership,
  expected validation, and required summary.
- Keep the main thread responsible for integration. Workers must not make
  competing edits to the same file or make cross-cutting architectural changes.
- Prefer parallel, read-only exploration/review/testing. For write work, split
  ownership by directory or file and run integrations sequentially when the
  workstreams overlap.
- Wait for all requested workers, inspect their results and diffs, then run
  final repository-level validation before reporting completion.
- For a small, single-file, or tightly coupled change, work in the main thread
  instead of delegating merely for its own sake.

## Frontend

For work under `frontend/`, also follow `frontend/AGENTS.md`; its more specific
product and security rules take precedence.
