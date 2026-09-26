# Morning schedule composition: one unsupported row discarded valid output

Date diagnosed: 2026-09-21  
Status: confirmed design failure. The immediate `SEM` contract gap is addressed
by the accompanying code change; row-level recovery remains proposed below.

## Observed failure

The September 21 `Classes + Tutorials + Labs` morning embed contained nine
correctly ordered Google iCal events. The rendered table nevertheless showed
`Unclear`, `Unclear`, and `-` for every row, followed by the exact source titles.

The persisted source rows were not missing the required facts:

- every event had a unique event ID;
- repeated courses represented distinct lecture and tutorial events;
- eight titles used the recognized `LEC` or `TUT` codes;
- the final title was `ECE201 - SEM 001`;
- every row carried `Location: PSE 4053`.

A read-only replay against those stored inputs reproduced the failure. The
first generator candidate correctly classified the first eight events and the
location, but returned `Unclear` for the `SEM` event. The critic rejected the
whole candidate because `SEM` was not represented. The repair mapped the
seminar to `Class`; the second critic then incorrectly reported duplicate event
IDs after confusing repeated course codes with duplicate event identities.
After two global rejections, the composer returned no composition and the host
replaced every inferred field with fallback placeholders.

## Confirmed design errors

### 1. The output contract could not represent a source value

`ScheduleSessionType` allowed only `Class`, `Tutorial`, `Lab`, and `Unclear`.
The live source also uses `SEM 001`, whose user-facing type is `Seminar`. The
generator could not preserve that fact without either losing information or
misclassifying it.

### 2. Event identity and course identity were conflated

A course can have more than one event on the same day. For example, an ECE250
lecture and an ECE250 tutorial are separate valid events. Duplicate detection
must compare stable event IDs, not course codes, titles, or session types.

### 3. The trust and failure unit was the entire batch

One unsupported or rejected row caused all independently valid rows to be
discarded. The renderer already supports field-level uncertainty, but it never
received the valid rows because composition had a single global accept/reject
result. This converted a localized ECE201 type issue into a useless nine-row
table.

### 4. The model critic repeated host-verifiable checks

Host code already checks exact ordered event-ID coverage and uniqueness. The
critic was also asked to judge missing and duplicate IDs, allowing a model
mistake to override a deterministic host result.

### 5. Failure diagnostics were erased

The caller suppresses composition exceptions, and generator/critic rejection
reasons are not persisted with morning progress. The delivered embed proves
only that inference was unavailable; it does not explain which row or field
failed.

## Proposed durable fix

1. Make known schedule codes host-owned facts. Parse the bounded title format
   deterministically and map `LEC` to `Class`, `TUT` to `Tutorial`, `LAB` to
   `Lab`, and `SEM` to `Seminar`. Extract a bounded `Location:` value from the
   trusted event context. Use `Unclear` or `-` only for the field that cannot be
   parsed.
2. Keep stable event IDs as the only uniqueness key. Explicitly permit multiple
   events for the same course and date when their event IDs are distinct.
3. Reconcile composition per event and per field. A rejected course, type,
   location, or note must degrade only that field on that event. Preserve all
   independently validated values from the other events.
4. Remove identity coverage and duplicate detection from the model critic.
   Keep those checks in the existing deterministic host validator. If a critic
   remains for semantic claims, change its contract from one global `accepted`
   flag to event-scoped decisions keyed by event ID.
5. Treat optional model reasoning as enrichment, not the source of truth for
   structured codes already present in the calendar title and context. A model
   or critic failure should still render the host-parsed course, type, and
   location.
6. Persist safe composition progress: generator status, critic status,
   degraded event IDs, degraded fields, and bounded reason codes. Do not store
   secret calendar URLs or unrestricted model transcripts.

## Required regression coverage

- `SEM 001` renders as `Seminar`.
- One course may have a lecture and tutorial on the same day with distinct
  event IDs; neither is diagnosed as a duplicate.
- One unknown or rejected session type degrades only that row's type while all
  other course, type, and location values remain visible.
- One unsupported field does not erase supported fields on the same row.
- An actual duplicate event ID is rejected deterministically by host code.
- Generator or critic unavailability still renders deterministic schedule
  facts and records an actionable bounded diagnostic.

## Acceptance condition

The fix is complete when a mixed batch containing valid lectures, tutorials,
labs, seminars, repeated same-course events, and one intentionally ambiguous
row preserves every supported field and marks only the ambiguous field or row
as degraded.
