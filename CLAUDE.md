# Portfonia — Agent Guidelines

AI-facing guidance for agent tooling working in this repository.
Last updated: 2026-09-03 (trimmed for size — see `docs/playbooks/`; mechanism
deep-dives referenced from the table below live in `docs/mechanisms/`).

## No over-engineering (MANDATORY, 2026-09-18)

Requirements analysis, design, and implementation in this repository all
stop at "sufficient for the actual, current requirement" — never add a
mechanism, abstraction, validation layer, or defense-in-depth check because
it is common practice, sounds more correct, or covers a hypothetical future
need. This applies to every Ring and every agent/harness working in this
repo.

**Why**: issue #515 — a hand-rolled Origin-header check added to a
read-only, no-state-change confirmation endpoint (on top of the app's
existing global `CORSMiddleware`, plus that endpoint's own nonce+Altcha
protections) broke the first real production use of the entire
public-confirmation mechanism. This was the same failure pattern as an
earlier retracted isolated-service architecture (#473) at smaller scale: a
plausible-sounding security/robustness justification, unmatched to the
actual threat model, that added cost (implementation, review, LLM budget,
and — this time — actual breakage) without being asked for. (The feature
both incidents involved has since been removed entirely — issue #542 — for
the same class of over-engineering; this rule and its precedent stand
independent of that feature's existence.)

**How to apply**:
- Before adding any check, layer, table, service, or config knob: name the
  concrete requirement it satisfies. "Might be good practice" or "more
  correct" is not a requirement.
- If a mechanism can't be tied to something the product or the current
  checkpoint's Contract constraints actually need, don't add it — flag it
  and ask instead of building it "to be safe."
- When unsure whether something is required or is gold-plating, ask the
  product owner before designing or implementing it, rather than defaulting
  to the more defensive option.
- This does not license skipping the compliance/security boundaries this
  file already states elsewhere as hard requirements (layer-3 rule,
  encryption at rest, worktree/PR flow, etc.) — those are the actual
  requirement, not the kind of speculative addition this rule targets.

**Playbook files** (situational detail moved out of this file to keep it a
rulebook, not a diary — read the linked file when its topic comes up, not
proactively):
- `docs/playbooks/llm-and-data-handling.md` — full LLM model-routing
  reasoning, two-pass isolation and deny-only routing.
- `docs/playbooks/regression-notes.md` — full incident history for every
  "known-fixed bug" one-liner.
- `docs/playbooks/git-and-review-incidents.md` — stacked-branch recovery
  steps, the two-GitHub-identity boundary's full history **including a
  2026-08-28/2026-08-30 correction that changes the current rule** (read
  this one before assuming the inline summary alone is complete), the
  production-infra-leak incident.
- `docs/playbooks/testing-notes.md` — test DB isolation/PID-suffix
  reasoning, the `caplog`-after-migrate mechanism.

## Time Handling (MANDATORY)

Unless a specific scenario explicitly calls for a different clock (a
market's own local session — CST/HKT/LONDON/BERLIN/JST/KST, see
`app/core/timezones.MARKET_TZ`), **every date/time default in this project
is NYSE/ET (`America/New_York`), never the process's system clock**.

**Why**: issue #494 — the daily portfolio-snapshot capture called naive
`date.today()`, which reads the container's system clock (UTC in every
deployed environment here). The daily Beat fires at 20:30 ET, which is
00:30 UTC the next calendar day, so every snapshot was permanently
mislabeled one day late — a real user's 2026-09-14 data point went
missing because of it. Full incident: issue #494.

**How to apply**:
- Code: use `app.core.timezones.today_et()` (or `datetime.now(tz=ET)` for
  a timestamp, not just a date) — never bare `date.today()`/
  `datetime.now()`/`datetime.today()`.
- Infrastructure: production host OS and every container run with
  `TZ=America/New_York` (`docker-compose.yml`) — this is defense in
  depth alongside the code convention above, not a substitute for it.

## Public surfaces stay in sync with the product (MANDATORY, 2026-10-12)

Any change to behaviour, coverage, cadence, pricing, limits, data sources, formulas
or UI updates, in the same PR and in all four locales, every surface outside the
paywall that describes or samples it (inventory and procedure:
`docs/playbooks/documentation-governance.md`, "Public surface sync"). Public sample
figures trace to real market data, a product formula or a verified public event,
and a constructed sample never claims to be a real report. **Why**: issue #741.
**How to apply**: every issue Design ends with "Public surfaces affected:" (each
surface and its change, or "none"); reviews check it against the diff.

## Where to find current state

This file holds **conventions and mechanisms**, not a project status board.

- **Open bugs, requests, and technical debt**: GitHub issues, `debt` /
  `bug` / `enhancement` labels (see "Issue Tracking" below — everything new
  gets an issue first).
- **Ring stage, recent session summaries, running progress**: Obsidian
  `Hermes/Portfonia/` project log.
- **Build/test status, HEAD commit**: `git log`, `pytest -q`, `mypy .` —
  always run these rather than trusting a written-down snapshot.

## System conventions (current behavior, not status)

| Item | Value |
|------|-------|
| LLM model | OpenRouter, split by call shape. Structured/JSON (holdings parsing) = `STRUCTURED_LLM_MODEL` (`openai/gpt-5.6-luna`, `reasoning_effort=none`, `data_collection=deny`). Non-Jade Pass 2 + analyze regenerate = `PRIMARY_LLM_MODEL` (`anthropic/claude-haiku-5.5`, `reasoning_effort=medium`, `data_collection=deny`, unpinned, #793). Translation = `LOW_COST_LLM_MODEL` (`openai/gpt-6-luna`, `reasoning_effort=none`, `data_collection=deny`, flex tier with one standard-tier fallback, #793). Jade Pass 2 + analyze regenerate = `JADE_PASS2_MODEL` (`anthropic/claude-haiku-5.5`, `reasoning_effort=high`, `data_collection=deny`, unpinned, #725). No BYOK. *Full reasoning in `docs/playbooks/llm-and-data-handling.md`.* |
| Infrastructure | Homebrew PostgreSQL@16 + Redis (native, not Docker); `make infra-up` not needed |
| **App runtime retired locally (2026-08-10)** | No local uvicorn/celery worker/celery beat/Next.js dev server anymore — running the app for manual verification happens only via production deploy (see Three-layer deployment flow below). Homebrew Postgres/Redis stay running locally, but only as backing services for `pytest` (real-Postgres integration tests per the Tests section) — never as targets for a locally-running app process. Do **not** start `uvicorn`/`celery worker`/`celery beat`/`next dev` on this machine; if a task needs to be seen working, that means deploying to production, not spinning up a local server. The old "kill and restart uvicorn/celery after any model/migration/router change" drill no longer applies — there is no long-lived local process to go stale. |
| Output language | reason in EN, render in `output_lang` via a translation pass with a fixed-term glossary — locale-keyed, single source of truth in `backend/config/i18n_glossary.yml` (`report_glossary`/`forbidden_renderings`; only `zh-Hans` populated today, schema reserves `zh-Hant`/`fr`/`es` for later); `en` = no-op. **Per-user since issue #308**: Ops async generation/rerun and the scheduled fan-out read the requesting/recipient user's own `users.locale` (`VALID_REPORT_LANGUAGES = ("en", "zh", "zh-Hant")`, self-service `PATCH /me/report-language`, Ops `POST /admin/users/by-email/report-language`) — `Settings.OUTPUT_LANG` (default `en`) is now only the fallback for a missing principal row for unresolved users; Ops async generation resolves the target user in its worker (#642). |
| Report statuses | `success` · `skipped` (quiet day, still emails heartbeat — EXCEPT a short manual quiet window: `session_node="manual"` + <2h span + 0 news + 0 anomalies suppresses the heartbeat as a same-day re-run artifact) · `needs_review` (compliance scan hit, NOT emailed) · `failed` · `in_progress` |
| Report title / email subject | `Portfonia <Holdings Briefing> — YYYY-MM-DD HH:MM ET` (title timestamp from `period_end`; the zh-Hans render substitutes the `report_glossary` term for "Portfonia Holdings Briefing" from `i18n_glossary.yml`); no "Intelligence" wording, or its zh-Hans equivalent (`forbidden_renderings` in the same file), anywhere. |
| Holdings model | `market` + `broker` are user-declared fields; `position` preserves book order. **§1 groups by `broker` (rendered as "Custodian" — zh-Hans term in `i18n_glossary.yml`'s `report_glossary`)** in that order with per-institution subtotals; cash sits inside its institution, broker-less rows fall into "Other". `position` is set on confirm (append = max+1, replace = 0..n), single-row `POST /holdings`, and `PATCH /holdings/reorder`. |
| Holdings upload | Async, not a single blocking request — see "Async holdings upload" section below (issue #77/#82/#85). |
| Re-render | `regenerate_report(mode=render\|analyze)` rebuilds from stored `report_inputs` without re-fetching; `POST /admin/users/{user_id}/reports/{report_id}/rerun`. render = token-free, analyze = Pass 2 only. |
| §1 / distribution / §4.1 classification dimension | **`asset_class`** (geography-first taxonomy — see table below), not `sector` or `asset_type`. `sector` (yfinance GICS) is retained ONLY for forward-event holding-relevance mapping (rate-sensitive/consumer sectors for FOMC/CPI events) — never reintroduce it into §1/distribution/§4.1. `by_asset_class` has no "Other" fallback (every `Holding` always has one, default `STOCK`). |
| Tests must mock external notify calls | `send_ops_alert`, `create_bug_report`, `send_report_email`, `send_subscription_notice` are mocked via an **autouse** fixture in `app/tests/conftest.py` (`_no_external_notifications`); `send_admin_alert_task.delay` is stubbed by `_rate_limit_memory` so a 429 cannot enqueue real Celery work. Never rely on individual tests remembering to patch them. A gap here previously sent 42 real "FX rates stale" emails to the admin inbox from three same-day pytest runs (test clock fixed to a historical date that always trips the staleness check against the real current date). |
| Identity (B4, issue #129, PR #183) | Request identity is `Depends(current_principal)` only: `Authorization: Bearer` verified against hosted Auth JWKS (ES256/RS256; **no `JWT_SECRET` Settings field**), then `users.auth_subject` + `status=active`. Missing/forged/unknown-`sub` → 401, **no auto-insert**. `get_current_user_id()` raises. Ops `/admin/*` is a separate bearer (`ADMIN_API_TOKEN`), not this JWT. **Do not deploy this cutover without B5** — unauthenticated `/holdings` `/reports` `/portfolio` now 401. `current_principal` also enforces a 15-minute rolling idle timeout (issue #235) and an 8-hour absolute session lifetime cap (issue #236, permanent app-level — Supabase's own Time-box is Pro-tier-only and inert on this project's Free plan); `GET /auth/session-status` is a cheap 200 probe returning Advanced access the frontend top-bar menu calls to avoid showing authenticated entries once either has expired. |

Every Beat entry must declare an explicit boolean in `API_QUIET_BEAT_ENTRIES`; heavy means LLM/paid API calls or measured runtime over one minute, and every new or changed entry must state its value in the issue Design for product-owner confirmation.

### Mechanism deep-dives

One line per document. Read the linked file before touching that area of the
code; it holds the full implementation record (root cause, design tradeoffs,
review provenance, per-issue history). **Keep each line to the subsystems the
file covers: record issue-by-issue changes in the mechanism file, never here.**
The per-issue index text that stood here until 2026-10-21 is archived in
`docs/playbooks/mechanism-index-legacy-20261021.md` (lookup only).

- [Capture and reporting](docs/mechanisms/capture-and-reporting.md) — capture nodes and report windows (ADR-002), per-user cadence and language, async Ops generation, web report history, instrument news collection, intel deepening and paid usage, Exa/sector slots, related-company links and the weekly name check, report-time intelligence reads (L1/L2/L3 shared analysis removed), Pass 2 material, FX coverage and per-pair `fx_rates_as_of`, multi-market capture, price-fetch fallbacks, fund NAV/China ETF fallbacks, operational event log. `lxml` stays installed although no code calls `get_earnings_dates`.
- [Report content and email](docs/mechanisms/report-content-and-email.md) — §4.2 anomaly table, confidence labels, technical position page, forward calendar, theme-anomaly weighting, email HTML/text rendering and List-Unsubscribe, single-language footer, §3 proportionality check.
- [Report generator refactor](docs/mechanisms/report-generator-refactor.md) — `report_generator.py` module split.
- [Macro coverage continuity](docs/mechanisms/macro-coverage-continuity.md) — §2 eligibility, overview+anchor+updates composition, `macro_coverage` table, regenerate semantics.
- [Macro keywords](docs/mechanisms/macro-keywords.md) — theme pool; bare single-word keywords false-fire, always qualify.
- [News dedup](docs/mechanisms/news-dedup.md) — `news_surfaced` ledger, per-user uniqueness, capped-window backfill.
- [LLM reliability](docs/mechanisms/llm-reliability.md) — `LLMErrorCode`/`ErrorPolicy`, same-day windows, Pass 2 completeness guard, retry/backoff, resumable retry.
- [Compliance and classification](docs/mechanisms/compliance-and-classification.md) — forbidden-vocab scan and ops alerting, `asset_class` taxonomy and thresholds, fund NAV classification, leverage multiplier, leveraged-product registration and detection. **The scan has a known structural limit: read its "Known limitation" note before opening an issue for the next false positive/negative.**
- [Holdings pipeline](docs/mechanisms/holdings-pipeline.md) — async upload, encryption at rest, domain CHECKs, accounts/`user_id` FKs, single-row CRUD and confirm modes, Portfolio overview and exports, overview email, per-user currency, `watch_tier`, holding change history.
- [Instrument symbols](docs/mechanisms/instrument-symbols.md) — shared ticker/market normalization; no per-ticker special cases.
- [Portfolio performance](docs/mechanisms/portfolio-performance.md) — snapshot tables, approximate EOD TWR, tracking start, benchmark/FX backfill, capture durability (outbox), capture-health alert and FX live quotes, allocation history, gap connector, snapshot export API.
- [Portfolio risk](docs/mechanisms/portfolio-risk.md) — volatility, Beta, questionnaire-relative Risk and Deviation. The logged-out homepage preview re-declares the panel's chart JSX: mirror changes to `risk-panel.tsx` exports or chart settings there.
- [Jade tools](docs/mechanisms/jade-tools.md) — holdings replay, tail risk, stress scenarios, style exposure, custom reference and tracking (`/jade/tracking`), rebalance and what-if, guide pages, Agent reads, public sample demo.
- [Agent API](docs/mechanisms/agent-api.md) — personal tokens, limits and quiet windows, audit, report/intelligence/technical pulls, notices and revocation, Jade-only holdings management, change history and rollback, Jade risk reads, large-change alerts, public OpenAPI schema.
- [Identity and auth](docs/mechanisms/identity-and-auth.md) — JWKS auth and `current_principal`, idle timeout and absolute lifetime, client idle-expiry, signup/invite anti-abuse, forgot password, questionnaire, analysis framework, Profile and `GET /me`, waitlist and invitation letters (automatic daily sends), referrals, Ops user directory and hard purge, self-service deletion.
- [Email verification](docs/mechanisms/email-verification.md) — verification core and Ops API, signup hook, report unsubscribe, verified-recipient gating.
- [Credit ledger](docs/mechanisms/credit-ledger.md) — balances, append-only ledger, purchases/refunds, purge protection, relinquishment, referral rewards and clawbacks.
- [Subscription](docs/mechanisms/subscription.md) — plans and charges, cancel/resume/unsubscribe, scheduled lifecycle, Daily Advanced, Jade tier and offers.
- [Payments and legal copy](docs/mechanisms/payments-and-legal-copy.md) — Paddle checkout, webhook crediting, refunds, Pricing comparison and legal pages.
- [Frontend chrome](docs/mechanisms/frontend-chrome.md) — shared `SiteHeader`, menus and locale switcher, onboarding flow, public SEO pages and locale URLs, Jade page and logo, calculating overlay.
- [Admin surface](docs/mechanisms/admin-surface.md) — `/admin/*` token auth, rotation and audit logging.
- [Backup and ops](docs/mechanisms/backup-and-ops.md) — daily `pg_dump` to object storage, restore drills, container instance-principal IMDS SNAT dependency.
- Playbooks: [documentation governance](docs/playbooks/documentation-governance.md) (incl. public surface sync), [full-context review](docs/playbooks/full-context-review.md), [git and review identities](docs/playbooks/git-and-review-incidents.md), [LLM and data handling](docs/playbooks/llm-and-data-handling.md), [regression notes](docs/playbooks/regression-notes.md), [testing notes](docs/playbooks/testing-notes.md).

## Language Policy (MANDATORY)

- **All repository content is English**: code, identifiers, comments, commit
  messages, PR descriptions, issue text, README, `docs/`, ADRs, tests.
  Repository filenames and documentation indexes must also be English;
  do not embed Chinese note titles or paths in repository documentation.
- **Obsidian project documentation is written in Chinese** (the product
  owner's working language) — this applies to `Hermes/Portfonia/*` design
  docs, dev logs, and any other Obsidian note for this project, which sit
  outside the repository entirely and are therefore not covered by the
  English-only rule above.
- **Conversation with the product owner in this repository's sessions
  defaults to Chinese**, regardless of the language of the request, unless
  the owner asks otherwise. This does not relax the English-only rule for
  anything written into the repository itself (code, commits, PRs, issues).
- **A handoff prompt written for a new session to continue work in this
  repository is written in Chinese and explicitly instructs the receiving
  session to converse with the owner in Chinese** — the same rule as live
  conversation above, stated explicitly so a fresh session with no memory
  of this convention still follows it from its first reply.
- **In-product strings are i18n-keyed** and shipped through the translation
  layer, never hardcoded in any single language. Runtime UI locales
  exposed to users: English, Spanish, Simplified Chinese, and Traditional Chinese
  (issue #209 added English and Simplified Chinese; issue #350 item 4 lifted Traditional
  Chinese's review gate — see `frontend/src/locales/README.md` for adding
  a fourth, and its "zh-Hant review status" section for the gate-lift
  history). `zh-Hant.json`'s catalog was originally LLM-drafted with no
  native-speaker review (blacktomb42 review, PR #226 round 2: an earlier
  version of this line overclaimed it as supported while still gated) —
  the product owner explicitly chose to ship it anyway rather than wait
  for that review; this is a deliberate, logged decision, not an
  oversight to "fix" by re-gating it. Report output languages are
  separate from UI locales: stored report codes are `en`, `zh` (Simplified),
  and `zh-Hant` (Traditional), since issue #582. Spanish (`es`, Latin American neutral es-419)
  is LLM-drafted and ships without native review by owner decision (#735).
  It is UI-only; Spanish UI users receive English reports, emails and exports. Both Chinese codes select
  the existing `zh-Hans` glossary source; `app/services/zh_hant.py` supplies
  Taiwan conversion using OpenCC `s2twp` and `config/zh_hant_terms.yml`.
  Report rendering landed in #583: both compliance scans run on the
  Simplified text, then the assembled report (plus the report email's
  subject and unsubscribe footer) is converted to Traditional. Transactional
  emails, invitation letters and their unsubscribe page support hand-authored
  Traditional copy since #584; only glossary-derived overview text is converted.
  Holdings/template and portfolio exports support hand-authored Traditional
  copy since #585; parser vocabulary adds Traditional variants at load time.
  Driven per-user by
  `users.locale` since issue #308; `Settings.OUTPUT_LANG` remains the fallback
  default. A UI locale is not a report language.
- Translation resources live under a dedicated locales directory
  (`frontend/src/locales/*.json` for UI chrome; `backend/config/
  i18n_glossary.yml` for report output; `_VERIFICATION_EMAIL_COPY` in
  `backend/app/services/email_sender.py` for the one transactional
  verification email, issue #260/PR #261; `_INVITATION_LETTER_COPY` in
  `backend/app/services/email_sender.py` and the unsubscribe page copy
  dict in `backend/app/routers/invitation_letters.py`, issue #569;
  `_RULES_ZH` / `_EXAMPLES_ZH` and `_RULES_ZH_HANT` / `_EXAMPLES_ZH_HANT` in
  `backend/app/services/holdings_export.py` for the holdings
  export/template dialect (English, Simplified and Traditional),
  issue #92/PR #310/#585 —
  `_HOLDINGS_CHANGE_ALERT_COPY` in `backend/app/services/email_sender.py` for
  large holdings-change alerts (issue #785);
  `_API_ACCESS_NOTICE_COPY` in `backend/app/services/email_sender.py` for
  agent snapshot-access notices (issue #651); see the Mechanism deep-dives table)
  and are the only places where non-English text legitimately appears in
  the repo. A lint rule (`i18next/no-literal-string` in
  `frontend/eslint.config.mjs`) enforces this for UI code.
  `_VERIFICATION_EMAIL_COPY` is deliberately its own small dict rather
  than folded into either existing mechanism: the next-intl catalog is
  browser-only and unreachable from this backend module, and
  `i18n_glossary.yml` is built for large LLM-generated report bodies, not
  a two-line transactional email — stored locale codes (`en`/`zh`/`zh-Hant`),
  matching `users.locale`/`OUTPUT_LANG`'s convention, not the frontend catalog's
  BCP-47 `zh-Hans` tag. The holdings export/template strings stay in
  `holdings_export.py` for the same reason: they are a downloaded file
  dialect, not report glossary terms — **since issue #319/PR #321**,
  `GET /holdings/export`/`GET /holdings/template` take an optional
  `locale` query param sourced from the frontend's UI locale that
  overrides the `users.locale` (report language) fallback these two
  endpoints used before; `GET /portfolio/export` (issue #331/PR #335)
  takes the same optional `locale` with the same precedence. Report
  generation itself is untouched.

## Product Boundary (NEVER VIOLATE)

Portfonia is an **intelligence service**, not an advisory service.

### Three-layer output rule

AI-generated content stops at layer 3. Layer 4 is a hard prohibition.

```
Layer 1  What happened                       (pure fact)
Layer 2  How it relates to your holdings     (contextual mapping, no judgment)
Layer 3  Signals worth watching              (point to observation, not action)
─────────────────────────────────────────────────────────────────
Layer 4  What you should do                  (FORBIDDEN — never emit)
```

### Forbidden vocabulary in any AI-generated output

`recommend`, `should`, `buy`, `sell`, `hold`, `reduce`, `increase`, `exit`,
`stop-loss`, `target price`, `will rise/fall to`, `entry point`, `oversold`,
`overbought`, `strong buy`, `bullish/bearish rating` — and their equivalents
in any other language. This is the prompt-side rule (the model's own voice
must never use them); the output-side scan backstop below is narrower for
several of these — see "Output-side backstop" for which ones stay a bare
block vs. a context-aware/third-party-attribution-aware scan vs. dropped
entirely as Layer-3 observation vocabulary.

### Compliance scaffolding

- Disclaimer text is injected at the **template layer**, not by the model.
  Every report has fixed header + footer disclaimers, rendered in the
  report's own `output_lang` only (issue #350 item 3 — previously always
  EN + zh-CN regardless of the recipient's actual language; see
  `report_sections._build_footer`). AI fills only the body region.
- Prompt-level hard constraints (the layer-3 rule + vocabulary blacklist)
  are part of the system prompt for every report and Q&A flow. Do not move
  these constraints to user-tunable prompts.
- **Output-side backstop**: prompt instructions are not a guarantee, so the
  generated body is scanned post-generation (`_scan_forbidden_output`) for
  high-precision advisory phrases. A hit sets the report status to
  `needs_review` and **suppresses email** — content is preserved for
  inspection, never delivered. The scan covers the LLM body only, never the
  template footer (whose disclaimer legitimately contains "buy/sell").
  English `recommend*` is context-aware (issue #375): first-person /
  product-to-user / `recommend you` / sentence-initial `recommend buying|selling|holding|reducing`
  still hold; third-party house-view attribution ("UBS … recommends",
  "the bank recommends", "analysts recommend") does not. Prompt blacklist
  still includes `recommend`. Residual: a user-directed recommend that
  mimics third-party syntax in the same clause may slip. Issue #443 applied
  the same de-bluntening to every remaining bare EN literal (product-owner
  direction: a cited third-party article routinely carries this vocabulary
  as factual background, and a bare-word scan can't tell that apart from
  the model's own advice): `entry point`/`target price` dropped to
  prompt-only (mirror ZH 入场/目标价, #65); `reduce exposure`/
  `increase position`/`stop-loss` kept a directive-context scan (mirror ZH
  止损/清仓, #74/#205); `oversold`/`overbought` dropped from the scan
  entirely — an objective TA state has no directive-shaped use to preserve,
  so they join the already-unscanned `support level`/`resistance level`/
  `golden cross`/`breakout` observation vocabulary; `strong buy`/
  `bullish rating`/`bearish rating`/`will rise to`/`will fall to` kept a
  third-party-attribution-aware scan (mirror `recommend*` itself, #375).
  Every one of these terms stays in the prompt blacklist regardless of scan
  treatment — the model's own voice is still told to avoid all of them.
  Built on two reusable builders in `forbidden_vocab.py` —
  `_directive_pattern()` for action-directive terms and
  `_own_voice_or_bare_assertion()` for rating/forecast terms — so
  adding/removing a modal, determiner, pronoun, or attribution verb is a
  one-line change to a shared constant, not a per-pattern hunt. *Five
  rounds of review (blacktomb42, PR #444) hardened this mechanism against
  real false-negative/false-positive pairs — full incident history:
  playbook `docs/mechanisms/compliance-and-classification.md` ("EN scan
  patterns: shared directive/own-voice builders").*
- **Single footer disclaimer, no inline markers** (2026-06-08; single-language
  since issue #350 item 3): the compliance base is the one disclaimer in the
  footer, rendered in the report's own language. The body carries NO
  per-sentence `[For information only…]` suffix and NO bracketed provenance tags
  (the legacy market-data/news/analysis marker tags stripped by
  `report_generator._STRAY_TAGS`, sourced from `i18n_glossary.yml`'s
  `legacy_removed_markers_zh`, or `[S#]`). The system prompt forbids the model from
  emitting them, and `_strip_markers` removes any that slip through. The scan
  backstop above does not depend on the suffix.

### Known-fixed bugs worth remembering (regression notes)

*Full incident history for every item below: playbook `docs/playbooks/regression-notes.md`.*

- **Fund NAV lookup**: `compute_portfolio` must look up price data with
  `captured_closes.get(h.ticker or h.fund_code or "")` — fund code-only
  holdings have no `ticker`, and `capture_fund_navs` stores NAV in
  `price_snapshots` keyed by `fund_code`. A ticker-only lookup silently drops
  every fund holding into `stale_tickers` — since #295 the row stays visible
  in §1 as `[price unavailable]` but is excluded from every aggregate (issue
  #1)
- **Sector backfill on re-upload**: `confirm_holdings` must call
  `backfill_sectors()` after commit — re-uploading holdings clears all rows,
  and `sector` is otherwise only populated by `POST /admin/portfolio/refresh`
  (moved from `POST /portfolio/refresh`, removed, in issue #129 checkpoint B2)
  or the scheduled capture tasks.
- **Next.js Turbopack + multipart**: Turbopack's `rewrites()` fails on
  `multipart/form-data` POST (ECONNRESET at proxy). Upload routes need a real
  Next.js API Route (`route.ts`) that manually forwards to the backend.
- **`frontend/public/` must stay non-empty** (issue #100/#101): git doesn't
  track empty directories, and `frontend/Dockerfile`'s runner stage copies
  it verbatim — a missing directory fails the Docker build (not `next
  build`/`bun run dev`, which don't care). Keep at least one tracked file
  there (`.gitkeep` is fine) even after removing every real asset.
- **Frontend has exactly one lockfile (`bun.lock`)** (issue #227/PR #255) —
  never reintroduce `package-lock.json`; `frontend/Dockerfile` runs `bun
  install --frozen-lockfile`, no separate lockfile-sync step needed.

## Architecture

| Layer | Choice |
|-------|--------|
| Frontend | Next.js + shadcn/ui. **Package manager is `bun`, exclusively — never `npm`/`npx`/`yarn`/`pnpm`, anywhere** (local dev, CI-equivalent gates, Dockerfile build) — reaching for `npm install`/`npm ci` even once caused three separate production deploy failures before issue #227 fixed it (full history: playbook `docs/playbooks/regression-notes.md`). |
| Backend | Python FastAPI |
| Database | PostgreSQL, self-hosted in Docker on the production VPS (not Supabase-managed — decided 2026-08-05 to cut hosting complexity). Supabase is used for **Auth only**. |
| Task queue | Celery + Redis |
| LLM | Pluggable (Claude / DeepSeek / etc.) — keep provider-swappable |
| Local dev | Homebrew PostgreSQL 16 + Redis (native), used only to back `pytest`'s real-Postgres tests — the app itself does not run locally anymore (see System conventions table). Colima serves this project's local development and testing, mainly on-demand Docker builds; it is normally stopped (see Quality Gates). |
| Production | Self-hosted on a free-tier cloud VM, Ubuntu 24.04 LTS. Provider, region, instance identifier, and IP are deliberately **not tracked in this repo** — see Obsidian doc below. |

### Three-layer deployment flow (MANDATORY)

Full step-by-step procedure — SSH/`systemd-run` launch, dropped-connection
handling, health-check verification, the required-`Settings`-field gate
before `docker compose up --build`, the stale-transient-unit fix, and the
separate smaller "env-only sync" procedure for a config-only change with no
code change — lives in [`docs/deployment.md`](docs/deployment.md). Read it
before running any production deploy or `.env` rollout; production server
specs (provider, region, instance name, IP, SSH user, remote paths) are
**never** in this repo — see Obsidian `Hermes/Portfonia/Portfonia
Environment Config.md`.

The one hard rule that governs every action there: code authority is
**local → Git only**. Never edit code on the production server, never `git
commit` there, never use it as a sync hub between machines — its only
legitimate local state is `.env` (uploaded via `scp`).

## Secrets and Configuration

- `.env` files are **never** committed. Enforce via `.gitignore` from day one.
- API keys (Claude, Resend, market-data providers) are loaded from `.env` only.
  Never hardcode, never log, never echo to stdout in error paths.
- **`.env.local` is untracked and per-worktree** (issue #56, 2026-09-04): a
  key rename or new key added while working in a task worktree lives only
  in that worktree's copy — it does **not** propagate back to the main
  checkout's `.env.local` when the worktree is removed, and there is no
  git history to catch the drift since the file was never tracked. After
  removing a worktree that changed `.env.local`, manually replicate the
  same edit to the main checkout's `.env.local` (or the setting silently
  reads as unset there — `Settings.<KEY>` fields default to `None`, so
  this fails quiet, not loud).
- **A fresh worktree has neither `.env.local` nor `backend/venv`** (both
  untracked, neither one a git-tracked path): running `pytest`/`ruff`/
  `mypy` in a new worktree fails outright (`Field required` config errors,
  or `ModuleNotFoundError` if a stray global `python3` is used instead)
  until both are wired up. Symlink `.env.local` from the main checkout
  (`ln -s /Users/garyj/Portfonia/.env.local .env.local` at the worktree
  root) and invoke the main checkout's venv by absolute path (e.g.
  `/Users/garyj/Portfonia/backend/venv/bin/python -m pytest`) rather than
  assuming an activated venv or a project-local one exists.
- For test code: never read or write the developer's real `~/.config/...`
  directories. Honor a project-scoped env var (e.g. `PORTFONIA_HOME`) and
  default tests to a temp dir. Direct use of `os.path.expanduser("~")` in
  code that tests will exercise is a bug.
- **Never commit a traceable production infrastructure identifier to this
  repo**: no real IP address, no cloud provider/region, no instance name/ID,
  no SSH username, no remote filesystem path — regardless of whether the repo
  is currently public or private (visibility can change, forks/clones
  persist regardless). This applies to `CLAUDE.md` and any other tracked
  file, not just code. The actual specs live only in the private Obsidian
  ops doc referenced from the deployment section below. *Full incident this
  rule comes from (2026-08-06 leak, ~30h exposure): playbook
  `docs/playbooks/git-and-review-incidents.md`.*

## Data Handling

- User holdings are sensitive. Encrypt at rest. **Never** include raw user
  holdings in training data, LLM fine-tuning datasets, or third-party logs.
- When sending holdings to an external LLM, scope the payload to what the
  current report needs. Do not attach the full portfolio history "just in case".
- **Scheduled intelligence isolation (enforced):** collection and paid
  deepening run in the scheduled intel slot. The report path reads only
  URL-free accepted bodies linked to the requesting user's identifiers and
  macro themes; it performs no outbound search and no collection LLM call.
- **Every LLM call uses `data_collection=deny`, with no exception.**
  Issue #793 supersedes the #78 translation exception and #477 fallback.
  *Full reasoning: playbook `docs/playbooks/llm-and-data-handling.md`.*
- Market data: cache same-day, same-symbol queries. yfinance is the default
  source; treat rate limits as a real constraint when adding new query paths.
- FX rates: pull once per day into the FX table; all valuation reads from that
  table. Do not call the FX source from request paths.

## Quality Gates (run BEFORE pushing)

Order matters because `validate` checks formatting non-destructively.

```bash
# Backend (FastAPI / Python)
ruff format .            # 1. fix formatting
ruff check --fix .       # 2. fix lints
mypy .                   # 3. types
pytest -q                # 4. tests

# Frontend (Next.js)
bun run lint:fix         # 1. eslint --fix
bun run typecheck        # 2. tsc --noEmit
bun run test             # 3. tests
bun run build            # 4. production build (MANDATORY for any frontend code change)
```

**`bun run build` is mandatory for every PR that touches `frontend/` code**
(product owner, 2026-10-04, after a production deploy failed). PR #646
(issue #642) passed lint, typecheck and tests, then failed `next build` at
deploy time: `useSearchParams()` without a `<Suspense>` boundary breaks
prerendering, which only the production build checks. Run it with the
`NEXT_PUBLIC_SUPABASE_URL`/`NEXT_PUBLIC_SUPABASE_ANON_KEY` build variables
set (placeholder values are enough), state the result in the PR body, and
put this step in every implementation prompt given to another agent.

There is no frontend formatter (`prettier` is not a dependency) — `bun run lint:fix`
is the only auto-fixing step. A prior version of this table listed a
`bun run format` step that never existed as a package.json script; fixed
2026-08-28 rather than left to drift further (see issue #227's frontend
Dockerfile fix for the sibling doc/reality gap this was found alongside).

**A docs-only diff skips this suite** (product owner, 2026-09-10). When the
diff touches no code — markdown under `docs/`, `CLAUDE.md`, `AGENTS.md`, a
vault note — none of the four backend or three frontend steps can be affected
by it, so running them is latency with no signal (a session burned ~10 minutes
doing exactly that for a one-line status note, which is what this rule
prevents). Say `docs-only` in the PR body instead, so the reviewer sees the
gate was skipped deliberately rather than forgotten. A PR that mixes docs with
any code change runs the full suite as usual.

Final gates (enforced by the local quality gate above, not CI — see CI-First Protocol):

- Type check passes (mypy strict, tsc strict).
- Lint passes with zero warnings.
- Format check passes (non-mutating).
- All tests pass.
- Frontend production build (`bun run build`) passes when `frontend/` code changed.
- No `any` / `Any`, no non-null assertions, no unused exports.

**Gap this doesn't cover**: none of the above actually builds the Docker
images. A change that only breaks `docker build` (e.g. deleting the last
file in `frontend/public/` — see the regression note above) passes every
gate here and still fails at deploy time, silently, until someone actually
redeploys. When a change touches `frontend/public/`, either `Dockerfile`,
or `docker-compose.yml`, run a real `docker build`/`docker compose build`
before pushing — `bun run dev`/`next build` do not exercise the same path
and will not catch this class of bug. Docker runs in Colima, which is
normally stopped on this machine: run `colima start` before the build and
`colima stop` after it, whether the build passed or failed. A missing Docker
socket means Colima is stopped, not that the build cannot be run.

## CI-First Protocol (MANDATORY)

> **Current reality (still true well into Ring 1, not a Ring-0-only state):**
> there is no automated CI — no PR-triggered workflow exists; the only
> GitHub Actions workflow is `release.yml`, which runs on push to `main`
> only and does not run tests (see Releases below). The local quality gate
> (see above), run before every push, stands in for CI. There IS a branch +
> PR for every change (see Branching below); "CI green" currently means
> "local gate green" on the PR's branch, and `gh pr checks` on an open PR
> will report no checks at all — that is expected, not a signal something
> is broken.

A task is NOT complete until CI is green.

After every `git push`:

1. Immediately run `gh pr checks --watch` (or `gh run watch`) and block until
   all checks finish. Today this reports "no checks reported" instantly
   (see the note above) — that is the expected, correct result on a PR
   branch, not a failure to investigate. Keep running this step anyway: it
   costs nothing, and it is what will actually surface a real check the
   moment PR-level CI is ever added.
2. **Green** → task may proceed.
3. **Red** → pull failing logs with `gh run view --log-failed`, fix the root
   cause locally (never retry blindly), commit, push again, re-watch.

**Verifying a reported merge is not re-running the gate** (2026-09-15): the
quality gate already ran green at commit/push time — that's what made the
commit mine to push. Checking that a reported merge actually landed means
`gh pr view --json state,mergeCommit,mergedAt`, `git fetch && git pull
--ff-only`, and a diff spot-check against the issue's Design comment — not
a fresh `ruff`/`mypy`/`pytest -q` pass. Re-run the full gate only with a
specific reason to doubt the reported green state (a rebase after the gate
ran, a manually-resolved merge conflict, or an explicit request).

Do not declare a task done, close a session, or move to the next task while
CI is red or still running. Leaving a PR red and moving on is the primary
failure mode this protocol exists to prevent.

## Branching

**Isolated worktree requirement (all repository changes):** The main checkout
is read-only for task edits. Create a separate git worktree on a task branch
before changing code, configuration, documentation, or agent instructions;
switching branches in the main checkout is not isolation. Keep main clean,
preserve unrelated user work, and submit the task branch as a PR for review.
Small or documentation-only changes have no exception. See AGENTS.md for
the task-edit recovery procedure. Merge/deployment authorization remains
separate from creating the PR.

> **2026-08-06 correction:** every change — code, config, or docs, at every
> Ring, no solo-work exception — starts on a branch and goes through a PR.
> The prior "Ring 0 commits directly to `main`" carve-out is retracted: it was
> read (incorrectly) as also licensing autonomous PR merges, and PR #79
> (issue #78) was merged without the product owner's sign-off as a result —
> reverted same day. **Merging any PR into `main` requires the product
> owner's explicit, real-time approval in the current conversation.** A green
> quality gate, a passed review (including a reviewer-identity self-review),
> or an issue/task description that says "implement and merge" are NOT
> themselves that approval — they make a PR ready to ask about, not ready to
> merge. Finishing a PR ends with "ready for your review" or "ready to
> merge?", not with `gh pr merge`.

```
main (production) ← dev (integration, Ring 1+ target — not yet in use) ← feat/* | fix/* | docs/*
                                                                           ↑
                                                                           hotfix/* (only emergencies, from main)
```

- Never commit directly to `main`. `dev` doesn't exist yet, so `feat/*` /
  `fix/*` / `docs/*` branches currently start from `main`; switch to
  branching from `dev` once it exists.
- `dev → main` promotion PRs must use `feat:` or `fix:` (a `chore:` title
  will not trigger a release).
- Delete branches after merge.
- **Stacked branches (branch B built on not-yet-merged branch A) + squash-merge
  is a known trap**: squash-merging A with `--delete-branch` auto-closes any
  open PR whose base is A's branch, with no recovery. If A merges before B
  is done, get B's commits onto `main` via `git merge main` (not `git rebase
  main` — see playbook for why) and open a fresh PR noting which closed PR
  it supersedes. *Full recovery steps + a `git merge` footgun this surfaces:
  playbook `docs/playbooks/git-and-review-incidents.md`.*

## Admin surface: API endpoint first, UI later (MANDATORY)

Any feature with an **administrative purpose** — something only the product
owner uses, not part of a normal user's journey — ships first as an
`/admin/*` API endpoint authenticated by an ops token. A management UI is an
optional layer on top of those endpoints, never a prerequisite for the
capability existing.

- **Status**: implemented (issue #129 Ring 1 stage B, checkpoint B2). See
  [`docs/mechanisms/admin-surface.md`](docs/mechanisms/admin-surface.md) for
  the endpoint history, the `ADMIN_API_TOKEN`/`_PREV` rotation auth, why the
  ops channel is deliberately NOT the user auth system, and the audit-logging
  + brute-force-alert mechanism.
- **Living endpoint reference**: every implemented and planned `/admin/*`
  endpoint (path, auth, params, curl example) is tracked in Obsidian
  `Hermes/Portfonia/Docs/Ops API Reference.md` — update it in the same
  change that adds/modifies/removes an endpoint, not at stage cleanup.
- Consequence to accept openly: some capabilities will exist with **no user
  interface**, reachable only via curl or an agent calling the endpoint. That
  is the intended tradeoff, not an oversight.

Full design, including token rotation, constant-time comparison, router-level
auth declaration, and audit logging: Obsidian `Hermes/Portfonia/Docs/Ring 1-B design.md` §4.

## Issue Tracking (MANDATORY)

**Project-wide issue documentation contract (2026-09-08):** Follow
`AGENTS.md` for issue structure and implementation-ready design requirements.
Keep the body concise; publish Requirements, Reasons, Exploration, Design,
and Contract constraints as five separate comments. Design must specify
modules/data flow, schema/API, algorithms, ordering, failure branches,
compatibility, and worked examples. Contract constraints must specify
invariants, dependencies, exclusions, concrete acceptance tests, validation,
and release/authorization gates. Desired outcomes alone are insufficient.
Separate confirmed decisions from authored designs and unresolved questions;
never invent approval. Keep design and validation proportional to the authorized
problem; do not turn this template into extra tooling, cleanup or operational
requirements. The owner decides whether adjacent work belongs in scope. Apply
this to all Portfonia issues, not just #377.

Every new feature/improvement request and every bug — regardless of whether
it's fixed immediately — gets a GitHub issue first, before the fix/feature
work starts. Issues are the project's request/bug ledger; the CLAUDE.md debt
table is for cross-session technical-debt reminders only, not a substitute.

- **Blocking / fix-now**: open issue → fix/implement → comment with commit
  hash + approach + verification → close.
- **Deferred**: open issue → leave in backlog → comment + close when later
  addressed.

**GitHub access and separate identities:** never attempt `gh auth login`/
stored OAuth for this project (retracted 2026-09-18 — OAuth reported
invalid mid-session despite the owner re-verifying it). Writes use
`GITHUB_TOKEN` from this project's `.env.local`, verified as `portfonia`,
via `gh api` (REST endpoints) exclusively — never `gh issue`/`gh pr` or
other high-level porcelain subcommands, which may call GitHub's GraphQL
endpoint internally. Reviews use `.env.local` `GITHUB_REVIEWER_TOKEN`,
verified as `blacktomb42`; never use that reviewer token for issue
maintenance, pushes, or merges. Load only the needed token for the
command and never expose its value. Authentication or a review does not
replace the owner's explicit merge approval; the existing self-review
restrictions still apply. See `docs/playbooks/git-and-review-incidents.md`
for the current procedure and clearly superseded historical rules.
Obsidian operations use the configured MCP, as specified in `AGENTS.md`
and documentation governance — that default is unaffected by the GitHub
REST-only change above.

## Reviews must be full-context, not diff-only (MANDATORY, 2026-09-15)

Any code review in this repo — Grok review, Claude's `/code-review`, a
Codex review, or a manual one — must read current governing docs/issue/
comments and trace the full caller/writer/reader/schema/transaction chain
of changed code, not just the rendered diff; separate a PR-introduced
defect from pre-existing surrounding behavior; and test cross-boundary
behavior (idempotent re-runs, empty input, rollback, a real dependency
where risk warrants it) rather than unit-level checks alone. Full
procedure/checklist: `docs/playbooks/full-context-review.md`. Review
approval never authorizes merge, deployment, or a production data
operation on its own.

## Design sessions do not implement (MANDATORY, 2026-09-15)

A session whose task was writing/authoring an issue or design doc must not
pivot into implementing code in the same session — not directly, not via a
subagent — unless the product owner explicitly asks it to in that session.
A copy correction, a question, or an instruction to file an issue is not
that ask. Do not open a task worktree until it is. (2026-10-14, #763.)
Applies to every agent/harness working in this repo (Claude Code, Grok CLI,
Codex, or otherwise), not just one. A design session ends by handing off a
self-contained prompt (see any issue's own comments for what "implementable
without this conversation's context" looks like) to a separate
implementation session — it does not ask "want me to implement this now?"
and does not start writing code on its own initiative.

## Conventional Commits (MANDATORY)

Format: `<type>(<scope>): <description>`

| Type | Version bump | Use for |
|------|--------------|---------|
| `feat:` | MINOR | new feature |
| `fix:` | PATCH | bug fix |
| `perf:` | PATCH | performance |
| `feat!:` | MAJOR | breaking change |
| `docs:`, `style:`, `refactor:`, `test:`, `chore:`, `ci:`, `build:` | none | non-release |

Examples:
- `feat(reports): add cross-market FX-normalized valuation`
- `fix(ingest): handle yfinance rate-limit on HK tickers`
- `docs: clarify layer-3 boundary in prompt template`

## Releases

Releases are semantic-versioned but **manually triggered** (issue #250 built
it, issue #283 changed the trigger — `.github/workflows/release.yml`,
`semantic-release` + `.releaserc.json`). **Never** bump versions or create
tags by hand. The workflow runs on `workflow_dispatch` only, not on every
push to `main` — issue #283: every merged PR pushing to `main` was firing
its own release, producing one tag per PR regardless of how those changes
actually reach production (deploy is a separate, manual, batched step — see
`docs/deployment.md`). The product owner runs it once per deploy batch
(Actions tab or `gh workflow run release.yml`); `semantic-release` still
walks every commit since the last tag on `main` (`.releaserc.json`'s
`branches: ["main"]`), so nothing is dropped from the changelog regardless
of the gap between runs. When it runs, it derives the next version from
commit types since the last tag (`feat`->minor, `fix`/`perf`->patch,
`feat!`->major; everything else = no release) and publishes a git tag +
GitHub Release. The Release notes ARE the changelog — there is no committed
`CHANGELOG.md` and nothing pushes back to `main` (deliberately: see PR #254
review — a changelog-commit plugin would have been a direct write to
`main`, contradicting the Branching rule below). This is release-only: it
does not run lint/type/test first, so the local quality gate stays the
pre-push responsibility documented in the CI-First Protocol.

## Code Standards

- **Python**: 3.11+, FastAPI, Pydantic v2, type hints required, `ruff` for
  lint + format, `mypy --strict` for types. No `Any` without justification.
- **TypeScript**: strict mode on. No `any`, no `!` non-null assertions, no
  unused locals/params (prefix with `_` only if intentionally unused).
- **No emojis in CLI output or server logs.** Use ASCII markers (`[OK]`,
  `[!]`, `[ERR]`, `[i]`). Emojis are fine in product UI copy and reports.
- Respect `NO_COLOR` for any terminal output.
- **Frontend contrast floor**: text at least 4.5:1 and non-text marks (chart
  series, focus rings, borders that carry meaning) at least 3:1, measured
  against the actual dark surface they sit on. Decline a review request that
  would go below the floor and record the ratio (see the full-context review
  playbook's "Recording review outcomes in the PR").
- Boundary validation: validate at system boundaries (HTTP handlers, file
  loaders, external API responses). Do not re-validate inside internal
  function chains — trust your types.

## Tests

- Unit tests live next to the code they cover.
- Integration tests hit a real Postgres (Homebrew Postgres 16 locally, not a
  mock). The whole point is to catch schema/migration drift.
- **Test DB isolation (issues #26/#27, PR #137)**: `session_test_db` creates
  `TEST_DATABASE_NAME` and migrates to head once per pytest session;
  `db_session` opens an outer transaction + SAVEPOINT; `alembic_cfg` uses a
  separate `MIGRATION_DB_NAME` so the revision walk can't drop the session
  DB; `SessionLocal` raises under pytest if `DB_NAME` isn't
  `TEST_DATABASE_NAME`. Both DB names are PID-suffixed (issue #152), not
  fixed strings — concurrent worktree test runs against the same local
  Postgres would otherwise drop each other's database mid-run. *Full
  reasoning: playbook `docs/playbooks/testing-notes.md`.*
- LLM prompt regressions: keep a small fixture of "input portfolio + expected
  shape of output" so prompt edits don't silently violate the layer-3 rule.
- Never let tests touch the developer's real home directory.
- **`caplog` sees nothing after the session migrate**: `alembic/env.py`'s
  `fileConfig()` disables any logger already instantiated before that
  point. Fix: `logging.getLogger("your.module").disabled = False` right
  before `caplog.at_level(...)`, scoped to the test file. *Full mechanism:
  playbook `docs/playbooks/testing-notes.md`.*

## Documentation

- `README.md` — short, user-facing intro, install, run.
- `docs/` — architecture, ADRs, runbook snippets. All English.
- [Documentation governance](docs/playbooks/documentation-governance.md)
  holds the project design-authoring and document-maintenance contract;
  `AGENTS.md` indexes it. Do not mirror this guidance into companion
  Obsidian configuration/design notes.
- Creating an Obsidian file requires the user's explicit authorization
  for that file. Updating documentation does not authorize creating a
  missing note or automatically synchronizing unrelated notes. Follow
  the authorized existing-document scope and the AGENTS.md rules.
- Update docs **in the same PR** as the code change that motivates them.
- API-level changes update `--help` text / OpenAPI schema / route docs in
  the same PR. Code and docs out of sync is a defect.

## Out of Scope (do not let scope creep pull this in)

Full product-scope decisions (what we deliberately don't build, and why)
live in Obsidian `Hermes/Portfonia/Portfonia Concept & Design.md` §1 + appendix — not
here, to keep this file to AI-actionable conventions rather than product
ideation. Quick check before any new feature: trade execution, tax/P&L
tracking, options/derivatives, price-only threshold alerts, social/sharing
features, and stock-pick-style recommendations are all explicitly excluded.

## When Principles Conflict

- **Compliance > everything**. If a feature can't be shipped without crossing
  the layer-3 boundary, the feature does not ship.
- **UX > YAGNI** for user-facing surfaces. If users need it, it's not
  speculative.
- **KISS applies to code AND user journey** — fewer steps, fewer options,
  fewer modes by default.
- **Match engineering investment to this project's actual scale, not to
  enterprise-SaaS reflexes** (product owner, 2026-09-14, after an
  isolated-service architecture for a since-removed feature — separate
  DB/Redis/Celery/frontend/domain across #451/#452/#453/#470 — turned a
  login into a cross-origin popup handshake and got reversed same-day, see
  #473). Portfolio-risk-tier /
  blast-radius-containment language is not a free justification for
  splitting a feature into its own isolated service, its own database, or
  its own auth handshake — this project has no per-seat enterprise
  contract, no compliance mandate, and no team of operators to carry that
  complexity. Before proposing or accepting an isolation/defense-in-depth
  design, check it against what this product and its actual usage
  actually need, not against what a well-funded SaaS would do. Default to
  the simplest thing that satisfies the real requirement; earn the extra
  layer with a concrete, current threat or constraint, not a generic
  "this is how it's normally done."
- **This file constrains the agent — any LLM/agent/harness working in
  this repo (Claude, Grok, Codex, or otherwise) — never the product
  owner** (generalized 2026-09-14 from the force-push case below to
  every rule in this document, not just git). For any action — risky,
  destructive, irreversible, or otherwise — the agent may state the
  concrete risk and recommend an alternative, but never frames it as the
  project "forbidding" or "disallowing" the owner from doing something,
  as if a written rule here could bind the owner's own decision. Only the
  owner tells the agent not to do something; no agent gets to invoke this
  file as authority over the owner.
- **Reversibility check before destructive actions** (DB migrations
  dropping columns, `rm -rf`, force pushes): state the risk, then act on
  the owner's explicit, real-time instruction — don't refuse by citing
  this file. **Precedent (2026-09-14, #473 retraction)**: the
  owner directed a hard reset + force-push of `main` back to a specific
  prior commit to retract a fully-merged, self-contained feature to a
  clean pre-feature state, in preference to a revert-commit chain that
  would keep the retracted code's bulk permanently in `main`'s diff/blame
  history. Before executing, the agent still verifies no other active
  branch depends on the commits being dropped (`git branch -r --contains
  <sha>`) and says so. Any PR/worktree still branched from the old tip
  then needs
  `git rebase --onto <new-main> <old-branch-point> <branch>`, not a plain
  `git rebase <new-main>` (a no-op when the old tip is already an
  ancestor of the branch).
