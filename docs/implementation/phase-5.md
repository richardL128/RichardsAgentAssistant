# Phase 5 — Academic planner

> The current replacement architecture keeps deterministic academic
> sync/planning authoritative. Messages from authorized owners in the private
> Discord channel, received by the native host daemon, are the sole Qwen path;
> the legacy model-based morning
> briefing schedule is non-executable. The automatic academic morning to-do notification is
> executable and model-free.

## Contract and acceptance criteria

Phase 5 adds the scoped academic planner: per-course Notion assessment sync,
private assessment-body/PDF ingestion with page or block citations, bounded OCR,
assessment-scoped lexical and local-vector retrieval, deterministic 7-14 day allocation,
authorized private-channel academic conversation, ambiguity
questions, and confirmation-only Notion writes.

The plan's acceptance tests require that:

1. Notion fixtures covering an assignment, quiz, fixed class, incomplete block,
   ambiguous PDF deadline, and DST boundary produce valid records/citations;
2. exact lookup remains available through full-text search while semantic
   assessment-material queries use local embeddings and cannot cross assessments;
3. the allocator never moves tests, deadlines, or fixed commitments, preserves
   configured buffers, and carries incomplete work forward visibly;
4. ambiguous facts become questions and are excluded from automatic hard
   constraints; and
5. natural-language check-ins create proposals, make no Notion writes until the
   exact confirmation event is supplied, and are replay-safe after application.

## Implemented

- Added `app/agents/academic_planner/contracts.py`: strict Pydantic contracts
  for assessments, fixed commitments, availability windows, incomplete blocks,
  ambiguity questions, study blocks, daily plans, work breakdowns, critiques,
  historical grounded morning briefing data, scheduled model-free morning
  notification data, check-in proposals, and proposed Notion changes.
  Datetimes are required to be timezone-aware and normalized to UTC at the
  contract boundary.
- Added `app/agents/academic_planner/allocator.py`: deterministic priority
  scoring and schedule allocation. It traverses availability windows, excludes
  ambiguous/completed assessments, protects fixed commitments with the
  configured buffer, splits work into bounded blocks, and marks incomplete work
  as carried forward.
- Added `app/agents/academic_planner/documents.py`: PDF/text media sniffing,
  a strict 15-page assessment bound, layout-aware extraction, injectable local
  OCR, explicit partial coverage, Notion block citations, and heading/page
  chunking.
- Added `app/agents/academic_planner/retrieval.py`: parameterized full-text
  lookup plus assessment-owned exact-cosine retrieval and guarded chunk reads.
  Production uses `pgvector`; SQLite tests use deterministic JSON vectors.
- Added `material_ingestion.py` and `material_reasoning.py`: content-addressed
  private artifacts, immutable source versions, last-good activation,
  identifier-only jobs, local embeddings, a bounded read-only tool loop,
  free-form cited insights, and entailment/relevance/prompt-injection critics.
- Added `app/connectors/notion.py`: least-privilege Notion reads limited to the
  configured courses, assessments, and study-block databases; bounded child
  block and attachment reads; allowlisted attachment hosts; allowlisted
  property IDs; and a concrete `AcademicNotionWriter` that patches only mapped
  properties after the workflow passes the exact confirmation event.
- Added `app/db/academic.py`: idempotent academic upserts, document chunk
  replacement, SQLite/PostgreSQL lexical retrieval, sync cursors, study plan
  and block persistence, incomplete-block carry-forward, durable check-in
  proposals, exact confirmation claiming, replay-safe applied state, and audit
  events for confirmed Notion writes.
- Added `app/agents/academic_planner/workflow.py`: plan orchestration,
  advisory-only model breakdown/critique calls, deterministic briefing context
  construction for historical tests, end-of-day check-in sending, conservative
  fallback check-in extraction, proposal creation, exact confirmation, and a
  worker entry point with explicit runtime injection. The briefing context
  contains only bounded normalized facts: current Toronto-local date labels,
  known assessment IDs, scheduled block IDs with exact durations, active
  learning-focus labels, explicitly confirmed performance signals, and deferred
  IDs.
- Added `app/agents/academic_planner/morning_notification.py`: executable
  deterministic morning to-do notification. It validates the stable period key
  `academic-morning:YYYY-MM-DD:HHMM:v1`, refreshes Notion immediately before
  planning, requires a fresh complete sync, builds the intended local day's
  deterministic plan, and sends one Discord message with delivery key
  `academic-morning-delivery:YYYY-MM-DD:HHMM:v1`. It never calls Qwen.
- Added `app/api/academic.py`: the original phase shipped HTTP boundaries for
  check-in proposal creation and exact confirmation. The replacement runtime
  has removed the deterministic `/academic/checkin` creator; proposals now
  originate only in the native Discord harness. Exact confirmation, rejection,
  and manual sync remain mounted, and confirmation returns a fail-closed `503`
  until a scoped live writer is explicitly injected.

## Acceptance evidence

- `tests/acceptance/test_phase5_academic_planner.py::test_phase5_academic_planner_acceptance_contract`
  seeds assignment, quiz, fixed class/test, incomplete block, conflicting-date
  PDF facts, scoped document chunks, and a concrete mocked Notion writer. It
  proves cited course/term lexical retrieval, ambiguity as a delivered question
  rather than a hard constraint, fixed deadline/commitment immutability,
  protected buffers, visible carry-forward, zero Notion PATCHes before exact
  confirmation, exactly one PATCH after confirmation, and no duplicate PATCH on
  replay.
