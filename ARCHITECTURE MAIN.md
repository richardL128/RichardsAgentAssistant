# Architecture Main

## Shared design principles

All three agents follow the same pattern:

```text
Narrowly scoped connectors → fact extraction → local Qwen reasoning
→ proposed output/action → user approval → audit log
```

The local Qwen model performs reading, reasoning, prioritization, and explanation. Deterministic tools collect facts, run tests/scanners, and perform approved writes. Credentials remain outside the model context: the orchestration layer exposes narrow tools such as “fetch today's commits” or “read an assessment page,” rather than unrestricted account access.

The system should use Toronto local time unless explicitly configured otherwise.

---

## 1. Daily cross-repo code review agent

### Goal

Review every push made that day across all repositories, then send one consolidated, evidence-based report.

```text
GitHub push history / webhooks
          ↓
Per-repo checkout at exact commit SHA
          ↓
Diff + tests + security scanners + dependency checks
          ↓
Qwen: focused reviews and finding triage
          ↓
Daily report
```

### Inputs

- GitHub account activity or push webhooks, including repositories not cloned locally.
- Each commit’s before/after SHA, diff, changed dependency/configuration files, and PR description when available.
- Repository-specific instructions: build/test commands, languages, framework, and ownership files.
- Tool output from tests, lint/type checks, secret scanning, dependency audits, Semgrep/static-analysis findings, and language-specific scanners.

- On the code's first run, it should do a "large batch" ingestion. It should ingest each project in the user's GitHub account one by one, perform a larger "code review" just to get an idea of the each repo's content and what it performs. It should store this context in a SKILLS.md. So when a particular project is being analyzed, a SKILLS.md which matches that project should be ingested. 

### Review flow

1. Discover all pushes made since midnight Toronto time and deduplicate commits already reviewed.
2. Classify risk before deep reading:
   - High-risk: auth, payments, database migrations, permissions, infrastructure, dependency changes.
   - Low-risk: documentation, formatting, generated files.
3. Build a compact review packet: changed code plus relevant surrounding code, tests, and configuration.
4. Run focused reviews for:
   - correctness and regressions;
   - security and data flow;
   - missing tests and maintainability.
5. Merge duplicate findings and discard vague speculation.
6. Deliver a daily report with actionable findings only. It should be presented in a way where a frontier model running on a harness (Claude Code, Codex) would be able to read the findings, and immediately **propose** a fix. 

### Finding standard

Each finding includes:

- Severity: block, important, or suggestion.
- Exact file and line.
- What can go wrong and why the changed code creates the risk.
- A minimal reproduction or missing test.
- Confidence and stated assumptions.

The agent must not create issues or commit fixes. Its normal delivery channels are inline GitHub review comments for high-confidence findings and a concise Discord summary; every published comment is retained in the audit log. It may prepare GitHub issue drafts or TODOs for review.

### Persistent memory and schedule

A local database tracks reviewed SHAs, dismissed findings, project conventions, and recurring false positives. Run a consolidated review around 6–7 p.m., with an optional later catch-up run. A webhook-triggered quick scan can immediately flag high-risk changes.

---

## 2. Investment news and thesis-monitoring agent

### Goal

Produce a daily, source-cited briefing on portfolio/watchlist exposure in tech, oil/energy, defence, and ETFs. The agent surfaces information and challenges theses; it does not provide autonomous trade instructions.

```text
Eight individually scoped trusted-source fetches
                    ↓
Validate, timestamp, and deduplicate source material
                    ↓
Match events → holdings / watchlist / ETF exposure / thesis
                    ↓
Qwen: mechanism, counter-case, and materiality reasoning
                    ↓
Dated daily briefing + thesis journal updates
```

### Strict trusted-source retrieval policy

The agent must **not** conduct open-ended web searches or mix untrusted search results into its analysis.

For each briefing, the orchestration layer makes a fixed number of narrow tool calls—initially **eight**, adjustable later. Each call retrieves only from one named, approved source. Qwen receives only the normalized results of those eight calls plus the user’s portfolio/thesis data.

Example initial allowlist categories:

1. SEC EDGAR filings.
2. Company investor-relations releases and earnings materials.
3. U.S. Energy Information Administration releases/data.
4. U.S. Department of Defense contract announcements or other approved government procurement source.
5. Reuters, if approved.
6. Financial Times, if approved and subscription/API access permits.
7. Wall Street Journal, if approved and subscription/API access permits.
8. Bloomberg, if approved and subscription/API access permits.

The final set should be explicit, versioned, and user-approved. A source tool is allowed to return no relevant items; the system must not substitute another site to fill the gap. It should retain source URL, publication time, title, author/outlet, and any licensing limitations. The briefing cites the original source links rather than reproducing full articles.

