# Phase 5 — Academic planner

## Contract and acceptance criteria

Phase 5 adds the scoped academic planner: Notion delta sync for the three
configured academic databases, bounded PDF/text extraction with page citations,
PostgreSQL full-text retrieval, deterministic 7-14 day allocation, morning and
end-of-day Discord interaction, ambiguity questions, and confirmation-only
Notion writes.

The plan's acceptance tests require that:

1. Notion fixtures covering an assignment, quiz, fixed class, incomplete block,
   ambiguous PDF deadline, and DST boundary produce valid records/citations;
2. retrieval answers exact course/policy questions through full-text search
   with citations and course/term scoping, with embeddings deferred;
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
  check-in proposals, and proposed Notion changes. Datetimes are required to be
  timezone-aware and normalized to UTC at the contract boundary.
- Added `app/agents/academic_planner/allocator.py`: deterministic priority
  scoring and schedule allocation. It traverses availability windows, excludes
  ambiguous/completed assessments, protects fixed commitments with the
  configured buffer, splits work into bounded blocks, and marks incomplete work
  as carried forward.
- Added `app/agents/academic_planner/documents.py`: bounded PDF/text extraction,
  page citations, deterministic deadline-ambiguity detection, and heading/page
  chunking for transparent retrieval. Scanned PDFs are marked `ocr_required`;
  OCR is not performed in this phase.
- Added `app/agents/academic_planner/retrieval.py`: parameterized PostgreSQL
  full-text query construction and retrieval helpers that retain page/block
  citations and apply course, term, document type, and access-classification
  filters. This module intentionally contains no embedding or `pgvector` code.
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
- Added `app/agents/academic_planner/workflow.py`: morning plan orchestration,
  advisory-only model breakdown/critique calls, end-of-day check-in sending,
  conservative fallback check-in extraction, proposal creation, exact
  confirmation, and a worker entry point with explicit runtime injection.
- Added `app/api/academic.py`: HTTP boundaries for check-in proposal creation
  and exact confirmation. The check-in endpoint returns the required
  confirmation event; the confirmation endpoint applies only through the
  configured writer. `app/main.py` mounts this router with the durable SQL
  store; confirmation returns a fail-closed `503` until a scoped live writer
  is explicitly injected.

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
  embeddings, SQL repository idempotency and confirmation state transitions,
  academic API proposal/confirmation behavior, and Notion adapter scoping.
- `tests/unit/test_academic_main.py::test_main_mounts_academic_proposals_and_confirmation_fails_closed`
  proves the real application mounts proposal capture against durable storage
  while refusing confirmation when no scoped Notion writer is configured.
- Focused Phase 5 validation passed with the repository virtualenv equivalents:
  `.venv/bin/ruff check ...`, `.venv/bin/pyright app`, and `.venv/bin/pytest`
  over all assigned academic/Notion unit tests plus the Phase 5 acceptance
  tests.

## Deferred external verification

- Live Notion database sync, live attachment download, and live Notion page
  updates require Richard's scoped Notion integration token, database IDs, and
  property mappings. Automated tests use `httpx.MockTransport` and verify the
  exact outbound method, URL path, allowlisted property ID, and payload shape.
- Live Discord morning-plan, ambiguity-question, confirmation, and end-of-day
  deliveries require Richard's Discord credentials and channel IDs. Automated
  tests use injected delivery recorders and verify idempotency keys and call
  boundaries.
- Qwen academic breakdown/critique quality is advisory in this phase and is not
  used to alter constraints or calendar state. The deterministic allocator and
  confirmation gate remain authoritative.
- OCR for scanned PDFs, embeddings, `pgvector`, semantic retrieval, and hybrid
  retrieval are deferred by the implementation plan until the documented corpus
  and retrieval-quality thresholds are met.
- The application deliberately does not synthesize Notion property or page
  mappings. A live `notion_writer` must be injected only after Richard supplies
  the scoped integration token and reviewed mappings; until then confirmation
  is unavailable rather than falling back to an arbitrary write target.

## Next phase

Phase 6 finance remains gated by Richard's explicit approval of the exact eight
sources, entitlements, and licence boundaries. No finance schedule should be
enabled merely because code exists.