- `tests/acceptance/test_phase5_academic_planner.py::test_phase5_allocator_handles_toronto_dst_boundaries_with_zoneinfo`
  uses real `ZoneInfo("America/Toronto")` spring-forward and fall-back
  datetimes, including distinct `fold=0` and `fold=1` repeated-hour
  commitments, and verifies allocation remains UTC-normalized and conflict
  free.
- Unit coverage includes allocator priority/buffer/carry-forward behavior,
  document extraction/chunking/ambiguity, retrieval query scoping without
  embeddings, morning-briefing grounding and invalid-output rejection, SQL
  repository idempotency and confirmation state transitions, academic API
  proposal/confirmation behavior, and Notion adapter scoping.
- `tests/unit/test_academic_main.py::test_main_mounts_academic_proposals_and_confirmation_fails_closed`
  proves the real application mounts proposal capture against durable storage
  while refusing confirmation when no scoped Notion writer is configured.
- Focused Phase 5 validation passed with the repository virtualenv equivalents:
  `.venv/bin/ruff check ...`, `.venv/bin/pyright app`, and `.venv/bin/pytest`
  over all assigned academic/Notion unit tests plus the Phase 5 acceptance
  tests.
- Scheduled morning notification coverage includes controlled trigger,
  duplicate/replay idempotency, successful live-delivery boundaries through the
  injected Discord delivery adapter, source setup/failure/stale handling, and
  health classification for missing runs, failed runs, missing delivery, failed
  delivery, uncertain delivery, and on-time success.

## Deferred external verification

- Live Notion database sync, live attachment download, and live Notion page
  updates require Richard's scoped Notion integration token, database IDs, and
  property mappings. Automated tests use `httpx.MockTransport` and verify the
  exact outbound method, URL path, allowlisted property ID, and payload shape.
- Live Discord conversation, ambiguity-question, confirmation, clarification, and
  scheduled morning notification deliveries require Richard's Discord
  credentials and channel IDs. Automated tests use injected delivery recorders
  and verify idempotency keys and call boundaries. Historical morning-briefing
  contracts are not registered as a scheduled model job; the current scheduled
  morning notification is deterministic and model-free.
- Qwen academic breakdown/critique quality is advisory in this phase and is not
  used to alter constraints or calendar state. Scheduled academic model calls
  are non-executable; an authorized private-channel Discord message is the sole
  Qwen trigger.
  The automatic morning notification must not call Qwen, even on source failure
  or an empty day.
- Authorized Discord-triggered academic requests use semantic progress rather
  than token streaming. The host creates one idempotent Discord progress
  message per inbound attempt and edits it through allowlisted stages such as
  runtime wake-up, model turn, catalog lookup, proposal validation, and the
  terminal ready/clarification/failure state. The separate durable proposal or
  clarification response remains authoritative; a failed progress edit never
  turns a safe proposal into a failure or bypasses exact confirmation.
- User-answerable missing or ambiguous facts open an owner/channel-scoped
  `agent_clarification` discourse session. The original request and authorized
  answers are kept in a redacted expiring artifact, while relational state
  retains only bounded metadata and its artifact key. The same authorized user
  in the same Discord channel may answer attempt two or three as ordinary
  authorized messages; the authenticated handoff discards messages from other
  users or channels before content persistence or model work. A third
  unresolved attempt closes the session and requires a new complete request;
  runtime, connector, timeout, and invalid model-output failures close safely
  without consuming an automatic rerun.
- Exact proposal confirmation and rejection remain model-free.
- Conversation-triggered study sessions are represented as explicitly
  confirmed `Studying Block — <topic>` pages in the relevant course's
  discovered Notion Assessments calendar. The initial request and any
  owner-scoped free-text clarification continuation may omit the bot mention.
  Qwen owns
  semantic interpretation of raw create/update/archive intent, topics, dates,
  and clarification needs. Deterministic code is limited to authorization,
  typed schemas, bounded/known IDs, write preconditions, and confirmation. The
  deterministic Discord preview renders every proposed block in
  Toronto local time with course, title, start, end, and duration; confirmed
  Notion creates write Date ranges with both `start` and `end`. This path does
  not use an external calendar, a separate Notion Calendar API, or automatic
  export of planner-generated PostgreSQL `StudyBlock` rows.
- Confirmed multi-page study-session batches use the existing durable
  confirmation claim plus an operation journal keyed by proposal ID and ordinal.
  The journal records bounded receipts for definitely completed operations.
  When a batch is uncertain or partially failed, LifeAgent does not replay it
  automatically and does not report full success unless every operation has a
  receipt.
- Assessment-material OCR, embeddings, and exact `pgvector` retrieval are
  active. No approximate vector index, universal rubric schema, or scheduled
  semantic model-based morning briefing is configured. The configured academic
  morning schedule is the deterministic to-do notification, not a semantic
  model job.
- The application deliberately does not synthesize Notion property or page
  mappings. A live `notion_writer` must be injected only after Richard supplies
  the scoped integration token and reviewed mappings; until then confirmation
  is unavailable rather than falling back to an arbitrary write target.

## Next phase

Phase 6 finance remains gated by Richard's explicit approval of the exact eight
sources, entitlements, and licence boundaries. No finance schedule should be
enabled merely because code exists.