### Inputs

- A manually maintained database with ticker, portfolio percentage, sector/theme, personal thesis, and “what would change my mind.”
- The approved eight-source allowlist described above.
- Market and ETF facts from separately approved data sources, such as prices, issuer-published ETF holdings, and benchmarks.

### Daily flow

1. Run the eight source-specific tool calls for material published that morning as well as material published since the market closed last night(Market Open) -> This flow should run as soon as the market opens (9:00 am EST)
2. Extract factual claims, publication time, involved companies/sectors/countries, and whether the information is primary or reported.
3. Deduplicate the same event reported by multiple approved outlets.
4. Map events against owned holdings, watchlist entries, ETF constituents/weights, and themes such as AI infrastructure, semiconductors, crude supply, and defence procurement.
5. Ask Qwen structured questions:
   - What changed today?
   - Which holdings or ETFs have plausible exposure?
   - Through what mechanism could it matter?
   - Is the likely impact immediate, medium-term, or merely narrative?
   - What would disconfirm the concern?
   - Is this new information or a restatement of an existing story?
   - What are potential stocks/funds that are worth investing in based on any global events. 

### Output: information and trade suggestions, not trade instruction

Rank a maximum of 5–10 events. Each event card includes:

```text
Event:
Exposure: ticker / ETF / sector, including direct or indirect exposure
Why it may matter:
Time horizon: days / quarters
Verified facts:
Uncertainties:
Counter-case:
Source links:
Portfolio/thesis impact: monitor / revisit thesis / no action
```

### Reusable evidence-first event-card template

Use the following template for every event. It is deliberately sector-neutral: it applies to technology, oil and gas, and defence/military holdings. Write for a 19-year-old investor with some finance knowledge. On first use, define any potentially unfamiliar term in plain English. Keep verified facts separate from analysis and forecasts.

```md
## [Short, factual event title] — [publication date, time zone]

**Event**

[One or two sentences saying exactly what happened, who announced or reported it, and when. Do not describe an estimate, rumour, or management forecast as an established fact.]

**Exposure**

- **Direct:** [ticker(s) owned or watched] — [one-sentence connection to the event].
- **Indirect:** [ETF(s), supplier/customer, commodity, or peer] — [how the connection works].
- **Sector/theme:** [for example: AI infrastructure, crude oil, defence procurement].
- **Portfolio context:** [portfolio weight, if available; otherwise “weight not provided”].

**Why it may matter**

[Explain the economic mechanism in 2–4 short sentences. State whether it could change revenue, costs, demand, supply, margins, regulation, contract backlog, or valuation. Define terms on first use: for example, “backlog (contracted work a company has not yet completed).”]

**Key figures**

| Figure | What it means in plain English | Period / as-of date | Source |
|---|---|---|---|
| [exact value and unit] | [plain-English interpretation] | [quarter/date] | [source name + link] |
| [exact value and unit] | [plain-English interpretation] | [quarter/date] | [source name + link] |
| [derived value, if useful] | [show the calculation] | [same period] | Agent calculation using [linked source(s)] |

Rules: Every number needs a unit, a time period or “as of” date, and a linked source. Label agent calculations explicitly and show the formula. Label company guidance, analyst estimates, and forecasts as estimates—never as results.

**Time horizon**

- **Days to weeks:** [possible near-term market reaction and why].
- **Quarters:** [what must happen in operating results for the thesis to be supported or challenged].

**Verified facts**

- [Atomic, source-backed fact with an inline link.]
- [Atomic, source-backed fact with an inline link.]
- [If sources conflict, state the disagreement instead of selecting a version without explanation.]

**Uncertainties**

- [What is unknown, unconfirmed, estimated, or dependent on a future decision?]
- [What data point, filing, earnings report, or government announcement would clarify it?]

**Counter-case**

[Give the strongest reasonable explanation for why the event may not help or hurt the holding. Include the condition that would make this counter-case more likely.]

**What to watch next**

- [Specific future date, release, contract award, inventory report, earnings report, or price/volume data point.]
- [A measurable threshold or development that would cause a thesis review.]

**Source links**

- [Primary source: company filing, company investor-relations release, government agency, or ETF issuer]
- [Approved reporting source, if used]

**Portfolio/thesis impact: `[monitor | revisit thesis | no action]`**

[One-sentence rationale. This is research context, not a trade instruction.]
```

Sector-specific evidence to prioritize:

- **Technology:** earnings revenue by segment, cloud/AI capital spending, product shipment data, gross margin, customer concentration, and official regulatory filings.
- **Oil and gas:** EIA production, inventory, and demand data; OPEC+ decisions; company production volumes; realized prices; refining margins; and geopolitical supply-disruption evidence.
- **Defence/military:** official contract awards, appropriations/budget documents, backlog, programme milestones, delivery schedules, export approvals, and defence-company filings. Do not treat headlines about conflicts as proof that a specific company will receive revenue.

