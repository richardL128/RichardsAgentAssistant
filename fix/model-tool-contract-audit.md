# Model tool contract audit: stale dates, partial sync, and ungrounded answers

Status: historical investigation findings from 2026-09-19. The replacement
query-contract architecture was implemented on 2026-09-21, followed by the
host-owned batch lifecycle and source-scoped partial-result architecture on
2026-09-22. This document preserves the evidence that motivated those changes;
it is not an active runtime contract. Issue 10 is a subsequent design finding
recorded alongside the original audit.

## Executive summary

The legacy-date responses are not primarily a clock or prompt-date problem.
The active prompt supplied the correct owner-local date, but the academic read
tools expose an unsafe retrieval contract:

- `search_courses(query)` discards `query` and returns up to 20 active courses.
- `search_assessments(query, course_id)` discards `query`, does not apply a time
  window or completion filter, orders oldest first, and returns up to 20 rows.
- The shared native harness serializes every tool result through a 4,096
  character ceiling. Oversized results are replaced by a lossy prefix, so the
  oldest rows can remain while current or future rows disappear.
- The final-response validator checks conversation lifecycle shape, not whether
  dates and items in the answer are supported by the tool result.

The result is a deterministic data-contract error followed by an ungrounded
model answer. Fixing individual Notion database queries or adding prompt text
would leave the same failure mode in other tools.

## Point-in-time runtime surface

The audited runtime exposed 20 tools:

### Academic tools

1. `search_courses`
2. `search_assessments`
3. `inspect_inbound_pdf`
4. `search_pending_assessment_creates`
5. `search_assessment_materials`
6. `create_assessment`
7. `create_misc_task`
8. `attach_material_to_assessment`
9. `find_course_event_slots`
10. `create_course_event`
11. `update_assessment`
12. `archive_assessment`

### Career tools

1. `search_jobs_context`
2. `search_job_interviews`
3. `prepare_job_interview`
4. `propose_interview_date`
5. `propose_interview_plan_save`

### Memory and lifecycle tools

1. `manage_academic_memory`
2. `manage_user_memory`
3. `emit_conversation_response`

The LEARN bridge was disabled in the audited runtime. If enabled, it would add
`search_learn_courses`, `get_learn_scheduled_items`, and
`get_learn_announcements`. The LEARN proposal tool is not currently assembled
because the proposal builder is absent.

Across the ten retained native sessions, 36 calls were observed:

| Tool | Calls |
| --- | ---: |
| `search_assessments` | 20 |
| `search_courses` | 8 |
| `emit_conversation_response` | 4 |
| `search_jobs_context` | 2 |
| `search_job_interviews` | 1 |
| `search_pending_assessment_creates` | 1 |

No mutation or proposal tool was called in those retained sessions.

## Confirmed incident behavior

The request “What are today's to dos” ran at 2026-09-19 23:09 in
`America/Toronto`. The system context contained that correct local date. The
model called:

```text
search_courses({"query":"today"})
search_assessments({"query":"today"})
```

The assessment result began with September 12, 14, 15, and 17 rows before a
September 19 row. The model then called September 15 and 17 items “today.” The
follow-up complaint caused the same broad reads and an even broader stale list.

At audit time, the database had one active, unarchived assessment due on
September 19 Toronto. The failure therefore occurred between the user request,
the read-tool contract, and final answer grounding—not because the database had
no current row.

## Confirmed issues

### 1. Academic search arguments are false affordances — critical

`SQLAlchemyAcademicPlannerStore.search_courses()` and
`search_assessments()` both execute `del query`. The schemas advertise search,
but the store returns bounded inventories for model-side selection.

The assessment query filters active/unarchived/Notion-backed rows, but it does
not filter:

- owner-local date or date range;
- `completed == false`;
- overdue versus upcoming scope;
- semantic or lexical query match.

It sorts by `due_at` ascending before a hard limit of 20. This makes stale rows
the most likely rows to survive both the database limit and later payload
truncation.

Primary evidence:

- `app/db/academic.py`, `search_courses`
- `app/db/academic.py`, `search_assessments`
- `app/agents/academic_planner/discord_harness.py`,
  `_SearchCoursesArgs` and `_SearchAssessmentsArgs`

### 2. Harness truncation destroys result semantics — critical

`app/agents/harness.py` applies `_safe_json()` to every successful result. A
payload over `MAX_EVENT_JSON_CHARS == 4096` becomes:

```json
{"truncated":true,"preview":"...prefix only..."}
```

This is not pagination. It removes result boundaries, counts, later rows, and
completeness guarantees. All inspected successful `search_assessments` calls
were truncated previews. The same failure can affect any broad tool result.

### 3. Tool results do not prove freshness or completeness — high

Academic search returns a raw list with no structured `as_of`, timezone,
applied filters, source freshness, result count, `has_more`, omitted count, or
cursor. The model cannot distinguish “these are all of today's items” from
“these are the first rows of an incomplete inventory.”

### 4. Final answers are not fact-grounded — critical

The lifecycle validator verifies terminal-tool shape, disposition, repeated
clarifications, and bounded conversation behavior. It does not verify that:

- mentioned assessment IDs came from a successful current-turn result;
- stated dates equal host-rendered dates;
- an item satisfies the requested temporal scope;
- stale or partial-sync warnings are disclosed.

The model is therefore both selector and narrator of deterministic facts.

Primary evidence:

- `app/agents/harness.py`, `_validate_lifecycle_turn`
- `app/agents/academic_planner/discord_harness.py`,
  `_validate_conversation_lifecycle`

### 5. Academic freshness is all-or-nothing — high

`_ensure_catalog_current()` permits reads only when the top-level sync status is
`succeeded`. A single unavailable calendar or role makes the result `partial`
and blocks every ordinary academic catalog search, including unrelated healthy
sources.

This produced the user-visible diagnostics:

- `assessment_calendar_missing`
- `learn_context_property_invalid`

Failing closed is safer than silently trusting cached rows, but the trust unit
is too broad. `AcademicNotionSyncResult` already reports `unavailable_roles`,
and `AcademicCourseCalendar` stores discovery status and `last_synced_at`; the
read boundary does not use that information to prove freshness for the exact
requested scope.

### 6. Career context repeats the broad-dump pattern — high

`search_jobs_context` validates and echoes `args.query` but does not use it to
filter interviews or application tables. It loads broad cached/current context
and is vulnerable to the same 4,096-character prefix truncation.

`search_job_interviews` is better: its store applies the query to upcoming
interviews. Career sync also intentionally falls back to cached data on several
failure states, but no host validator guarantees that the model communicates
the stale-data warning.

Primary evidence:

- `app/agents/job_interviews/agent_loop.py`, `_search_jobs_context`
- `app/db/job_interviews.py`, `search_interviews`

### 7. Partial-sync runs can also fail the terminal protocol — medium

In retained partial-sync sessions, the model produced useful plain assistant
text but did not call `emit_conversation_response`. The harness consequently
ended with `native_harness_failed`. This is separate from the retrieval error,
but it turns a diagnosable data failure into a generic agent failure.

### 8. LEARN capabilities are not checkpointed by the active harness — medium,
conditional

`LearnToolState` implements checkpoint export/restore, but the active Discord
harness saves only academic and career tool state. If LEARN is enabled, a
resumed conversation can lose its host-validated course capabilities.

### 9. Tests preserve implementation behavior, not user semantics — high

The focused suite passed (`91 passed`), but current tests explicitly accept
arbitrary course/assessment queries returning inventory rows. There is no
cross-tool contract test proving that “today” excludes past, tomorrow, or
completed records, or that a final answer is a grounded subset of returned
facts.

### 10. Schedule composition is all-or-nothing — high

A subsequent 2026-09-21 incident exposed the same overly broad failure-unit
pattern in the morning `Classes + Tutorials + Labs` output. One
`ECE201 - SEM 001` row could not be represented by the original session-type
contract. The critic rejected the complete schedule, then incorrectly treated
repeated course codes as duplicate event IDs during repair. The host consequently
discarded valid course, type, and location results for every other row and
rendered the entire table as `Unclear / Unclear / -`.

The trust unit must be the independently identified event and field, not the
whole schedule category. Multiple events for the same course and day are valid
when their stable event IDs differ. Host code must own identity coverage and
duplicate detection; a model critic must not override those deterministic
checks or invalidate unrelated supported rows.

The reproduced runtime evidence, design errors, and proposed row-level repair
are documented in
[`morning-schedule-composition-failure.md`](morning-schedule-composition-failure.md).

## Tool areas that are comparatively well guarded

The audit did not find the same deterministic query defect in every tool:

- inbound PDF inspection is owner/channel scoped and bounded;
- pending assessment-create search applies its query and is bounded;
- assessment-material search requires a current-turn assessment capability;
- academic and career mutation tools create confirmation-gated proposals and
  validate host-issued capabilities;
- user and academic memory tools bind identity and raw owner text at the host;
- `search_job_interviews` applies query matching to upcoming interviews;
- enabled LEARN schedule reads use structured date windows and course
  capabilities, although their timezone and checkpoint behavior still require
  validation during the replacement.

These safeguards should be retained while the read/query architecture is
replaced.

## Required fix direction

The implementation must replace the shared contract rather than patch specific
database strings:

1. Resolve temporal intent into a host-owned typed scope and exact owner-local
   time window.
2. Apply deterministic date, completion, ownership, source, and course filters
   before ranking and limiting.
3. Return a bounded result envelope with freshness and completeness proof.
4. Use real pagination; never give the model a syntactically truncated prefix.
5. Ground date-sensitive final responses in returned IDs and host-rendered
   dates.
6. Scope freshness failures to the requested sources.
7. Apply the same contract to academic, career, and enabled LEARN reads.
8. Add end-to-end contract tests spanning user text, tool arguments, store
   filters, payload serialization, checkpoint resume, and final response.

The implementation superseded its local historical plan. Current runtime
behavior is documented in `ARCHITECTURE MAIN.md` and the affected operations
runbooks.
