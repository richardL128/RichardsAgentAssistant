# Implement semantic misc-calendar routing

## Objective and user-visible outcome

Add first-class support for one reserved `misc` row in the configured Notion Courses
database. The row owns the same seeded inline Assessments calendar as every academic
course row. When the owner sends a dated or timed personal/general to-do through the
authorized Discord bot—for example, `scrub the toilets @6:00 pm tdy`—the model must
semantically recognize that the item is unrelated to Jobs/career and unrelated to a
specific course, then prepare the item for the `misc` calendar.

The user must receive the existing prompt acknowledgement/progress lifecycle and a
clear confirmation preview. No Notion write occurs until the existing exact
`confirm <proposal-id>` flow succeeds. After synchronization, misc events participate
in calendar reads and the morning briefing without being mislabeled as coursework.

## Requirements

1. Reserve exactly one active Courses row whose normalized title is `misc` as the
   miscellaneous calendar target. Match case-insensitively and ignore surrounding or
   repeated whitespace, but do not use fuzzy matching and do not hardcode its Notion
   page/database/data-source ID.
2. Reuse the existing seeded-calendar contract: the `misc` page must own exactly one
   discoverable Assessments/Assessment Calendar child database with valid Name and Date
   properties.
3. Add a dedicated model tool, `create_misc_task`, with only a task title and local
   wall-clock due time. The host—not the model—must synchronize the catalog, resolve the
   unique reserved misc row, and bind the proposal to its opaque course ID.
4. Update the native Discord system policy so dated/timed calendar creation requests are
   semantically sorted among:
   - Jobs/career tools only for job applications, interviews, employers, roles, or other
     career context;
   - academic course tools only when the task is clearly tied to coursework or a course;
   - `create_misc_task` when the task is a personal, household, administrative, errand,
     or otherwise general to-do unrelated to Jobs and coursework;
   - one concise clarification question when the target is genuinely ambiguous.
5. Keep ordinary non-calendar questions on the direct-answer path. Do not turn every
   unrelated message into a misc task.
6. Represent created misc items as task events with canonical `Task — <title>` titles so
   proposal previews and later syncs remain intelligible.
7. Fail closed with an actionable, model-visible tool error when the misc row is missing,
   duplicated, inactive, or lacks a valid seeded calendar. Never fall back to a course or
   Jobs calendar.
8. Preserve the existing authorization, acknowledgement, progress, idempotency,
   proposal, exact-confirmation, optimistic-write, and Notion discovery boundaries.
9. Treat synchronized misc events as a distinct calendar source area and render them in
   an `Upcoming miscellaneous tasks` morning-briefing section, while continuing to use
   the academic store for their semantic cache and persistence.
10. Keep misc task deadlines out of the deterministic academic study-block allocator;
    they are calendar to-dos, not coursework that requires preparatory study blocks.
11. Preserve all unrelated dirty-worktree changes.

## Explicit non-goals

- Do not create a new top-level Notion database or add another configured database ID.
- Do not make runtime requests automatically create or seed the Notion `misc` row. The
  one-time feature rollout may create it explicitly, then runtime discovery owns it.
- Do not bypass confirmation or write to Notion directly from semantic classification.
- Do not use keyword-only parsing of the Discord sentence to choose between Jobs,
  coursework, and misc.
- Do not retain a fallback that silently puts unresolved misc tasks into an academic or
  Jobs calendar.
- Do not redesign the Jobs workspace, the Discord ingress/wake architecture, or the
  existing per-course calendar schema.

## Current architecture and repository context

- `NotionConnector.discover_course_assessments` already discovers every active non-Jobs
  Courses row and its seeded Assessments database. Therefore a valid `misc` row already
  reaches `AcademicNotionSync` without connector changes.
- `SQLAlchemyAcademicPlannerStore.search_courses` currently exposes all valid writable
  course calendars, but the active native harness tells the model not to use academic
  tools for unrelated requests. There is no safe, dedicated misc target today.
- `DiscoveredAcademicNotionWriter` already creates confirmed events in any resolved,
  valid per-course data source and can be reused unchanged.
- The native handler in `app/agents/academic_planner/discord_harness.py` is the active
  semantic/tool boundary. The host coordinator and wake/handoff layers intentionally
  preserve the authorized message and should remain non-semantic durability/security
  boundaries.
- Morning calendar contracts currently distinguish only `course` and `jobs`; misc must
  become a third source area while still saving semantics through the academic store.

## Expected files and components to change

- `app/agents/academic_planner/contracts.py`
  - add task assessment semantics, a misc-aware course role, and an internal
    `CreateMiscTaskCall` contract.
- `app/agents/academic_planner/discord_harness.py`
  - add `create_misc_task`, progress mapping, semantic sorting policy, local-time
    validation, unique host-side misc resolution, and mutation recording.
- `app/agents/academic_planner/proposal_validation.py`
  - validate the host-resolved misc role and convert the call into a canonical task
    `ProposedChange`.
- `app/db/academic.py`
  - expose a bounded unique-misc lookup over active valid seeded calendars; label
    synchronized misc calendar items with source area `misc`.
- `app/agents/academic_planner/sync.py`
  - persist entries from the reserved misc row as task events without academic
    assessment-type clarification prompts.
- `app/agents/calendar_briefing/contracts.py`
  - add the `misc` source area.
- `app/agents/academic_planner/morning_notification.py`
  - keep misc semantic-cache writes on the academic store and render a separate misc
    section.
- Focused unit/acceptance tests under `tests/unit/` and, where practical, the existing
  active native Discord boundary tests.