The agent must not connect to a brokerage, place trades, or use unqualified “buy”/“sell” directives. A thesis-change log records whether an event invalidates, strengthens, or does not affect the user’s existing thesis.

---

## 3. University personal-assistant and academic planner

### Goal

Use Notion as the source of truth for assignments, tests, quizzes, events, rubrics, instructions, and course outlines. Convert those inputs into a realistic day-by-day work and study plan, then run an end-of-day progress check-in through Discord.

```text
Notion assessment calendar + attached PDFs/pages
                    ↓
Extract deadlines, weights, rubrics, and test scope
                    ↓
Confirm ambiguous course-outline facts once
                    ↓
Deterministic scheduler + Qwen planner/critic
                    ↓
Daily plan, study blocks, carry-forward queue, Discord check-in
```

### Suggested Notion structure

Use one top-level Courses database. Each course row/page owns one seeded inline
Assessments database; its calendar is only a view over those assessment pages.

| Source | Essential fields |
| --- | --- |
| Courses | Course Code title, priority ranking, course-outline PDF/page, term, assessment policy |
| Per-course Assessments | Name title, Date, optional weight, instructions, rubric, scope, status, estimated time |
| PostgreSQL study/work blocks | Linked assessment, planned duration, actual duration, completion state, notes |

An assessment can be a deterministically confirmed quiz or assignment. Unknown
labels wait for authorized Discord clarification. The Notion calendar displays
the underlying assessment pages; LifeAgent stores generated study blocks in
PostgreSQL rather than requiring a second Notion database ID.

### Ingestion flow

1. Sync changed Notion pages and attachments.
2. Extract assignment rubrics/instructions and course-outline details into structured fields.
3. Preserve citations, for example: “worth 15%, course outline page 3.”
4. Flag uncertain extraction for one-time confirmation rather than treating ambiguous PDFs as truth.

### Assignment prioritization

For each assignment, Qwen produces a work breakdown:

- Deliverables and grading criteria.
- Likely research, build, write, test, and submission steps.
- Dependencies.
- Estimated effort range.
- Risk of leaving work late.
- Remaining available work sessions before the deadline.

The priority is explainable:

```text
assignment priority =
deadline pressure
+ grade weight
+ effort remaining ÷ available time
+ dependency/risk
+ course priority
```

Time estimates improve from actual time logged against previous assignment types and courses.

### Test and quiz prioritization

The course outline plus the Notion test scope provides the ground truth.

```text
test-study priority =
deadline pressure
+ test/quiz weight
+ course-priority ranking
+ scope size
+ current confidence gap
```

A low-weight quiz tomorrow covering two topics receives a short review block. A heavily weighted midterm in a high-priority course with broad scope receives spaced blocks across multiple days. Qwen can create topic-level study tasks but must not claim mastery because it generated study materials.

### Scheduling and carry-forward rules

- Fixed classes, events, sleep, commute, and personal commitments are non-negotiable.
- Reserve buffer time; a 100% full calendar is not realistic.
- Plan the next 7–14 days in detail.
- Give high-priority tasks protected deep-work blocks with realistic durations.
- Never move a test, deadline, or required event.
- Automatically roll incomplete work blocks forward, label them as carried over, and re-evaluate their priority.
- Ask before changing a deadline or creating a major new calendar commitment.

### Daily interaction and end-of-day Discord check-in

Each morning, the agent sends or presents: “Here are the three most valuable blocks today, why they matter, and what is safe to defer.”

At the user’s configurable end-of-day time, the agent sends a Discord direct message or private designated channel message asking:

1. What progress was made on each of today’s planned to-dos?
2. How did each test or quiz taken today go?
3. Are there any new tasks, deadlines, tests, or events that should be added to Notion?

It should accept short natural-language replies, extract proposed Notion updates, and show a concise confirmation before creating or modifying entries. It should update time estimates and the next-day plan only from confirmed progress; it must not infer that planned work was completed merely because it appeared on the calendar.

The assistant is a planner and tutor, not a mechanism for completing graded work dishonestly.

---

## 4. Minimal operations UI

The frontend's complete architecture and implementation guidance lives in [frontend/AGENTS.md](frontend/AGENTS.md). The UI is a read-only health and audit console for the three agents; Discord and GitHub remain the places where work and communication occur.

---

## Recommended implementation order

1. Build the daily cross-repo code reviewer first: inputs are clear and quality is easiest to measure.
2. Add the personal academic planner and Discord end-of-day check-in.
3. Add the finance briefing after selecting and authorizing the exact eight-source allowlist and any subscriptions/data access needed.
