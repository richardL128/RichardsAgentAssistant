# Third-party integration runbook

## Purpose and safety boundary

This document explains how LifeAgent connects to Notion, Discord, and the eight approved finance-source categories in the architecture. It is a setup and implementation runbook, not a request to turn every integration on immediately.

The connector—not Qwen—makes every third-party request. Qwen receives a small, normalized packet of facts and source links only. It never receives an API key, a browser session, a website password, unrestricted internet access, or a generic HTTP tool.

Start every connector in **read-only / dry-run** mode. A connector may write only after its specific approval rule is implemented and tested:

- Notion writes require the student's explicit confirmation of the proposed change.
- Discord writes are the approved briefings, plans, check-ins, alerts, and confirmation prompts.
- Finance sources are read-only forever.

## Shared connector contract

Each provider has one typed Python adapter under `app/connectors/`. An adapter is the only code allowed to know the provider's base URL, credentials, and request format.

```text
scheduled or inbound event
  -> connector adapter (allowlisted operation only)
  -> Pydantic-normalized record + source metadata
  -> validation/redaction/deduplication
  -> agent workflow
  -> proposed or approved delivery/write
  -> audit record and delivery receipt
```

Every adapter must implement these behaviours:

| Requirement | Implementation rule |
| --- | --- |
| Least privilege | Request only the scopes/permissions listed in this document. Never use an owner/admin credential where a bot/integration credential works. |
| Secrets | Read secrets from local environment/Docker secrets; never commit them, include them in prompts, log them, or return them from the API. |
| Timeouts/retries | Use finite connect/read timeouts. Retry only network/429/5xx failures with capped exponential backoff; do not retry 401/403 until credentials or permissions change. |
| Idempotency | Persist the inbound event ID or outbound delivery intent before side effects. Retrying must not duplicate a Notion page update or Discord message. |
| Auditability | Record provider, operation, target ID (redacted where appropriate), request ID, status, timestamp, and a redacted error diagnostic. |
| Content handling | Store only the minimum source material permitted by the source's licence. Preserve the original URL, publication time, and retrieval time. |

Use a separate local configuration entry for each connector. `.env.example` has names only; real values live in the host keychain/Docker secrets or a never-committed local `secrets.env`.

```dotenv
NOTION_TOKEN=
NOTION_COURSES_DATABASE_ID=

DISCORD_BOT_TOKEN=
DISCORD_ACADEMIC_AUTHORIZED_USER_IDS=[]
DISCORD_ACADEMIC_GATEWAY_ENABLED=false
DISCORD_ACADEMIC_MESSAGE_CONTENT_ENABLED=false
DISCORD_ACADEMIC_CHANNEL_ID=
DISCORD_FINANCE_CHANNEL_ID=
DISCORD_CODE_REVIEW_CHANNEL_ID=

SEC_USER_AGENT=LifeAgent/0.1 contact@example.com
EIA_API_KEY=
SAM_GOV_API_KEY=
REUTERS_CREDENTIALS=
FT_API_KEY=
WSJ_OR_DOW_JONES_CREDENTIALS=
BLOOMBERG_CREDENTIALS=
```

The finance credential names are intentionally placeholders. They become real only after the exact source contract and approved API method are known.

---

## 1. Notion: academic source of truth

### Choose the connection type

Use a **Notion internal connection** for this personal, single-workspace system. It provides one static integration token and avoids building a multi-user OAuth flow. A public OAuth connection is appropriate only if LifeAgent will be installed by other people or in many workspaces.

### User setup in Notion

