---
name: codebase-grounding
description: Ground answers about a repository's technology stack, feature status, implementation state, or agent configuration in the current codebase. Use when answering repository-specific technical or status questions; do not treat plans, prompts, legacy notes, or assumptions as factual evidence.
---

# Codebase Grounding

Base repository-specific claims on the current implementation.

## Evidence rules

- Inspect the codebase before answering specifics about the technology stack, feature status, implementation state, runtime wiring, or agent-related configuration and specifications.
- Treat executable code, active configuration, manifests and lockfiles, tests, migrations, deployment definitions, and current entrypoints as primary evidence.
- Do not ground explanations or conclusions in implementation plans, phase plans, prompts, legacy Markdown, handoff notes, roadmaps, or unstated assumptions. These may be used only as search leads or explicitly labeled historical context.
- Verify that a configured component is actually wired into an active path before describing it as implemented or enabled. Distinguish code present, configured, tested, deployed, and observed running when the question depends on that difference.
- Prefer focused repository searches such as `rg` and `rg --files`, then read the relevant source files rather than inferring from filenames or summaries.
- When sources conflict, report the conflict and favor the active runtime path. If the implementation does not establish an answer, say that it is unknown instead of filling the gap from a plan or assumption.
- Support material conclusions with the relevant file paths and line references when practical.

Plans and prompts can explain intended history, but they cannot establish the repository's present behavior or status.
