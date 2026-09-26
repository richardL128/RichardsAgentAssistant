# Implement semantic item queries and host-owned grounded completion

## Objective and user-visible outcome

Fix the Discord bot's recurring failure on owner-data questions such as:

> What are my to do's today

The model must remain responsible for interpreting the user's meaning. The host must
not map phrases or keywords directly to tools. Once the model has expressed a semantic
query, the host must validate it, execute it against trusted data, verify that the
evidence can support the requested answer, and complete the response without depending
on the model to reproduce canonical facts or perfectly obey a terminal tool protocol.

After this change, differently worded requests with the same meaning should produce the
same semantic query and grounded result. Nearby requests with different meanings, such
as a task list versus a schedule or full agenda, must remain distinguishable. A model
that emits malformed output must not be allowed to invent an answer, but a harmless
formatting failure after an unambiguous successful read should not discard valid results.

## Diagnosis

The active model-facing API currently overlaps source discovery and item retrieval:

- `search_courses` searches course/calendar sources, including the reserved `misc`
  calendar, but does not return dated task items or register an answerable grounding
  envelope.
- `search_assessments` searches actual items, applies temporal and completion filters,
  and registers trusted query evidence.
- The model can reasonably interpret `search_courses(query="today", roles=["misc"])`
  as the relevant tool for a to-do request even though that result cannot answer the
  question.
- Ordinary academic conversations additionally require the model to call
  `emit_conversation_response` after its data work. Valid plain-text answers are
  corrected once and then fail if the terminal tool is still absent.
- The nightly conversation already has a host-side post-tool lifecycle resolver, but
  the ordinary academic path does not use an equivalent mechanism.

The latest incident combined both weaknesses: the model selected a source-discovery
tool, wrote an unsupported empty-result conclusion, then failed the terminal lifecycle
protocol. Earlier failures show the same lifecycle fragility even when the model's
plain-text explanation was reasonable.

## Architectural decision

Use this responsibility boundary:

| Responsibility | Owner |
| --- | --- |
| Interpret the user's natural language and distinguish tasks, schedule, agenda, or another intent | Model |
| Express that meaning through a small semantic query contract | Model |
| Validate query fields, calculate local date windows, apply completion filters, and query stores | Host |
| Classify the capability of returned evidence | Host |
| Select a supported subset of returned items when needed | Model, through stable IDs |
| Validate selected IDs and render canonical titles, dates, status, pagination, and freshness | Host |
| Complete the conversational lifecycle from trusted read evidence | Host |
| Authorize or infer mutations | Never from prose; existing explicit proposal and confirmation flow only |

Deterministic code may operate on validated contracts, tool results, stable IDs, and
side-effect classifications. It must not route free-form user text with keyword,
regular-expression, or phrase tables.

## Requirements

1. Introduce one canonical model-facing read operation for dated calendar/task items,
   tentatively named `search_calendar_items`. Its typed semantic arguments should cover:
   - `view`: `tasks`, `schedule`, `agenda`, or `all_items`;
   - `temporal`: the existing typed temporal query (`today`, `tomorrow`, `this_week`,
     `upcoming`, `overdue`, `date_range`, or `all`);
   - `completion`: `incomplete`, `complete`, or `all`;
   - optional source areas such as courses, misc, and LEARN;
   - optional residual semantic search text, pagination, and bounded limits.
2. The new operation must search actual item rows and return stable item IDs plus a
   trusted query envelope. Date calculations and timezone localization remain host
   responsibilities.
3. Make this the sole model-facing path for answering dated academic/misc item lists.
   Remove the superseded `search_assessments` tool from the exposed runtime tool set
   rather than retaining two alternative item-query paths. Internal store helpers may
   be reused.
4. Narrow `search_courses` to source/entity selection only, such as resolving an opaque
   course ID before a proposal. Its name, description, result contract, and validation
   must make clear that it cannot answer task, due-item, schedule, or agenda questions.