- `ARCHITECTURE MAIN.md` and the Notion setup documentation in
  `docs/operations/getting-started.md`.

No database migration is expected: the reserved role is derived from the authoritative
Notion row title, and the existing `assessments.assessment_type` column accepts bounded
string values. If implementation evidence contradicts this, stop and update this plan
before introducing a migration.

## Ordered implementation steps

1. Add shared normalization/role helpers and typed contracts for course versus misc
   targets, task events, and the internal misc-create mutation call.
2. Add a store query that returns at most two valid active reserved misc targets so the
   harness can distinguish missing, unique, and duplicate states. Do not depend on the
   generic 20-row course-search limit.
3. Add the dedicated native `create_misc_task` schema and handler. Synchronize once,
   resolve the reserved row host-side, add the resolved option to the turn's verified
   course map, localize the owner's wall time, and record the internal mutation.
4. Extend proposal validation to require that the resolved course is explicitly role
   `misc`, require a future due time, build `Task — <title>`, and emit the normal
   `create_assessment` ProposedChange for the existing writer/confirmation path.
5. Rewrite the relevant native system/tool descriptions to require semantic sorting and
   prohibit silent fallback between target areas. Keep direct answers intact.
6. During Notion sync, derive the reserved role from the course title. Persist misc
   calendar rows as task events and suppress academic-type clarification for them; retain
   date/schema clarification and normal error handling.
7. Add `misc` to calendar source contracts, item construction, semantic persistence
   routing, and morning rendering.
8. Exclude `task` assessments from academic planner facts so chores never consume study
   availability or appear as generated study blocks.
9. Update architecture/setup documentation with the exact row-name and seeded-calendar
   requirements and the confirmation behavior.
10. Add regression tests and run focused static/test validation, then the repository-level
   suite appropriate to the changed files.

## Migration and compatibility considerations

- Existing courses and Jobs retain their current behavior and storage.
- Existing databases need no schema migration.
- Deployments without a `misc` row continue to answer ordinary messages; only an attempted
  misc calendar creation fails with actionable setup guidance.
- Exactly one valid `misc` row is required for writes. Duplicate or malformed rows are an
  error, not an arbitrary selection.
- Existing proposal payloads remain compatible because the final persisted change still
  uses `field="create_assessment"`; only the new `task` assessment-type value is added.
- The existing Notion writer remains the sole confirmed write path.

## Validation steps

1. Contract/unit tests:
   - task and misc role validation;
   - canonical `Task — <title>` proposal conversion;
   - model cannot provide or spoof the misc course ID;
   - past or timezone-bearing tool timestamps fail as existing local-time contracts do.
2. Store/sync tests:
   - one active valid `misc` row resolves;
   - missing, duplicate, inactive, and invalid-calendar misc rows do not resolve;
   - misc entries sync as task events without academic type clarification;
   - normal course and Jobs discovery remain unchanged.
3. Active Discord boundary tests with representative text:
   - `scrub the toilets @6:00 pm tdy` produces acknowledgement/progress, invokes
     `create_misc_task`, and yields a confirmation proposal targeting only `misc`;
   - an ECE/course task still uses the academic course flow;
   - an interview/job task still uses career tools;
   - a normal unrelated question returns a direct answer and no proposal;
   - missing/duplicate misc setup returns a clear failure and no proposal/write.
4. Confirmation/write test:
   - exact confirmation applies one page create to the resolved misc child data source;
   - rejection and replay remain safe.
5. Morning test:
   - misc items render under `Upcoming miscellaneous tasks`, course items under course
     dates, and Jobs items under job events;
   - misc semantic results persist via the academic store, never the career store.
6. Run Ruff and Pyright for changed production files, focused Pytest suites, then the full
   test suite if the environment supports it.
7. If the configured real Discord/Notion/Ollama environment is available, send the exact
   representative Discord message and verify acknowledgement, proposal, confirmation,
   and final Notion placement. If unavailable, state clearly that live end-to-end behavior
   remains unverified rather than claiming deployment success.

## Acceptance criteria

- A representative personal chore is semantically routed to the reserved misc calendar,
  never a Jobs or academic course calendar.
- The proposal visibly identifies `misc`, uses a task title, and requires exact
  confirmation before the write.
- The host chooses the unique misc target from synchronized authoritative data; the model
  cannot inject an arbitrary target ID.
- Missing, duplicate, or malformed misc setup cannot cause a write elsewhere.
- Existing academic, Jobs, direct-answer, acknowledgement/progress, and confirmation
  behaviors remain covered and passing.
- Synced misc items are shown separately in the morning briefing and use the academic
  semantic cache path.
- Documentation explains how to create/share the reserved row and seeded calendar.

## Known risks and unresolved decisions

- Semantic correctness ultimately depends on the configured model choosing the dedicated
  tool according to the system policy. Host validation guarantees target integrity after
  the tool is chosen, but cannot prove the model's classification quality from mocked
  tests alone. A real Discord/Ollama test is required for a deployment-level claim.
- The current worktree contains unrelated in-progress edits in several relevant files.
  Implementation must patch narrowly and review diffs carefully.
- If more than 20 course calendars exist, generic course search remains bounded, but misc
  creation is unaffected because its dedicated lookup bypasses that limit.

No product decision is currently unresolved; the explicit request to write the plan and
then execute it is treated as approval to proceed in this session. During execution, the
configured Notion integration created exactly one `misc` Courses row and its inline
`Assessments` data source; a subsequent production discovery read verified one unique
valid target with `Name` and `Date` properties.
