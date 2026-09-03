# CLAUDE.md — LifeAgent

Instructions for Claude Code working in this repository. `AGENTS.md` covers
multi-agent orchestration; `.claude/sandbox.md` covers the containerized
sandbox.

## Committing: Claude drafts, Richard commits

**Claude never commits.** Do not run `git commit`, `git push`, `git add -A`
followed by a commit, `git reset --hard`, `git rebase`, or any other command
that creates or rewrites history. Richard writes every commit himself.

At the end of every major goal — a user prompt carried through to completion,
not each intermediate step within one — do this, in order:

1. Confirm the work is actually finished and verified. If something is
   incomplete or unverified, say so before drafting anything.
2. Run `git status --short` and `git diff --stat` so the message describes what
   really changed, including untracked files.
3. Write the proposed commit message to `.claude/commit-message.txt`
   (gitignored) **and** print it in the response inside a fenced block, so it
   can be read without opening a file.
4. Ask Richard to commit, naming the command:

   ```
   Ready to commit: git commit -F .claude/commit-message.txt
   ```

If a goal produced no file changes, say so instead of drafting a message.

### Commit message format

```
<imperative subject, <= 72 chars, no trailing period>

<body: what changed and why it changed, wrapped at 72 columns. Cover
every logical change in the diff, not just the headline one. Name the
files or areas touched when that helps a reader navigate.>

<what was verified, and what was not — say plainly if something is
untested.>

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

Keep the trailer only while Claude is drafting these; drop it from
`.claude/commit-message.txt` by hand if you would rather not carry it.

## Running things

```bash
# Application stack (Postgres, API, three workers) — from the host
docker compose up -d --build
docker compose logs -f api
docker compose down

# Claude Code in its sandbox — see .claude/sandbox.md
scripts/claude-sandbox.sh login    # once
scripts/claude-sandbox.sh

# Tests and checks
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run alembic upgrade head
```

## Layout

- `app/` — FastAPI application, agents, connectors, LLM gateway, DB models and
  Alembic migrations.
- `infra/` — application image, worker entrypoints, and `claude-sandbox/` for
  the agent sandbox image.
- `compose.yaml` — application stack. `compose.claude.yaml` — sandbox only, a
  separate compose project so `docker compose up` never starts an agent.
- `scripts/` — host-side helpers, including `claude-sandbox.sh`.
- `docs/implementation/` — per-phase implementation notes.