5. Add an explicit evidence capability to trusted query envelopes, for example
   `result_kind=calendar_items`, `course_sources`, `jobs`, or `learn_content`. A final
   item-list answer must be grounded in a current answer-capable envelope of the correct
   kind.
6. Never allow `course_sources` evidence by itself to support a conclusion that no tasks
   or events exist. Failed and stale queries must likewise not count as current answer
   evidence.
7. For grounded item lists, validate that every selected `item_id` belongs to the
   referenced query envelope. Render factual titles, dates, status, source area,
   pagination, and freshness notices from canonical host data rather than model prose.
8. Generalize the existing post-tool lifecycle resolver and wire it into ordinary
   academic conversations. A successful, unambiguous, read-only item query should be
   completable by the host even if the model emits plain text or omits
   `emit_conversation_response`.
9. Keep `emit_conversation_response` as a supported model protocol if useful, but do not
   make a redundant terminal call the only way to preserve an otherwise valid read.
10. Preserve strict behavior for mutations. Plain text, malformed terminal output, or a
    host-inferred intent must never create, update, move, archive, confirm, or reject an
    item. Existing proposal and exact-confirmation boundaries remain authoritative.
11. Preserve acknowledgement and truthful progress messages during multi-step work.
    Progress wording may be driven by a validated semantic query, not by host keyword
    matching over the original message.
12. Degrade at the smallest safe unit. One invalid item selection should not erase
    independently valid selected items, but uncertainty must be surfaced and the host
    must never silently substitute a different item.
13. Preserve authorization, idempotency, checkpoint durability, local-time semantics,
    freshness behavior, pagination, and output limits unless this plan explicitly
    changes them.
14. Preserve unrelated dirty-worktree changes and integrate with the current source
    state rather than assuming the repository matches an older plan or commit.

## Malformed and non-schema output policy

Do not parse arbitrary model prose to recover tool arguments, dates, IDs, or intent.
Use the following bounded behavior:

1. On a malformed semantic query or terminal object, make at most one schema-repair
   request using the concrete validation diagnostic. Reuse the repository's structured
   invocation/repair facilities where practical.
2. If exactly one current successful, read-only, answer-capable envelope exists and the
   requested output is mechanically unambiguous, the host may ignore malformed prose,
   synthesize grounded completion, and render the trusted results.
3. If item selection is required, only accept stable IDs that validate against the
   envelope. Do not extract selections from prose.
4. If several envelopes or possible views make the answer ambiguous, return one concise
   clarification question instead of guessing.
5. If no answer-capable evidence exists, a plain-text factual claim is unsupported.
   Correct the model or return an actionable retrieval failure; never publish the claim
   as a grounded answer.
6. If any write, proposal, confirmation, or other side effect is pending, fail closed
   after the bounded repair. Do not synthesize a mutation lifecycle from plain text.

This recovery is deterministic only over trusted host state. It is not a second
natural-language router or a legacy execution path.

## Semantic view behavior

- `tasks` means unfinished actionable items according to persisted item/event type and
  completion status. It excludes pure class/tutorial schedule entries. Do not infer
  task type from title keywords such as `meeting`, `homework`, or `lecture`.
- `schedule` means scheduled classes, tutorials, appointments, and events for the
  requested period.
- `agenda` means the combined relevant tasks and scheduled events for the period.
- `all_items` is an explicit broad retrieval view, not the silent fallback for an
  unrecognized request.
- If the model cannot distinguish the intended view with adequate confidence, it should
  ask a concise clarification question.

If current persisted types cannot express these distinctions reliably, stop and revise
the item taxonomy or this plan before adding title-based heuristics.

## Explicit non-goals

- Do not create a phrase-to-tool routing table for `todo`, `today`, `due`, or any other
  wording.
- Do not add regular expressions or keyword scoring to classify the user's intent.
- Do not require exact final prose, a fixed tool-call sequence, or a particular
  paraphrase from the model.
