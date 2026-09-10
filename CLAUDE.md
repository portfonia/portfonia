# Portfonia — Agent Guidelines

AI-facing guidance for agent tooling working in this repository.
Last updated: 2026-09-03 (trimmed for size — see `docs/playbooks/`; mechanism
deep-dives referenced from the table below live in `docs/mechanisms/`).

**Playbook files** (situational detail moved out of this file to keep it a
rulebook, not a diary — read the linked file when its topic comes up, not
proactively):
- `docs/playbooks/llm-and-data-handling.md` — full LLM model-routing
  reasoning, the two-pass isolation / BYOK exception provenance.
- `docs/playbooks/regression-notes.md` — full incident history for every
  "known-fixed bug" one-liner.
- `docs/playbooks/git-and-review-incidents.md` — stacked-branch recovery
  steps, the two-GitHub-identity boundary's full history **including a
  2026-08-28/2026-08-30 correction that changes the current rule** (read
  this one before assuming the inline summary alone is complete), the
  production-infra-leak incident.
- `docs/playbooks/testing-notes.md` — test DB isolation/PID-suffix
  reasoning, the `caplog`-after-migrate mechanism.

## Where to find current state

This file holds **conventions and mechanisms**, not a project status board.

- **Open bugs, requests, and technical debt**: GitHub issues, `debt` /
  `bug` / `enhancement` labels (see "Issue Tracking" below — everything new
  gets an issue first).
- **Ring stage, recent session summaries, running progress**: Obsidian
  `Hermes/Portfonia/` project log.
- **Vigil product and Ring 0 contracts**: Obsidian
  `Hermes/Portfonia/Vigil Concept & Design.md` and
  `Hermes/Portfonia/Vigil_R0_Dev.md`. Track reusable Portfonia capabilities
  separately from Vigil implementation and acceptance evidence; follow
  `AGENTS.md` for documentation boundaries.
- **Build/test status, HEAD commit**: `git log`, `pytest -q`, `mypy .` —
  always run these rather than trusting a written-down snapshot.

## System conventions (current behavior, not status)

