# Academic semantic discourse memory

## Scope and decisions

- The feature is available only to the academic Discord agent. Finance and code-review
  workflows do not import or query its memory tables.
- Authorized users can ask to see memory with plain wording such as “see memory,”
  “show me your memory,” “what do you remember about my coursework?,” or “show my
  learning focuses.” The top-level Qwen decision semantically separates a learning-memory
  subrequest from any calendar operation; memory viewing cannot itself create a Notion proposal.
- An explicit academic struggle creates or reinforces a learning focus and schedules a
  separate practice block. It never relabels an assessment block as practice.
- Active learning focuses are verified struggle evidence for the morning academic
  briefing, but only bounded labels and focus IDs cross that model boundary; raw
  reflection text, embeddings, and private Discord message bodies stay out of the
  briefing context.
- Reflections are stored as bounded raw text together with a local Ollama embedding,
  model identity, vector dimensions, and non-secret embedding telemetry.
- Clearing a focus is a hard delete of the focus, its reflection text, its vectors, and
  its lifecycle events. Historical study blocks remain, but their deleted-focus foreign
  key is cleared and their `practice` kind is retained.
- The configurable default practice duration is 30 minutes.

## Runtime flow

1. An authorized Discord message first preserves exact confirm/reject as formal security
   hooks, then sends free-form prose to the top-level Qwen academic decision. Qwen owns
   create/update/archive/clarify interpretation and may return a distinct bounded
   learning-memory subrequest alongside calendar tools. The application layer does not
   route ordinary prose with a keyword classifier.
2. Qwen may perform bounded read-only course, assessment, active-focus, and semantic
   searches. It returns a typed create, reinforce, resolve, or snooze action, or one
   clarification question.
3. A clarification and its verified partial facts are persisted against the Discord
   channel and user. The next authorized reply resumes that session without requiring
   another mention.
4. A completed create or reinforce action stores the combined reflection turns and
   embedding, and marks practice due for the next Toronto calendar day.
5. Morning planning loads active due focuses in its first facts snapshot, allocates a
   dedicated practice block before normal assessment allocation, and may pass a bounded
   verified-context label to the conversational briefing. An unschedulable focus is
   explicitly reported as deferred.
6. The next end-of-day run asks whether practice is still needed. “Yes” reinforces the
   focus and schedules another next-day practice block. “No,” “resolved,” or “clear”
   hard-deletes it.

## See-memory review

The see-memory workflow reads only the requesting Discord owner’s active and snoozed
academic learning focuses. Deleted rows are absent by design, and unowned rows are
quarantined until an operator assigns ownership explicitly.

Qwen receives normalized, bounded facts only:

- an opaque focus id for host validation, never for Discord display;
- course code, topic, active/snoozed status, practice duration, next review time, and
  missed reminder count;
- a bounded current reflection summary when available; and
- a truncated flag when the host limited the focus set.

Embeddings, arbitrary database rows, finance memory, code-review state, raw Notion data,
and unrelated agent state are not prompt inputs. Qwen returns structured output with
`summary_text`, `covered_focus_ids`, and whether the memory set was truncated. The host
accepts only ids that were supplied in the bounded facts. If the structured output is
invalid or references an unknown id, Discord receives a deterministic grounded fallback
summary assembled from the verified focus facts. If there are no active or snoozed owned
focuses, the response is:

`I do not currently have any active or snoozed academic learning focuses stored for you.`

After a non-empty summary, the host opens a durable `memory_review` discourse session
scoped to the Discord user, Discord channel, included focus ids, each focus revision or
`updated_at` concurrency token, pending operation, candidate focus ids awaiting
clarification, expiration, and inbound Discord event ids for idempotency. The session
stores metadata and bounded labels only; it does not duplicate embeddings or complete raw
reflection text.

Replies resume an open `memory_review` session even without a bot mention. “never mind,”
“cancel,” “ignore that,” and equivalent unambiguous cancellation wording are checked
deterministically before Qwen is invoked. Cancellation closes the session, clears pending
state, does not mutate focuses or reflections, and returns:

`No problem—nothing was changed.`

## Delete and rewrite semantics

Explicit deletion with a subject, such as “I am no longer struggling with circuit
design,” is applied without another confirmation only when it uniquely matches a verified
focus from the active memory-review session or an owner-scoped semantic search. The host
locks the focus row, revalidates ownership and revision, hard-deletes the focus, deletes
its raw reflection rows, vectors, and lifecycle events, and clears historical
`study_blocks.learning_focus_id` while retaining `block_kind='practice'`. Database UUIDs
are never exposed in Discord responses.

Ambiguous deletion, such as “I am no longer struggling,” never mutates the database,
even if there is only one likely focus. The host asks a grounded clarification question
using verified focus data, persists the proposed operation, candidate ids, candidate
revisions, and the clarification question, and applies a later “yes” only after locking
and revalidating the same focus and revision. “No” or a corrected subject safely closes
or continues clarification without deletion.

Explicit correction, such as “It isn’t general circuit design; I’m struggling
specifically with nodal analysis,” is treated as replacement when it uniquely matches an
owned verified focus. The host locks and revalidates the focus, updates canonical
course/topic/assessment fields, deletes stale reflection-memory rows and stale vectors,
stores the corrected user text with a new embedding, increments the focus revision,
resets review/reminder counters, marks the focus active, and schedules a separate
practice block for the next Toronto calendar day. Only a non-text audit fact that a
rewrite occurred is retained outside semantic retrieval. If embedding generation fails,
the corrected canonical focus and raw text are retained with typed failure metadata, and
the stale vector remains deleted.

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

Migration `0012_academic_memory_review` adds explicit Discord ownership and concurrency
metadata to learning focuses:

- nullable `owner_user_id` and `owner_channel_id`;
- non-null `revision` with default `1`;
- owner/status/review indexes for scoped memory reads and edits; and
- an owner/session-kind/state/expiry index for durable `memory_review` session lookup.

The migration backfills focus ownership only when a focus points at one source
`academic_discourse_session` whose `discord_user_id` and `discord_channel_id` are both
present. Focuses without that exact owner evidence remain unowned and must not appear in
any user’s see-memory response or owner-scoped mutation path until resolved.

## Configuration

- `ACADEMIC_MEMORY_ENABLED=true`
- `ACADEMIC_MEMORY_DEFAULT_PRACTICE_MINUTES=30`
- `ACADEMIC_MEMORY_SNOOZE_AFTER_MISSED_CHECKINS=2`
- `ACADEMIC_MEMORY_DELETE_AFTER_MISSED_CHECKINS=5`
- `ACADEMIC_CONFIRMATION_TTL_HOURS=24` also bounds Discord memory-review session expiry
  unless a more specific review TTL is introduced.
- `ACADEMIC_END_OF_DAY_SCHEDULE=21:00` sets the Toronto daily review/practice cadence.
- `EMBEDDING_MODEL=qwen3-embedding:0.6b`
- `EMBEDDING_MODEL_DIGEST=` (optional pin)
- `EMBEDDING_TIMEOUT_SECONDS=30`

Embedding failure is typed and does not discard the raw reflection or focus transition;
semantic retrieval only considers memories with valid vectors of the current query
dimension.
