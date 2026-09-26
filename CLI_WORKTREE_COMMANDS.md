# LifeAgent Codex CLI worktree commands

This runbook is for `codex-cli 0.155.1`, which exposes `--worktree` for both
interactive sessions and `codex exec`. It keeps the main LifeAgent checkout as
the integration checkout and gives each independent top-level coding task a
managed Git worktree.

## Safety rules

- Start one top-level `codex --worktree` process per independent write stream.
- Subagents inside one Codex session share that session's worktree.
- Keep the main checkout clean before using it as the source for new tasks.
- Only one active task may edit `app/db/migrations/versions/` at a time.
- Worker worktrees should not run the persistent LifeAgent Compose stack. Let
  pull-request CI validate against its fresh PostgreSQL service.
- Tell every task to create its own branch, commit, and push before finishing.

## 1. One-time verification

```sh
cd /Users/richardliu/Desktop/LifeAgent
codex --version
codex --help | grep -- --worktree
git remote -v
git status --short --branch
```

The Codex help output must contain `--worktree`. If `git status` shows changes,
finish that work or commit only its owned files. Do not blindly run `git add .`,
`git stash`, reset, or cleanup commands while agents are active.

## 2. Install uv if missing

```sh
command -v uv
```

If that prints nothing:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv --version
```

## 3. Add short commands to zsh

```sh
nano "$HOME/.zshrc"
```

Add:

```sh
export LIFEAGENT_REPO="/Users/richardliu/Desktop/LifeAgent"
alias law='codex -C "$LIFEAGENT_REPO" --worktree'
alias lawe='codex exec -C "$LIFEAGENT_REPO" --worktree'
```

Reload and verify:

```sh
source "$HOME/.zshrc"
type law
type lawe
```

## 4. Prepare main once before a batch of tasks

```sh
cd "$LIFEAGENT_REPO"
git status --short --branch
```

Continue only when the checkout is clean:

```sh
git switch main
git pull --ff-only origin main
```

## 5. Start an interactive isolated task

```sh
law 'Implement the calendar retry change.

Before editing, create and switch to branch agent/calendar-retry.
Follow every applicable AGENTS.md.
Do not edit app/db/migrations/versions unless this task is the designated migration owner.
Do not run the persistent Docker Compose stack or use a shared development database.
Run uv sync --frozen if the worktree has no usable virtual environment.
Run focused validation and the relevant ruff, pyright, and pytest checks.
Commit the complete change and run:
git push -u origin agent/calendar-retry
Return the branch, commit SHA, changed files, validation results, and unresolved risks.'
```

Use a different branch for every concurrent task.

## 6. Start an unattended isolated task

```sh
lawe 'Implement this bounded task.

Task: add focused unit tests for calendar retry behavior.
Branch: agent/calendar-retry-tests
Owned files: tests/unit/test_calendar_retry.py

Before editing, create and switch to the named branch.
Follow every applicable AGENTS.md.
Do not edit files outside the stated ownership.
Do not edit app/db/migrations/versions.
Do not run the persistent Docker Compose stack or use a shared database.
Run uv sync --frozen if needed, validate, commit, and run:
git push -u origin agent/calendar-retry-tests
Return the commit SHA and validation summary.'
```

Run additional independent tasks in separate terminals.

## 7. Inspect or resume sessions

```sh
codex agents -C "$LIFEAGENT_REPO"
cd "$LIFEAGENT_REPO"
codex resume
```

## 8. Open a pull request

GitHub CLI is not currently installed. Open the compare page for the pushed
branch, replacing the encoded branch suffix:

```sh
open 'https://github.com/richardL128/RichardsAgentAssistant/compare/main...agent%2Fcalendar-retry?expand=1'
```

The existing pull-request CI runs quality checks and integration tests with a
fresh PostgreSQL service.

### Optional fully terminal-based PR flow

If Homebrew is installed:

```sh
brew install gh
gh auth login
gh auth status
```

Then:

```sh
gh pr create --base main --head agent/calendar-retry --fill
gh pr checks agent/calendar-retry --watch
gh pr merge agent/calendar-retry --merge --delete-branch
```

Merge only after review and required CI succeed.

## 9. Refresh main after merges

```sh
cd "$LIFEAGENT_REPO"
git status --short --branch
git switch main
git pull --ff-only origin main
```

The checkout must be clean first. There is no need to rebuild every worker
environment. New worktree sessions start from the updated `main` HEAD.

## 10. Sole migration-owner task

```sh
law 'Act as the sole migration owner for this batch.

Before editing, create and switch to branch agent/schema-migration.
Own app/db/migrations/versions/ exclusively.
Create the required Alembic migration without rewriting migration history.
Validate upgrade-to-head using an isolated disposable PostgreSQL database or pull-request CI.
Run relevant tests, commit, and run:
git push -u origin agent/schema-migration
Return revision identifiers, commit SHA, and validation results.'
```

Never let two tasks independently create the next numbered migration.

## 11. Occasional cleanup

Cleanup is periodic, not required after every merge:

```sh
cd "$LIFEAGENT_REPO"
git worktree list
```

Inspect a worktree before removal:

```sh
git -C '/absolute/path/from-git-worktree-list' status --short --branch
```

Only when it is clean and its work is committed and pushed:

```sh
git worktree remove '/absolute/path/from-git-worktree-list'
git worktree prune
```

Delete a local task branch only after Git confirms it is merged:

```sh
cd "$LIFEAGENT_REPO"
git branch --merged main
git branch -d agent/calendar-retry
```

Do not use force-removal, `git branch -D`, `git clean`, or recursive filesystem
deletion as routine cleanup.

## Daily minimal workflow

```sh
cd "$LIFEAGENT_REPO"
git status --short --branch
git pull --ff-only origin main
law 'YOUR TASK, UNIQUE BRANCH, FILE OWNERSHIP, VALIDATION, COMMIT, AND PUSH INSTRUCTIONS'
```

Then create the PR, let CI validate it, merge, and refresh `main` before the
next batch.

## References

- https://learn.chatgpt.com/docs/codex/cli
- https://learn.chatgpt.com/docs/environments/git-worktrees
- https://docs.astral.sh/uv/getting-started/installation/
- https://github.com/cli/cli/blob/trunk/docs/install_macos.md