- Do not trust model-written titles, dates, IDs, counts, or empty-result claims when
  canonical host evidence is available.
- Do not preserve the old exposed item-query tool as a fallback or alternate runtime
  architecture.
- Do not redesign the Notion databases, Discord ingress, authorization model, proposal
  confirmation flow, or unrelated agents.
- Do not treat this change as a general latency optimization project, although it must
  avoid unnecessary repeated model or synchronization calls introduced by the new
  flow.

## Relevant repository context

- `app/agents/academic_planner/contracts.py` contains the current distinct course and
  assessment query contracts.
- `app/agents/academic_planner/discord_harness.py` defines the active native tools,
  handlers, trusted query envelopes, grounding validation, progress mapping, rendering,
  and ordinary/nightly model loops.
- `_search_assessments` currently normalizes temporal scope and records grounding
  envelopes; `_search_courses` returns source options but does not record answerable
  item evidence.
- `_GROUNDED_QUERY_TOOLS` currently recognizes item-query tools but excludes
  `search_courses`.
- The ordinary native tool loop does not currently wire a post-tool lifecycle resolver,
  while the nightly path does.
- `app/agents/harness.py` contains strict terminal lifecycle validation, one correction
  attempt, and the newer post-tool lifecycle resolver hook.
- `app/llm/gateway.py` provides native tool binding and a structured-output path with
  schema validation and bounded repair.
- `app/agents/query_contracts.py` contains reusable temporal scope and window contracts.
- `app/db/academic.py` contains the actual item searches and should remain authoritative
  for temporal/completion filtering.
- Existing unit tests primarily script model messages. They validate host mechanics but
  do not by themselves prove that the configured model generalizes across natural
  language requests.

## Expected files and components to change

- `app/agents/academic_planner/contracts.py`
  - add the unified semantic item query and result/evidence capability contracts;
  - retire model-facing assessment-query contracts if they are no longer used elsewhere.
- `app/agents/query_contracts.py`
  - reuse or minimally extend shared temporal/completion query types without duplicating
    date normalization logic.
- `app/agents/academic_planner/discord_harness.py`
  - replace the exposed item-read tool, narrow source discovery, record typed evidence,
    validate answer capabilities and item IDs, render canonical results, and wire the
    general lifecycle resolver.
- `app/agents/harness.py`
  - generalize resolver invocation so safe read recovery can occur after successful
    tools and after a no-tool/plain-text terminal response before correction/failure.
- `app/db/academic.py`
  - expose or adapt a bounded unified item query over existing stores if the harness
    cannot compose current queries without duplicating filtering semantics.
- `app/llm/gateway.py`
  - change only if the existing structured repair boundary cannot be reused as-is.
- `tests/unit/test_agent_harness.py`
  - cover general lifecycle recovery and fail-closed side-effect cases.
- `tests/unit/test_academic_native_discord_harness.py`
  - cover semantic item querying, evidence capabilities, host rendering, malformed
    outputs, and the original incident.
- Relevant architecture and operations documentation describing the active tool and
  lifecycle boundary. Update only documents that describe current runtime behavior.

The executing agent must inspect the current dirty worktree first. Several expected
files already contain in-progress lifecycle changes; patch them narrowly and do not
overwrite or duplicate that work.

## Ordered implementation steps

1. Inspect the current repository state, dirty diffs, active runtime wiring, contracts,
   query envelopes, renderers, lifecycle hooks, and focused tests. Confirm which current
   edits belong to the in-progress nightly lifecycle work.
2. Define the unified `search_calendar_items` argument/result contract and evidence
   capability enum. Reuse shared temporal contracts and existing opaque stable IDs.
3. Implement or adapt the host query path so all task, schedule, and agenda reads return
   canonical items through one model-facing operation. Keep temporal windows,
   completion filtering, timezone handling, source-area filtering, ordering, limits,
   freshness, and pagination host-side.