| Item | Value |
|------|-------|
| LLM model | OpenRouter, split by call shape (issue #78). Structured/JSON (holdings parsing) = `STRUCTURED_LLM_MODEL` (`openai/gpt-5.6-luna`, `reasoning_effort=none`, `data_collection=deny`). Unstructured/free-text (Pass 1 search-query gen + translation render) = `LOW_COST_LLM_MODEL` (`~deepseek/deepseek-v4-flash-latest`, OpenRouter BYOK to DeepSeek direct, `enforce_data_collection=False` + `allow_fallbacks=False` — scoped compliance exception, these two call sites only). PRIMARY (Pass 2 + regenerate) = `deepseek/deepseek-v4-pro`, `data_collection=deny`, no BYOK — never `anthropic/*` here (too expensive; config drift if seen). *Full reasoning (why gemma was dropped, why the BYOK exception, the `allow_fallbacks` pairing) in playbook `docs/playbooks/llm-and-data-handling.md`.* |
| Infrastructure | Homebrew PostgreSQL@16 + Redis (native, not Docker); `make infra-up` not needed |
| **App runtime retired locally (2026-08-10)** | No local uvicorn/celery worker/celery beat/Next.js dev server anymore — running the app for manual verification happens only via production deploy (see Three-layer deployment flow below). Homebrew Postgres/Redis stay running locally, but only as backing services for `pytest` (real-Postgres integration tests per the Tests section) — never as targets for a locally-running app process. Do **not** start `uvicorn`/`celery worker`/`celery beat`/`next dev` on this machine; if a task needs to be seen working, that means deploying to production, not spinning up a local server. The old "kill and restart uvicorn/celery after any model/migration/router change" drill no longer applies — there is no long-lived local process to go stale. |
| Output language | reason in EN, render in `output_lang` via a translation pass with a fixed-term glossary — locale-keyed, single source of truth in `backend/config/i18n_glossary.yml` (`report_glossary`/`forbidden_renderings`; only `zh-Hans` populated today, schema reserves `zh-Hant`/`fr`/`es` for later); `en` = no-op. **Per-user since issue #308**: self-service generate/regenerate and the scheduled fan-out read the requesting/recipient user's own `users.locale` (`VALID_REPORT_LANGUAGES = ("en", "zh")`, self-service `PATCH /me/report-language`, Ops `POST /admin/users/by-email/report-language`) — `Settings.OUTPUT_LANG` (default `zh`) is now only the fallback for a missing principal row and the value the admin manual-generate endpoint still deliberately uses unchanged. |
| Report statuses | `success` · `skipped` (quiet day, still emails heartbeat — EXCEPT a short manual quiet window: `session_node="manual"` + <2h span + 0 news + 0 anomalies suppresses the heartbeat as a same-day re-run artifact) · `needs_review` (compliance scan hit, NOT emailed) · `failed` · `in_progress` |
| Report title / email subject | `Portfonia <Financial Analysis Report> — YYYY-MM-DD HH:MM ET` (title timestamp from `period_end`; the zh-Hans render substitutes the `report_glossary` term for "Portfonia Financial Analysis Report" from `i18n_glossary.yml`); no "Intelligence" wording, or its zh-Hans equivalent (`forbidden_renderings` in the same file), anywhere. |
| Holdings model | `market` + `broker` are user-declared fields; `position` preserves book order. **§1 groups by `broker` (rendered as "Custodian" — zh-Hans term in `i18n_glossary.yml`'s `report_glossary`)** in that order with per-institution subtotals; cash sits inside its institution, broker-less rows fall into "Other". `position` is set on confirm (append = max+1, replace = 0..n), single-row `POST /holdings`, and `PATCH /holdings/reorder`. |
| Holdings upload | Async, not a single blocking request — see "Async holdings upload" section below (issue #77/#82/#85). |
| Re-render | `regenerate_report(mode=render\|analyze)` rebuilds from stored `report_inputs` without re-fetching; `POST /reports/{id}/regenerate`. render = token-free, analyze = Pass 2 only. |
| §1 / distribution / §4.1 classification dimension | **`asset_class`** (geography-first taxonomy — see table below), not `sector` or `asset_type`. `sector` (yfinance GICS) is retained ONLY for forward-event holding-relevance mapping (rate-sensitive/consumer sectors for FOMC/CPI events) — never reintroduce it into §1/distribution/§4.1. `by_asset_class` has no "Other" fallback (every `Holding` always has one, default `STOCK`). |
| Tests must mock external notify calls | `send_ops_alert`, `create_bug_report`, `send_report_email` are mocked via an **autouse** fixture in `app/tests/conftest.py` (`_no_external_notifications`); `send_admin_alert_task.delay` is stubbed by `_rate_limit_memory` so a 429 cannot enqueue real Celery work. Never rely on individual tests remembering to patch them. A gap here previously sent 42 real "FX rates stale" emails to the admin inbox from three same-day pytest runs (test clock fixed to a historical date that always trips the staleness check against the real current date). |
| Identity (B4, issue #129, PR #183) | Request identity is `Depends(current_principal)` only: `Authorization: Bearer` verified against hosted Auth JWKS (ES256/RS256; **no `JWT_SECRET` Settings field**), then `users.auth_subject` + `status=active`. Missing/forged/unknown-`sub` → 401, **no auto-insert**. `get_current_user_id()` raises. Ops `/admin/*` is a separate bearer (`ADMIN_API_TOKEN`), not this JWT. **Do not deploy this cutover without B5** — unauthenticated `/holdings` `/reports` `/portfolio` now 401. |

### Mechanism deep-dives

Each entry below is the full implementation record (root cause, design
tradeoffs, review provenance) for one system, filed under `docs/mechanisms/`.
This table is the pointer index — read the linked file before touching that
area of the code, not just the one-line summary here.

- [Frontend chrome (header/nav) convention](docs/mechanisms/frontend-chrome.md) — issue #146/#148 (shared `SiteHeader`); #214 session re-verification; #209 global i18n catalog; #220 Profile menu entry; #269 Profile section reorder; #390 Holdings+Report management merge; #350 item 4 LocaleSwitcher rebuild.
- [Profile page: GET /me account summary](docs/mechanisms/identity-and-auth.md) — issue #220/#221: `/profile` summary + full `GET /me` shape; #269 adds verification timestamps.
- [Post-signup onboarding](docs/mechanisms/frontend-chrome.md) — issue #221: ToS gate, `/questionnaire?onboarding=1` → `/welcome` flow; #280 reordered to questionnaire→holdings→welcome; #290 send-stop copy lift.
- [Async holdings upload](docs/mechanisms/holdings-pipeline.md) — issue #77/#82/#85: `POST /holdings/upload` returns 202 + job id, Celery parses, 45s SLA, two-layer hard-kill resolution.
- [Holdings encryption at rest](docs/mechanisms/holdings-pipeline.md) — issue #31: field-level Fernet via SQLAlchemy `TypeDecorator`, system-wide key, `ORDER BY` moved to Python.
- [Holdings domain CHECK constraints](docs/mechanisms/holdings-pipeline.md) — issue #25: DB-level CHECKs on `pricing_mode`/`asset_type`/`currency`/`asset_class`, naming-convention gotcha.
- [Postgres backup to OCI Object Storage](docs/mechanisms/backup-and-ops.md) — issue #106/#76: daily `pg_dump` -> OCI, instance-principal auth, two real restore drills.
- [Cash/wmf holdings exclusion fix](docs/mechanisms/holdings-pipeline.md) — issue #120/PR #121: structured-extraction model put cash amounts in the wrong field; two-layer fix.
- [Accounts table + user_id FKs](docs/mechanisms/holdings-pipeline.md) — issue #129 checkpoint B7: `accounts` table + `holdings.account_id`, real `user_id` FKs on `holdings`/`reports`/`upload_jobs`/`news_surfaced`.
- [Holdings single-row CRUD + confirm modes](docs/mechanisms/holdings-pipeline.md) — issue #92/#130 C1, PR #310: `POST`/`PATCH`/`DELETE /holdings/{id}`, `PATCH /holdings/reorder`, `POST /holdings/confirm?mode=append|replace`, export/template dialect; #319/#321 UX follow-ups; #323/#327 replace-hint callout.
- [Portfolio overview dashboard — C2](docs/mechanisms/holdings-pipeline.md) — issue #320/#130 C2, PR #322: `/portfolio` reads `GET /portfolio/summary`; `by_group`/`by_broker` aggregates; P&L only for priced `auto` holdings.
- [Portfolio dashboard: by-account breakdown, currency display modes, sector removal](docs/mechanisms/holdings-pipeline.md) — issue #330, PR #332: `by_broker`/`by_account` split, currency display toggle (later replaced by #350), by-sector card removed from UI.
- [Currency & language cleanup batch — per-user currency, by-currency card, cash P&L, as-of banner](docs/mechanisms/holdings-pipeline.md) — issue #350 items 1/2/5/6: per-user `base_currency`, unified by-currency card row, cash dilutes cost basis not P&L%, as-of banner per-market copy.
- [Portfolio snapshot export: xlsx and md](docs/mechanisms/holdings-pipeline.md) — issue #331, PR #335: `GET /portfolio/export?format=xlsx|md`, computed/priced rows (not the re-import dialect).
- [Portfolio overview email — explicit send button](docs/mechanisms/holdings-pipeline.md) — issue #202, PR #329: `POST /portfolio/send-overview`, 15-min per-user cooldown, no `reports` row/LLM call.
- [FX currency coverage + ticker-normalization consistency](docs/mechanisms/capture-and-reporting.md) — issue #204/PR #253: FX pairs widened to all 14 `VALID_CURRENCIES`; ticker-collision override table; #351/#352 follow-up fixes.
- [Shared instrument symbol normalization — instrument_symbols](docs/mechanisms/instrument-symbols.md) — issue #57 (PRs #359/#361/57-3): consolidates ticker/market normalization into one module, removes the `_yfinance` forwarding shim; issue #417 removes the hardcoded PSH ticker-collision override — no ticker gets per-ticker special treatment.
- [Portfolio Performance — Phase 1 + tracking-start + holiday history + param-limit fixes](docs/mechanisms/portfolio-performance.md) — issue #360/#382/#383/#366/#377/#398/#402/#403/#406/#407: snapshot tables, approximate EOD TWR, `tracking_start` correction (#366), benchmark/FX multi-year seed scripts, both scripts' 65535-param chunking fix, #406 USDCNH history (Twelve Data, closed), #407 csi300 Tencent kline fallback when yfinance drops dates.
- [Snapshot capture durability: freeze-then-publish + outbox replay](docs/mechanisms/portfolio-performance.md) — issue #373: per-(user, day) `portfolio_snapshot_outbox`, batch-level transaction boundary decision, `POST /admin/portfolio/snapshots/recover`, guarded catch-up recompute (`CATCHUP_LOOKBACK_DAYS`), outbox retention.
- [Per-pair independent FX rate resolution + fx_rates_as_of](docs/mechanisms/capture-and-reporting.md) — issue #354: per-pair `fx_rates_as_of`, `FxAsOfBanner`, currency-switcher rebuild on `MenuDropdown`.
- [Multi-market capture: UK/Europe/Japan/Korea + capture_supported](docs/mechanisms/capture-and-reporting.md) — issue #311/PR #312 (+#313/#314/#316/#318): capture widened to 7 markets, `Holding.capture_supported` boolean, GBX detection generalized.
- [Price-fetch resilience: typed errors, telemetry, Finnhub + Massive.com fallbacks](docs/mechanisms/capture-and-reporting.md) — issue #56: `PriceFetchErrorCode` classification, Finnhub spot + Massive.com EOD US-only fallbacks, both fail-open per ticker.
- [Fund NAV realtime path: Sina Finance fallback](docs/mechanisms/capture-and-reporting.md) — issue #20: Tiantian Fund's realtime endpoint blocked in production, Sina fallback added.
- [Fund NAV staleness observability](docs/mechanisms/capture-and-reporting.md) — issue #298/PR #303: per-fund stale/missing NAV WARNING + ops alert, durable Redis dedup; terminal send moved after retry/fallback in #389.
- [Bounded China NAV/ETF capture fallback](docs/mechanisms/capture-and-reporting.md) — issue #389: swallowed-empty retry, Eastmoney→Sina latest NAV, Yahoo→Tencent ETF gaps with adjustment admission; #135 closed as superseded, scheduler root cause still unconfirmed.
- [Capture layer + incremental reporting](docs/mechanisms/capture-and-reporting.md) — ADR-002: capture nodes, report window, multi-user fan-out (Ring 1 A1).
- [Per-user report cadence (mwf/weekly)](docs/mechanisms/capture-and-reporting.md) — issue #191: per-cadence Beat rows + `active_user_ids()` fan-out, `users.report_cadence` CheckConstraint, Ops cadence endpoint.
- [Per-user report language](docs/mechanisms/capture-and-reporting.md) — issue #308: `users.locale` CheckConstraint drives `output_lang` for self-service + scheduled fan-out; `Settings.OUTPUT_LANG` now fallback-only.
- [On-demand report generation is async](docs/mechanisms/capture-and-reporting.md) — issue #193: `POST /reports/generate` returns 202 + a pollable `report_jobs` row (`GET /reports/jobs/{job_id}`); Celery `generate_report_job` calls the existing pipeline; `/admin/.../reports/generate` stays synchronous.
- [L2 shared macro-event cache](docs/mechanisms/capture-and-reporting.md) — Ring 1 stage A3, issue #128: per-event-key cache, two daily budgets.
- [Personalized assembly + fan-out budget fairness](docs/mechanisms/capture-and-reporting.md) — Ring 1 stage A4, issue #128: `report_assembly.py`, `shared_budget.py` fair-share allocation.
- [L3 day-level cross-name synthesis](docs/mechanisms/capture-and-reporting.md) — Ring 1 quality gate, issue #128/PR #167: cross-name mechanism clusters, leak-prevention shape.
- [Narrative-layer redesign: Pass 2 material widening](docs/mechanisms/capture-and-reporting.md) — Ring 1 quality gate, issue #128/PR #168: material sharing not narrative sharing.
- [System default analysis framework — B1](docs/mechanisms/identity-and-auth.md) — Ring 1 stage B, issue #129/PR #172: `config/analysis_framework.yml`, injection order, §2 rewrite.
- [Identity seam: current_principal + explicit user_id — B3](docs/mechanisms/identity-and-auth.md) — Ring 1 stage B, issue #129/PR #181.
- [Users, invites, and JWKS auth — B4](docs/mechanisms/identity-and-auth.md) — Ring 1 stage B, issue #129/PR #183: JWKS verification, no `JWT_SECRET`, invite redeem.
- [Idle-timeout server enforcement](docs/mechanisms/identity-and-auth.md) — issue #235, PR #240 (3 review rounds): Redis-backed idle check on `current_principal`, fail-open on Redis outage.
- [Ops user hard-purge](docs/mechanisms/identity-and-auth.md) — issue #199/#225/B7 (`DELETE /admin/users/{id}`) + #274/#275 (by-email sibling): hard-deletes local rows + Supabase Auth account, handles Auth-only orphans.
- [Ops user directory read](docs/mechanisms/identity-and-auth.md) — issue #278/PR #285: read-only `GET /admin/users` filter/list, built to satisfy delete-by-email's pre-delete confirmation policy.
- [Generic email verification: core mechanism + Ops API](docs/mechanisms/email-verification.md) — issue #260/#261 core mechanism + Ops API; #262/#263 signup hook; #257/#279 unsubscribe; #276/#288 verified-recipient gating; #289/#292 and #290/#294 follow-ups.
- [Signup / invite anti-abuse](docs/mechanisms/identity-and-auth.md) — issue #190: Redis fixed-window limits on signup/invite-mint, fail-closed, no Turnstile.
- [Forgot-password trigger](docs/mechanisms/identity-and-auth.md) — issue #231: backend-mediated Supabase reset trigger, self-hosted Altcha PoW, exists/not-exists response.
- [Frontend auth closure — B5](docs/mechanisms/identity-and-auth.md) — Ring 1 stage B, issue #129: `/login`+`/signup`, `src/proxy.ts`, cookie session via `@supabase/ssr`.
- [Investment-style questionnaire — B6](docs/mechanisms/identity-and-auth.md) — Ring 1 stage B, issue #129: `user_investment_context`, 3-layer enum validation, `/questionnaire` wizard.
- [Macro keyword theme pool](docs/mechanisms/macro-keywords.md) — issue #129 B1 + issue #175: widened to 17 themes; bare single-word keywords false-fire, always qualify.
- [News dedup ledger](docs/mechanisms/news-dedup.md) — issue #30: `news_surfaced` ledger closes the window-boundary permanent-miss gap; per-user uniqueness.
- [`report_generator.py` module split](docs/mechanisms/report-generator-refactor.md) — issue #37: pure refactor into `report_context`/`report_llm`/`report_serializers`/etc.
- [Report content features](docs/mechanisms/report-content-and-email.md) — Ring0 #1-4 + R-3/R-5/R-6/R-7/R-8: §4.2 anomaly table, confidence labels, §4.4 technical position, §2.5 forward calendar.
- [Report email HTML rendering](docs/mechanisms/report-content-and-email.md) — issue #24/#117: inline styles, bulletproof wrapper table; #257/#279 added text alternative + List-Unsubscribe headers.
- [Report footer disclaimer: single-language, not always bilingual](docs/mechanisms/report-content-and-email.md) — issue #350 item 3: footer disclaimer renders only the report's own `output_lang`, not always EN+zh-Hans.
- [LLM failure taxonomy](docs/mechanisms/llm-reliability.md) — issue #55: `LLMErrorCode`/`ErrorPolicy`, classification by HTTP status, five real defects fixed.
- [Bounded retry for shared intel caches](docs/mechanisms/llm-reliability.md) — issue #160: `attempt_count` bounds L1/L2 retries instead of a permanent null-marker lock.
- [Reliability mechanisms (window/dedup/LLM-call correctness)](docs/mechanisms/llm-reliability.md) — same-day windows, Pass 2 completeness guard, `_call_llm` retry/backoff; issue #61 resumable retry from stored raw LLM output.
- [Compliance + ops alerting](docs/mechanisms/compliance-and-classification.md) — forbidden-vocab scan, disclaimer, `send_ops_alert`, GitHub issue auto-creation.
- [Asset classification + fund NAV capture](docs/mechanisms/compliance-and-classification.md) — `asset_class` economic-exposure dimension, `ticker_themes`, fund NAV via lsjz.
- [§1 / distribution / §4.1 read `asset_class`, not sector](docs/mechanisms/compliance-and-classification.md) — 2026-06-19: switched from `sector`/`asset_type`, concentration threshold rules.
- [Asset_class thresholds are admin-configurable](docs/mechanisms/compliance-and-classification.md) — issue #35: `config/asset_class_thresholds.yml`, hot-reloaded, closed taxonomy.
- [Leveraged-product threshold multiplier](docs/mechanisms/compliance-and-classification.md) — issue #87: `ticker_leverage_overrides` table widens anomaly thresholds and tightens §4.1 concentration thresholds by `leverage_multiple`.

## Language Policy (MANDATORY)

- **All repository content is English**: code, identifiers, comments, commit
  messages, PR descriptions, issue text, README, `docs/`, ADRs, tests.
  Repository filenames and documentation indexes must also be English;
  do not embed Chinese note titles or paths in repository documentation.
- **In-product strings are i18n-keyed** and shipped through the translation
  layer, never hardcoded in any single language. Runtime UI locales
  exposed to users: English, Simplified Chinese, and Traditional Chinese
  (issue #209 added the first two; issue #350 item 4 lifted Traditional
  Chinese's review gate — see `frontend/src/locales/README.md` for adding
  a fourth, and its "zh-Hant review status" section for the gate-lift
  history). `zh-Hant.json`'s catalog was originally LLM-drafted with no
  native-speaker review (blacktomb42 review, PR #226 round 2: an earlier
  version of this line overclaimed it as supported while still gated) —
  the product owner explicitly chose to ship it anyway rather than wait
  for that review; this is a deliberate, logged decision, not an
  oversight to "fix" by re-gating it. Report output languages are
  separate and narrower: report translation
  only covers the bare codes `en`/`zh` — not `zh-Hans`, the frontend
  catalog's BCP-47 tag; `i18n_glossary.yml`'s locale keys are the one place
  `zh-Hans` legitimately appears on this side of the boundary (see the
  bare-code/BCP-47 distinction two paragraphs below). Driven per-user by
  `users.locale` since issue #308 (`VALID_REPORT_LANGUAGES = ("en", "zh")`)
  — `Settings.OUTPUT_LANG` is now only the fallback default, not the
  source of truth (see System conventions table below). A UI locale is not
  a report language.
- Translation resources live under a dedicated locales directory
  (`frontend/src/locales/*.json` for UI chrome; `backend/config/
  i18n_glossary.yml` for report output; `_VERIFICATION_EMAIL_COPY` in
  `backend/app/services/email_sender.py` for the one transactional
  verification email, issue #260/PR #261; `_RULES_ZH` / `_EXAMPLES_ZH` in
  `backend/app/services/holdings_export.py` for the holdings
  export/template dialect keyed off `users.locale`, issue #92/PR #310 —
  four mechanisms, not one/two/three, see the Mechanism deep-dives table)
  and are the only places where non-English text legitimately appears in
  the repo. A lint rule (`i18next/no-literal-string` in
  `frontend/eslint.config.mjs`) enforces this for UI code.
  `_VERIFICATION_EMAIL_COPY` is deliberately its own small dict rather
  than folded into either existing mechanism: the next-intl catalog is
  browser-only and unreachable from this backend module, and
  `i18n_glossary.yml` is built for large LLM-generated report bodies, not
  a two-line transactional email — bare locale codes (`en`/`zh`), matching
  `users.locale`/`OUTPUT_LANG`'s convention, not the frontend catalog's
  BCP-47 `zh-Hans` tag. The holdings export/template strings stay in
  `holdings_export.py` for the same reason: they are a downloaded file
  dialect, not report glossary terms — **since issue #319/PR #321**,
  `GET /holdings/export`/`GET /holdings/template` specifically (no other
  route) take an optional `locale` query param sourced from the
  frontend's UI locale that overrides the `users.locale` (report
  language) fallback these two endpoints used before; report generation
  itself is untouched.

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
in any other language.

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
  mimics third-party syntax in the same clause may slip.
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
| Local dev | Homebrew PostgreSQL 16 + Redis (native), used only to back `pytest`'s real-Postgres tests — the app itself does not run locally anymore (see System conventions table). Colima for Hermes gateway only. |
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
- **Two-pass isolation (enforced):** Pass 1 (search-query generation, low-cost
  model) must carry only public data — macro themes + news headlines.
  Holdings-derived data, including **price anomalies**, belongs only in
  Pass 2. Regression locked by `test_pass1_prompt_excludes_holdings_derived_
  anomalies` and `test_generate_report_pass1_call_has_no_holdings`. Do not
  reintroduce holdings into `_build_pass1_prompt`.
- **`data_collection=deny` on every LLM call by default**, as defense in
  depth. **Exception (issue #78)**: Pass 1 search-query gen + translation
  render — both `LOW_COST_LLM_MODEL` — pass `enforce_data_collection=False`
  + `allow_fallbacks=False` (routed via OpenRouter BYOK straight to
  DeepSeek's first-party backend; a scoped, sign-off compliance tradeoff for
  these two call sites only). Never extend to another call site without the
  same sign-off, never drop the `allow_fallbacks=False` pairing. *Full
  reasoning: playbook `docs/playbooks/llm-and-data-handling.md`.*
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
```

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
- No `any` / `Any`, no non-null assertions, no unused exports.

**Gap this doesn't cover**: none of the above actually builds the Docker
images. A change that only breaks `docker build` (e.g. deleting the last
file in `frontend/public/` — see the regression note above) passes every
gate here and still fails at deploy time, silently, until someone actually
redeploys. When a change touches `frontend/public/`, either `Dockerfile`,
or `docker-compose.yml`, run a real `docker build`/`docker compose build`
before pushing — `bun run dev`/`next build` do not exercise the same path
and will not catch this class of bug.

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

**Two separate GitHub identities, don't mix them up** (actual accounts live
in `.env.local`, never committed — this file intentionally does not name
them): `GITHUB_TOKEN` is the primary write identity — repo owner, used for
commits/pushes, issue/PR creation, and merges. `GITHUB_REVIEWER_TOKEN`
(blacktomb42) is read + PR-review-only. Using it is never a substitute for
the product owner's own merge authorization, and its approval never comes
from self-review. *Full identity-boundary history, including a later
correction to who owns the blacktomb42 account and when it's appropriate
to use it: playbook `docs/playbooks/git-and-review-incidents.md` — read
that before assuming this paragraph alone is the current, complete rule.*

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
- **Reversibility check before destructive actions** (DB migrations dropping
  columns, `rm -rf`, force pushes). Confirm with the user before executing.