1. In the [Notion integrations dashboard](https://www.notion.so/profile/integrations), create an internal connection named `LifeAgent`.
2. Enable only the capabilities needed by the planner:
   - Read content.
   - Read user information only if it is actually required for an audit display.
   - Update content, because confirmed planner updates create/edit Notion entries. Do not use the update capability until the confirmation workflow is shipped.
3. Copy the internal integration token into the local secret store as `NOTION_TOKEN`.
4. Create one top-level Courses database. Each course row/page must contain one inline Assessments database created from the reviewed New Course template.
5. On the top-level Courses database, open **Share** → **Add connections** and select `LifeAgent`. Creating a token alone does not grant access to arbitrary workspace pages.
6. Copy only the top-level database ID into `NOTION_COURSES_DATABASE_ID`. LifeAgent discovers each underlying data-source ID and child Assessments database; do not put those discovered IDs in `.env`.
7. Decide where uncertain facts and confirmations appear: use the private Discord channel by default. Do not create a hidden Notion “automation” database unless it is a deliberate, documented part of the student's workflow.

### Required database mapping

LifeAgent discovers property IDs from each physical data-source schema and
requires the expected name and type before enabling that course.

| LifeAgent concept | Notion database / expected data |
| --- | --- |
| Course | Courses: `Course Code` title; optional term and priority |
| Assessment | Per-course inline Assessments: `Name` title, `Date` date; optional weight, status, and estimated minutes |
| Work block | PostgreSQL planner state; no separately configured Notion database ID |

Keep the Notion page URL and page ID for every imported object. Download an attached PDF during sync to the artifact store because Notion-hosted file URLs can expire; record the original attachment metadata and retrieval timestamp.

### How the adapter works

**Read path**

1. Retrieve the configured Courses database, discover its data source, and query course rows.
2. Paginate each course page's children, discover exactly one seeded Assessments database and its physical data source, then query its event pages.
3. Convert properties and blocks to a Pydantic `NotionAssessment`/`NotionCourse` record.
4. Extract PDF/page text, retain page/block citations, and compare its source-version hash with the stored version.
5. Upsert normalized records. Flag conflicting or ambiguous fields rather than guessing.

Use Notion webhooks if the connection/workspace configuration supports the required events. Keep a scheduled delta-sync fallback because it is easier to reason about, recovers from missed events, and is needed for attachment refreshes. Incoming webhooks must be signature-verified and deduplicated before they enqueue a sync.

**Write path**

```text
Discord reply / planner proposal
  -> proposed Notion patch shown to student
  -> explicit confirmation tied to proposal ID
  -> update only the specified page/properties
  -> re-read changed page and record audit event
```

Never let an LLM send raw Notion patch JSON. The proposal contains a typed, whitelisted field change; deterministic code validates the page belongs to one of the configured databases and applies only the confirmed fields.

### Notion connection tests

- The health check validates the token, one configured Courses database ID, discovered child sources, required property IDs/types, and page sharing without displaying secret values.
- A fixture Course, Assessment, and Study block are imported with correct field mapping and canonical page URLs.
- A PDF attachment produces text with page citations; a non-text PDF becomes an explicit OCR/confirmation case rather than fabricated text.
- A proposed deadline update makes no Notion change before confirmation; after confirmation it changes exactly one target property and writes an audit event.
- Revoking the connection or unsharing a database produces an actionable `notion_unauthorized` health failure.

Official reference: [Notion authorization](https://developers.notion.com/guides/get-started/authorization) distinguishes internal static-token connections from OAuth; [public connection documentation](https://developers.notion.com/guides/get-started/public-connections) explains page-level access selection.

---

## 2. Discord: delivery, approval, and private check-ins

### Chosen integration pattern

Create one Discord application with one bot user. The three agents share it, but each uses a configured private channel/thread and clear message prefix. This avoids three tokens, three duplicate permission models, and confusing ownership.

Use the Discord **Gateway** for incoming private replies and the normal REST API for outbound messages. Gateway is a persistent connection from the local worker to Discord, so the Mac does not need to expose a public URL. This is preferable to an HTTP interaction endpoint for the first local deployment.

The system needs inbound text only for the student's planner check-in/confirmation flow. Do not enable broad message collection. The gateway handler accepts messages only from `DISCORD_ACADEMIC_AUTHORIZED_USER_IDS` in exactly `DISCORD_ACADEMIC_CHANNEL_ID`; direct messages and every other channel are ignored. Keep `DISCORD_ACADEMIC_MESSAGE_CONTENT_ENABLED=false` for button-only or outbound-only deployments. Enable both that flag and Discord's privileged Message Content intent only when this narrow free-text reply flow is required.

### User setup in Discord

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications), then add a Bot user.
2. Copy the bot token into the secret store. The Application ID and Public Key are not runtime requirements for this Gateway-only flow; add them only if an HTTP interactions endpoint is implemented later. Rotate the token immediately if it is pasted into a terminal, chat, or repository by mistake.
3. In the Bot settings, enable only required gateway intents. Leave every privileged intent disabled for outbound-only or button-only testing. To receive the planner's inbound free-text check-ins, enable **Message Content Intent** in the portal and set `DISCORD_ACADEMIC_MESSAGE_CONTENT_ENABLED=true`; the listener then requests only Guild Messages and Message Content. Never enable Presence Intent or Server Members Intent.
4. Create a private server category or private channels for Finance, Code Review, and Planner. Restrict visibility to the owner and bot. A private channel is more reliable than bot DMs, which can be disabled by user/server privacy settings.
5. Invite the bot with the OAuth2 scope `bot`. The `applications.commands` scope is optional and is needed only if slash commands are implemented later; it is a scope, not a bot permission. Grant only these bot permissions: View Channel, Send Messages, Embed Links, and Read Message History. Add Attach Files only if the plan intentionally sends files. Never grant Administrator, Manage Server, Manage Roles, or broad moderation permissions.
6. Copy the owner user ID and channel IDs with Discord Developer Mode enabled. Put them in the secret/config store.
7. Start in a test channel. Send a test message, capture its message ID/permalink, and verify no other channel is readable or writable by the bot.

### Commands and messages

Keep the public command surface small:

| Input | Purpose | Allowed action |
| --- | --- | --- |
| Planner end-of-day reply in private channel | Report progress/new work | Creates a **proposed** planner/Notion change only. |
| `confirm <proposal-id>` | Confirm a displayed proposed change | Allows exactly that queued Notion patch. |
| `reject <proposal-id>` | Decline a proposed change | Marks the proposal rejected; makes no external write. |
| Optional `/status` | Read health summary | Reads LifeAgent data only. |

Do not add commands to trade, publish GitHub comments, modify finance sources, run arbitrary jobs, or expose raw source material. Agent-sent messages must include the run/proposal ID and a concise source/deep link. Persist a delivery intent before the REST call, then persist the message ID and permalink receipt on success.

For later buttons/modals/slash commands, Discord interactions may be received by Gateway **or** HTTP webhook, but not both for the same application interaction flow. If an HTTP interaction endpoint is added later, it must be publicly reachable, verify Discord signatures, respond to the initial request quickly, and enqueue long work; it must not run model inference inline.

### Discord connection tests

- The bot can send one test message only to the configured test channel and stores a delivery receipt.
- The gateway reconnects after a simulated network loss without duplicating an inbound event.
- A message from any user outside `DISCORD_ACADEMIC_AUTHORIZED_USER_IDS`, or from an unconfigured channel, is ignored before its content is inspected, audited, or stored.
- A planner reply creates a proposal; only a matching owner confirmation applies it.
- Rate-limit and permission failures become `attention`/`failed` diagnostics with no retry storm.

Official references: [Discord app setup](https://docs.discord.com/developers/quick-start/getting-started), [Gateway](https://docs.discord.com/developers/events/gateway), and [interactions](https://docs.discord.com/developers/interactions/receiving-and-responding).

---

## 3. Finance sources: exactly eight scoped connectors

### Before any finance connector is enabled

The architecture prohibits open web search and source substitution. A paid personal website subscription is **not automatically API permission**. Do not automate a logged-in browser, reuse cookies, scrape paywalled pages, use an unofficial news aggregator, or send licensed full article text to the model unless the provider agreement explicitly permits that exact automated/internal use.

Create an `approved_sources` record for every source before deployment:

| Field | Example |
| --- | --- |
| Stable source ID / display name | `sec_edgar` / `SEC EDGAR` |
| Version | `finance-allowlist-v1` |
| Base URL or contracted API product | `https://data.sec.gov` |
| Credential secret reference | `SEC_USER_AGENT` or secret-store key; never the value |
| Allowed operation | `filings since timestamp for configured CIKs` |
| Allowed fields retained | title, URL, publication time, issuer, filing type, short permitted extract |
| Request/rate limits | provider-specific configured ceiling |
| Licence/retention/LLM-use note | link to approved terms/contract confirmation |
| Approval audit event | approver, time, decision, review date |
| Health rule | freshness target and required/non-required status |

One briefing runs **exactly eight source calls**—one per enabled source below. “No relevant items” is a valid result. A source failure is recorded and reported; it never triggers a ninth call or a substitute site.

### Initial eight-source allowlist and connection method

| # | Source connector | How to obtain access | Connector scope | Important limit |
| --- | --- | --- | --- | --- |
| 1 | SEC EDGAR | No API key; set an honest descriptive `User-Agent` with contact information. | `data.sec.gov` company submissions/XBRL and linked EDGAR filing pages for configured CIKs. | Respect SEC fair-access guidance (currently no more than 10 requests/second across the client) and cache/deduplicate filing accession numbers. |
| 2 | Company investor relations | Build a per-issuer registry of the company-approved IR newsroom/RSS/API URL. No generic company web crawler. | Releases, earnings materials, and filings only for configured holdings/watchlist issuers. | Each URL/domain is a reviewed allowlist entry; prefer RSS or an official API over HTML parsing. |
| 3 | U.S. EIA | Register for a free EIA Open Data key; store it as `EIA_API_KEY`. | EIA API v2 routes selected for inventories, production, demand, prices, and energy releases. | Request only selected series/date window; API key is required for API calls. |
| 4 | U.S. government procurement | Create a SAM.gov account and generate a public API key. Use the Contract Awards API as the initial approved government-procurement source. | Award records for configured DoD agencies/contractors and date window. | Award data and permissions have access/timeliness limits; this is not proof of company revenue without company/filing corroboration. |
| 5 | Reuters | Obtain an explicit Reuters/Thomson Reuters product agreement that provides programmatic delivery and permits this internal automated research use. | Only the contracted Reuters feed/endpoints and the configured date/topic/ticker filters. | Do not scrape Reuters.com/Reuters Connect. Licence terms control retention, display, and whether text may be passed to an LLM. |
| 6 | Financial Times | Obtain an active FT Content/Headline API entitlement and API key that permits the planned internal use. | Only the licensed endpoint, normally headline/summary metadata for configured queries. | Do not turn a consumer FT subscription into automation. Respect contract cache/retention/display limits. |
| 7 | Wall Street Journal / Dow Jones | Obtain a Dow Jones/WSJ enterprise news API or feed agreement, with written permission for automated internal research use. | Only the contracted feed/endpoints and configured filters. | A WSJ website subscription is not an API. No direct website scraping or credential automation. Keep this connector disabled until access is confirmed. |
| 8 | Bloomberg | Obtain the appropriate Bloomberg product/API entitlement, commonly a Bloomberg Data License/API arrangement, and register required IPs/credentials. | Only licensed news/data endpoints and configured tickers/themes. | Bloomberg web APIs have product-specific auth/usage requirements; no Terminal/browser scraping. Keep disabled until a licence is confirmed. |

The fourth source intentionally uses SAM.gov Contract Awards rather than a loose scan of defence headlines. It is an official procurement source, gives structured award evidence, and satisfies the architecture's “Department of Defense contract announcements or other approved government procurement source” category. Add a separate Defense.gov contract-announcement adapter only by replacing a source in a new approved allowlist version—not as an uncounted extra call.

### Source-specific developer notes

#### 1. SEC EDGAR

- Maintain a `ticker -> CIK` mapping with a source/version; never assume ticker text is a unique SEC identifier.
- Poll submissions for only configured CIKs, then fetch only new accession numbers. Normalize filing type, filing date, issuer, accession number, primary document URL, and selected structured facts.
- Use conditional HTTP requests where supplied and a local request limiter below SEC's fair-access ceiling.
- Treat a filing as primary evidence. Preserve accession URL and exact filing timestamp.

Reference: [SEC developer resources](https://www.sec.gov/about/developer-resources) and [data.sec.gov](https://data.sec.gov/).

#### 2. Company investor-relations registry

Create a reviewed record per company:

```text
ticker, legal issuer name, CIK (if applicable), IR base domain,
approved RSS/API/release URL, earnings calendar URL, parser type,
last reviewed, source owner
```

The adapter fetches only these saved URLs. It accepts releases/earnings artifacts published after the source's watermark, records their original URLs/timestamps, and emits no claim until a parser validates the publication metadata. If an issuer changes its IR provider/layout, mark the source attention and ask for a registry update—do not fall back to web search.

#### 3. U.S. Energy Information Administration

- Register at [EIA Open Data](https://www.eia.gov/opendata/register.php), then set `EIA_API_KEY`.
- Configure a small approved series list, such as weekly U.S. crude inventory/production and relevant petroleum demand/pricing routes. Do not issue broad historical downloads for a daily briefing.
- Store series ID, units, frequency, period/as-of date, release date, and retrieval time. A daily value without its period/unit is invalid.

Reference: [EIA API v2 documentation](https://www.eia.gov/opendata/documentation.php).

#### 4. SAM.gov Contract Awards

- Create a SAM.gov account and generate a Public API Key in the account profile; store it as `SAM_GOV_API_KEY`.
- Use the Contract Awards API's documented, pagination-aware search endpoint. Configure DoD agency/recipient/date filters rather than running broad keyword searches.
- Normalize award/IDV identifier, awardee, agency, signed date, obligated/total value, description, and original SAM URL.
- Label award data as government-award evidence, not realized issuer revenue. The agent must explain uncertainty such as options, timing, subcontracting, or a parent/subsidiary relationship.

Reference: [SAM.gov Contract Awards API](https://open.gsa.gov/api/contract-awards/).

#### 5–8. Licensed news/data providers

Before writing code for Reuters, FT, WSJ/Dow Jones, or Bloomberg, obtain this written answer from the provider/account manager:

1. Which product/API/feed is licensed to this account?
2. May the application retrieve it automatically on a schedule?
3. May it retain title, URL, timestamp, summary, and excerpt? For how long?
4. May source text be sent to a self-hosted local LLM to create an internal summary? If yes, what maximum amount and retention rules apply?
5. May the generated briefing show any excerpt, or only a link/title?
6. What authentication, IP allowlisting, rate limit, audit, and attribution obligations apply?

Record the provider's answer and contract reference in the source approval record. Until then the connector returns `disabled_unlicensed`; it does not attempt authentication.

- **Reuters:** Reuters Connect offers subscription/licensing arrangements and Reuters/Refinitiv products can offer contracted API delivery. Confirm the exact product and AI-use terms; Reuters terms may restrict ML/AI uses. [Reuters Connect plans](https://www.reutersconnect.com/plans-and-pricing) are not by themselves an implementation specification.
- **Financial Times:** FT's API products are entitlement-dependent. Some headline API licence documentation describes an API-key flow and cache/rate limits, but use the terms provided to the actual account rather than relying on public examples.
- **Wall Street Journal/Dow Jones:** Treat this as an enterprise contracting task, not an engineering task. Implement only after Dow Jones confirms the supported feed/API and permitted internal use. A normal WSJ login must never be stored in LifeAgent.
- **Bloomberg:** Bloomberg Data License can provide REST/API/SFTP delivery; Bloomberg Web API access may require OAuth/JWT credentials and registered IP addresses. Use only the product documentation available to the licensed account. [Bloomberg Data License](https://professional.bloomberg.com/products/data/data-license/) and [Web API policy](https://console.bloomberg.com/about/82) describe the commercial/access model.

### Finance adapter behaviour

All eight adapters return the same normalized envelope:

```json
{
  "source_id": "sec_edgar",
  "source_version": "finance-allowlist-v1",
  "retrieved_at": "2026-09-02T13:00:00Z",
  "status": "ok | empty | failed | disabled_unlicensed",
  "items": [
    {
      "external_id": "provider-specific-stable-id",
      "title": "...",
      "url": "https://...",
      "published_at": "2026-09-02T12:30:00Z",
      "issuer_or_subject": "...",
      "primary_or_reported": "primary | reported",
      "permitted_excerpt": "...",
      "licence_class": "public | contracted-metadata | contracted-excerpt"
    }
  ],
  "diagnostic": null
}
```

The finance agent validates that exactly eight envelopes have the active allowlist version before it reasons. It stores source URL/time/title/outlet and permitted metadata, deduplicates the same event across sources, and gives Qwen only material allowed by that source's licence. Every event card links to the original source; it does not reproduce full articles.

### Finance connection tests and go-live gate

1. Run every adapter against a fixture/mock response first. Confirm it cannot make a request to a non-allowlisted hostname.
2. Run the four public/official connectors with a tiny date/ticker fixture and verify timestamps, units, IDs, source URLs, and request-rate limits.
3. For each licensed connector, add the provider-supplied sandbox/test credential or a contract-approved production smoke test. Do not test by scraping a website.
4. Simulate timeout, 429, invalid credential, empty result, and malformed payload for every source. A failed source must be visible in the health record and must not trigger a substitute source.
5. Run one complete dry-run briefing and assert there are exactly eight source envelopes, no trade directive, source citations on every factual claim, and no disallowed full-text content in the database/Discord payload.
6. The user approves `finance-allowlist-v1`—including licences, retention, and local-LLM-use notes—before the market-open schedule is enabled.

## First setup order

1. Configure and test Notion read-only sync with a small test database.
2. Configure the Discord bot in a private test channel and validate outbound delivery only.
3. Add the confirmed planner reply/confirmation flow, then enable the minimal inbound Discord permission/intent required.
4. Configure SEC, company-IR registry, EIA, and SAM.gov in dry-run mode.
5. Obtain explicit API/licence approval for Reuters, FT, WSJ/Dow Jones, and Bloomberg. Leave each disabled if approval is absent.
6. Approve the final eight-source `finance-allowlist-v1`, test an eight-call dry run, and only then enable the scheduled finance briefing.
