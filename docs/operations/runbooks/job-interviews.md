# Job interviews and research

Use this runbook when the combined morning briefing omits interview guidance or
the bot asks for Jobs, matching, Date, posting, or research setup.

## Inspect without exposing application contents

```bash
curl --fail -X POST http://127.0.0.1:8000/job-interviews/sync | jq .
curl --fail http://127.0.0.1:8000/job-interviews/health | jq .
docker compose logs --tail=200 worker-academic-planner
```

Health output contains only status, counts, and timestamps. Do not paste raw
Notion rows, tokens, Discord messages, or full posting copies into logs.

## Setup conditions

- `jobs_page_missing` / `jobs_page_duplicate`: keep exactly one active Courses
  row titled `Jobs`.
- An application-table count or active-row count of zero: add an ordinary
  Notion table to Jobs; its headers and column order are flexible.
- `interviews_database_missing` / `interviews_database_duplicate`: keep exactly
  one inline database titled `Interviews`.
- `interview_date_missing` / `interview_date_invalid`: correct that round's
  Date or answer the focused Discord clarification.
- `provider_unconfigured`: direct posting research can continue, but broad
  company search remains disabled until an approved provider is implemented and
  configured.

An expired, login-only, or blocked posting must not be bypassed. Paste or attach
the posting when asked. Existing maintained plan guidance remains available,
but a requested research refresh does not silently substitute stale evidence.

## Writes and recovery

Interview Date and preparation-plan writes never occur from sync or research.
Review the exact Discord preview, then send the displayed `confirm
<proposal-id>` event. A changed Notion page/property or LifeAgent-owned plan
section invalidates the preview; fix the source and request a fresh proposal.
Never retry an uncertain partial write blindly. Inspect the career write receipt
and the target page first; unrelated user-authored blocks must remain untouched.