4. Replace the exposed `search_assessments` runtime tool with the new canonical tool.
   Narrow `search_courses` to source selection and ensure its result contract is not
   answer-capable for item-list questions. Do not keep the superseded runtime tool as a
   fallback.
5. Record `result_kind` and other required answer capabilities in every trusted query
   envelope. Update grounding validation so an answer must cite evidence capable of
   supporting its response type.
6. Update item selection and rendering so the model supplies only validated stable IDs
   or an explicit all-results choice. Render factual list content from host records and
   preserve item-level valid results when another selected item is invalid.
7. Generalize the post-tool lifecycle resolver. Invoke it after successful tools and on
   plain-text/no-terminal responses before the normal correction path. Apply the
   malformed-output policy above, with distinct read-only and side-effect behavior.
8. Update model policies and tool descriptions around semantic views, evidence use, and
   clarification. Do not add phrase examples as deterministic routing rules; examples
   may teach intent distinctions but cannot be consumed by host routing code.
9. Update progress mapping so validated task, schedule, and agenda queries produce
   specific truthful acknowledgement/progress without inspecting keywords in raw user
   text.
10. Add focused contract, store, harness, checkpoint/resume, and rendering tests,
    including exact incident replay, paraphrases, contrast requests, and malformed
    outputs.
11. Update current-behavior documentation and remove references to the superseded
    exposed item-query path. Preserve immutable historical records without leaving the
    old architecture executable.
12. Run formatting, static analysis, focused tests, repository-level tests appropriate
    to the changed surface, and then the real configured model/Discord acceptance checks
    when the environment is available.

## Migration and compatibility considerations

- Make the unified item-query architecture the sole configured default. Do not retain
  the old model-facing item tool as a rollback or fallback path.
- Existing store methods may remain as internal implementation helpers if they are not
  independently exposed to the model and do not create competing semantics.
- Inspect persisted native-conversation checkpoints for old tool names or pending tool
  calls. Either migrate their non-side-effect state explicitly or expire/restart those
  conversations with a clear user-visible message. Do not execute an obsolete pending
  tool call through a hidden legacy handler.
- Preserve immutable audit/history rows and prior transcripts. Historical references to
  the old tool do not require keeping it executable.
- Preserve existing proposal payloads and confirmation semantics unless current code
  evidence proves a migration is necessary.
- If query-envelope persistence changes, version it and provide a bounded migration or
  safe invalidation path. Never reinterpret an old source-selection envelope as
  answer-capable item evidence.

## Validation strategy

### Exact incident regression

Create a fixture with a fixed owner timezone and date, plus both scheduled classes and
unfinished task-like items on that date. Send the literal message:

> What are my to do's today

Assert semantic outcomes rather than exact prose or an incidental internal call order:

- the normalized view is `tasks`, temporal scope is `today`, and completion is
  `incomplete`;
- the answer is grounded in a current `calendar_items` envelope;
- `course_sources` evidence cannot support the answer;
- every rendered item ID belongs to the envelope;
- the bot cannot report an empty list while matching trusted items exist;
- class/tutorial-only schedule entries are excluded according to persisted type;
- the response reaches a completed lifecycle state.

This test is an incident replay, not a production phrase rule.

### Semantic paraphrase and contrast coverage

Exercise multiple ways of expressing the same task intent, for example:

- `What do I need to get done today?`
- `Show me today's tasks.`
- `Anything on my plate today?`
- `Do I have anything due today?`
- `Today's to-dos?`

Assert the same normalized semantic request and evidence class, allowing different tool
call formatting and final prose.

Also exercise nearby but different meanings:

- `What's my schedule today?` must use the `schedule` view;
- `Give me my full agenda today` must combine tasks and scheduled events;
- `Which courses am I taking?` is source/entity discovery and must not be presented as
  an item query.

Do not encode these sentences into application routing code. Deterministic unit tests
should validate host behavior for supplied semantic contracts. A separate configured-
model evaluation or live acceptance test should measure whether the model generalizes
from natural language to those contracts.

### Malformed-output and lifecycle coverage

