# Academic semantic discourse memory

## Scope and decisions

- The feature is available only to the academic Discord agent. Finance and code-review
  workflows do not import or query its memory tables.
- An explicit academic struggle creates or reinforces a learning focus and schedules a
  separate practice block. It never relabels an assessment block as practice.
- Reflections are stored as bounded raw text together with a local Ollama embedding,
  model identity, vector dimensions, and non-secret embedding telemetry.
- Clearing a focus is a hard delete of the focus, its reflection text, its vectors, and
  its lifecycle events. Historical study blocks remain, but their deleted-focus foreign
  key is cleared and their `practice` kind is retained.
- The configurable default practice duration is 30 minutes.

## Runtime flow

1. An authorized Discord message first passes through the academic memory discourse
   loop. Non-memory messages fall through to the existing academic/Notion flow.
2. Qwen may perform bounded read-only course, assessment, active-focus, and semantic
   searches. It returns a typed create, reinforce, resolve, or snooze action, or one
   clarification question.
3. A clarification and its verified partial facts are persisted against the Discord
   channel and user. The next authorized reply resumes that session without requiring
   another mention.
4. A completed create or reinforce action stores the combined reflection turns and
   embedding, and marks practice due for the next Toronto calendar day.
5. Morning planning loads active due focuses in its first facts snapshot and allocates a
   dedicated practice block before normal assessment allocation. An unschedulable focus
   is explicitly reported as deferred.
6. The next end-of-day run asks whether practice is still needed. “Yes” reinforces the
   focus and schedules another next-day practice block. “No,” “resolved,” or “clear”
   hard-deletes it.

## Missed check-in lifecycle

The default lifecycle is deterministic and configurable:

| Daily sweep | Focus state | Action |
| --- | --- | --- |
| Initial due review | Active | Ask whether practice is still needed |
| Miss 1 | Active | Send reminder 1; keep practice active |
| Miss 2 | Snoozed | Send reminder 2; stop scheduling practice |
| Misses 3–5 | Snoozed | Send one reminder per daily sweep |
| Sweep after reminder 5 | Deleted | Hard-delete focus, raw text, vector, and events |

A “yes” response at any point resets missed/reminder counters and returns the focus to
active daily practice. An explicit user snooze uses the chosen resume timestamp.

## Persistence

Migration `0011_academic_learning_focuses` enables pgvector and creates:

- `academic_discourse_sessions` and `academic_discourse_turns` for resumable,
  idempotent multi-turn handling;
- `academic_learning_focuses` for active/snoozed planning state;
- `academic_reflection_memories` for raw text and native pgvector values; and
- `academic_learning_focus_events` for lifecycle history until hard deletion.

`study_blocks` gains `learning_focus_id` and `block_kind`. The PostgreSQL service and
integration-test containers use the pinned `pgvector/pgvector:0.8.6-pg16-bookworm`
image.

## Configuration

- `ACADEMIC_MEMORY_ENABLED=true`
- `ACADEMIC_MEMORY_DEFAULT_PRACTICE_MINUTES=30`
- `ACADEMIC_MEMORY_SNOOZE_AFTER_MISSED_CHECKINS=2`
- `ACADEMIC_MEMORY_DELETE_AFTER_MISSED_CHECKINS=5`
- `EMBEDDING_MODEL=qwen3-embedding:0.6b`
- `EMBEDDING_MODEL_DIGEST=` (optional pin)
- `EMBEDDING_TIMEOUT_SECONDS=30`

Embedding failure is typed and does not discard the raw reflection or focus transition;
semantic retrieval only considers memories with valid vectors of the current query
dimension.