- Valid item query followed by correct terminal schema completes normally.
- Valid item query followed by plain text/no terminal tool is completed from the single
  trusted envelope without another factual model answer.
- Source discovery followed by `nothing due` is rejected as unsupported.
- Invalid or unknown query fields receive one bounded schema repair.
- Invalid item IDs are rejected individually while valid independent IDs remain usable
  and uncertainty is surfaced.
- Multiple answer-capable envelopes with ambiguous selection cause clarification rather
  than silent synthesis.
- Failed or stale queries do not become answer evidence.
- Plain text after a proposal/write remains rejected and performs no side effect.
- Checkpointed read recovery is idempotent and does not rerun tools or send duplicate
  final messages.

### Repository and live validation

1. Run formatter/linter and type checking for changed production files.
2. Run focused harness, academic Discord, query contract, store, and lifecycle tests.
3. Run the broader unit/integration suite appropriate to the repository.
4. With the configured local model available, run the exact incident message,
   paraphrases, contrast requests, and deliberately malformed response simulations.
5. With the real Discord environment available, verify the Heard, Working, and
   Completed/Failed lifecycle, canonical grounded content, retry idempotency, and absence
   of duplicate replies.
6. If a live environment is unavailable, report that limitation explicitly; scripted
   gateway tests alone are not evidence of deployment-level semantic behavior.

## Acceptance criteria

- No application code maps raw phrases, keywords, or regular expressions directly to a
  specific tool or semantic view.
- The configured model can express task, schedule, agenda, and source-discovery intent
  through distinct typed semantics.
- One canonical model-facing item query is the sole runtime path for dated academic and
  misc item-list answers.
- The literal incident prompt returns a grounded, non-empty task result whenever the
  trusted fixture contains matching tasks, and never treats a course source list as an
  empty task list.
- Same-intent paraphrases normalize to the same semantic view in configured-model
  evaluation, while schedule, agenda, and course-source contrast prompts remain
  distinct.
- Canonical host data, not model prose, determines rendered item titles, dates, status,
  counts, pagination, and freshness warnings.
- A harmless terminal-format failure after one unambiguous read does not discard useful
  evidence or show a generic lifecycle error.
- Ambiguous reads request clarification, and unsupported factual claims are not sent.
- All mutation and confirmation flows remain fail-closed, explicit, authorized, and
  idempotent.
- Existing valid items survive item-level degradation when another independent item is
  malformed or invalid.
- Focused and repository-level validation pass, and live/configured-model validation is
  reported accurately.

## Known risks and unresolved implementation decisions

- Model semantic accuracy cannot be proven by scripted tool-call tests. Maintain a
  configured-model evaluation set and validate the real Discord boundary before making
  a deployment-level claim.
- The precise internal query composition may span academic, misc, and LEARN stores.
  The executing agent must choose the smallest cohesive adapter after inspecting current
  store APIs; it must not expose multiple competing model-facing item tools.
- Existing in-progress lifecycle edits overlap expected files. Integration must reuse
  them where correct and avoid reverting unrelated work.
- Stored item taxonomy may not yet distinguish every task from every scheduled event.
  Persisted explicit type is authoritative; if it is insufficient, revise the taxonomy
  deliberately rather than introducing title-based heuristics.
- Host-rendering all items is safe only when the semantic query clearly requests all
  matching results. Queries that require ranking, comparison, or selection may need a
  second validated ID-selection step.
- Removing a model-facing tool can invalidate open checkpoints. The implementation must
  audit and handle those checkpoints before rollout.

## Fresh-session execution instruction

After this plan is reviewed and explicitly approved, start a fresh Codex session with:

`Read prompts/implement-semantic-item-query-grounding.md and execute it step by step. Validate the completed feature according to the plan and repository instructions.`

The fresh session must inspect the current repository state before editing. This plan
is authoritative for the agreed design, but it does not supersede current repository
evidence or more specific `AGENTS.md` instructions.
