# Portfonia — Agent Guidelines

AI-facing guidance for agent tooling working in this repository.
Last updated: 2026-08-25

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
| LLM model | OpenRouter, split by call shape (issue #78, 2026-08-06). **Structured/JSON** (holdings parsing, `holding_parser.py`, the only call site requiring schema-compliant output) = `STRUCTURED_LLM_MODEL` (`openai/gpt-5.6-luna` — moved off `google/gemma-4-31b-it` in issue #84, 2026-08-06: the gemma pin to OpenInference's bf16 endpoint was itself the latency bottleneck, 371s worst case on a 30-row holdings file; `gpt-5.6-luna` measured 10.9-13.8s on the same file with 30/30 rows correct on manual audit — one manual run, not yet a systematic eval), `reasoning_effort=none` (`_STRUCTURED_REASONING_EFFORT` in `holding_parser.py` — this model defaults reasoning to "medium", wasted cost/latency for mechanical extraction), open/unpinned provider selection for both of 2 identical attempts (`app/core/llm.py:structured_provider` — no precision-pin concern for this model, unlike gemma's third-party quantized resellers); `data_collection=deny` applies throughout. **Unstructured/free-text** (Pass 1 search-query gen, `report_prompts.py`/`report_generator.py` + translation render, `report_translation.py` — split from a single `report_generator.py` in issue #37) = `LOW_COST_LLM_MODEL` (`~deepseek/deepseek-v4-flash-latest` — leading `~` is OpenRouter's "-latest" alias convention), routed via OpenRouter BYOK straight to DeepSeek's own backend (`order=["DeepSeek"]`, module constant `_BYOK_PROVIDER_ORDER` in `report_llm.py`) with `enforce_data_collection=False` — a scoped compliance exception for these two calls only — **and `allow_fallbacks=False` (hard pin, no marketplace fallback)**: since `deny` is off for these calls, an open fallback on DeepSeek unavailability could silently reroute the (holdings-bearing, for translation) payload to a training-permitting provider `deny` would normally have excluded; the call must fail rather than degrade that guarantee (PR #79 review finding). Reasoning/thinking tokens are explicitly disabled (`disable_reasoning=True`) since this alias defaults reasoning on unlike the non-aliased model. **PRIMARY (Pass 2 analysis + regenerate) = `deepseek/deepseek-v4-pro`**, unchanged — provider=DigitalOcean,Venice, `data_collection=deny`, no BYOK. Sonnet/Anthropic models are NOT used here — too expensive (~$0.2/call); if `PRIMARY_LLM_MODEL` ever shows an `anthropic/*` value it is config drift, revert it. |
| Infrastructure | Homebrew PostgreSQL@16 + Redis (native, not Docker); `make infra-up` not needed |
| **App runtime retired locally (2026-08-10)** | No local uvicorn/celery worker/celery beat/Next.js dev server anymore — running the app for manual verification happens only via production deploy (see Three-layer deployment flow below). Homebrew Postgres/Redis stay running locally, but only as backing services for `pytest` (real-Postgres integration tests per the Tests section) — never as targets for a locally-running app process. Do **not** start `uvicorn`/`celery worker`/`celery beat`/`next dev` on this machine; if a task needs to be seen working, that means deploying to production, not spinning up a local server. The old "kill and restart uvicorn/celery after any model/migration/router change" drill no longer applies — there is no long-lived local process to go stale. |
| Output language | reason in EN, render in `OUTPUT_LANG` (Ring 0 default `zh`) via a translation pass with a fixed-term glossary — locale-keyed, single source of truth in `backend/config/i18n_glossary.yml` (`report_glossary`/`forbidden_renderings`; only `zh-Hans` populated today, schema reserves `zh-Hant`/`fr`/`es` for later); `en` = no-op |
| Report statuses | `success` · `skipped` (quiet day, still emails heartbeat — EXCEPT a short manual quiet window: `session_node="manual"` + <2h span + 0 news + 0 anomalies suppresses the heartbeat as a same-day re-run artifact) · `needs_review` (compliance scan hit, NOT emailed) · `failed` · `in_progress` |
| Report title / email subject | `Portfonia <Financial Analysis Report> — YYYY-MM-DD HH:MM ET` (title timestamp from `period_end`; the zh-Hans render substitutes the `report_glossary` term for "Portfonia Financial Analysis Report" from `i18n_glossary.yml`); no "Intelligence" wording, or its zh-Hans equivalent (`forbidden_renderings` in the same file), anywhere. |
| Holdings model | `market` + `broker` are user-declared fields; `position` preserves upload order. **§1 groups by `broker` (rendered as "Custodian" — zh-Hans term in `i18n_glossary.yml`'s `report_glossary`)** in upload order with per-institution subtotals; cash sits inside its institution, broker-less rows fall into "Other". `position` is populated automatically on confirm. |
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

- [Frontend chrome (header/nav) convention](docs/mechanisms/frontend-chrome.md) — issue #146/#148: one shared `SiteHeader`, auth-gated Get Started menu; issue #214 (PR #215): unconditional pathname-triggered session re-verification (no throttling — a grace window that shipped in PR #215 was reverted the same day), optimistic logout + login-pending placeholder, bounded `getUser()` timeout/retry, Home menu entry; issue #209: global next-intl message catalog (`frontend/src/locales/{en,zh-Hans,zh-Hant}.json`) replaces `home-messages.ts`/`messages.ts`, `lang` and the locale switcher are no longer home-only, no URL-based locale routing (product decision); issue #220 (PR #228): Home menu entry replaced by Profile (`/profile`), lucide-react icons on every menu entry, new `profile` locale namespace.
- [Profile page: GET /me account summary](docs/mechanisms/identity-and-auth.md) — issue #220/PR #228: `/profile` account email + change-password (Server Action verifies via the session's own email, never a client-submitted one) + investment-style link + delivery-email display + non-interactive placeholders; `GET /me` ships the full #221 response shape in this PR (`missing` never contains `"tos"`; the page did not render a gap card from it at #220 time — that landed with #221, see the next bullet).
- [Post-signup onboarding](docs/mechanisms/frontend-chrome.md) — issue #221: ToS gate (client checkbox + backend `Literal[True]`, both required), `signup` redirects to `/questionnaire?onboarding=1` (the only `mode="onboarding"` trigger), `mode` prop on `QuestionnaireForm`/`HoldingsManager` (no `/onboarding/*` route tree), new `/welcome` route with sessionStorage dedupe, Profile gap card reading `GET /me`'s `missing`, `report_cadence` defaults to `weekly`, admin manual-generate's no-holdings 422 removed (`active_user_ids()`/Beat untouched at the time — cadence follow-up landed separately as issue #191, see the entry below), and a real password-leak fix in the 422 secret-redaction handler that adding the required field exposed.
- [Async holdings upload](docs/mechanisms/holdings-pipeline.md) — issue #77/#82/#85: `POST /holdings/upload` returns 202 + job id, Celery parses, 45s SLA, two-layer hard-kill resolution.
- [Holdings encryption at rest](docs/mechanisms/holdings-pipeline.md) — issue #31: field-level Fernet via SQLAlchemy `TypeDecorator`, system-wide key, `ORDER BY` moved to Python.
- [Holdings domain CHECK constraints](docs/mechanisms/holdings-pipeline.md) — issue #25: DB-level CHECKs on `pricing_mode`/`asset_type`/`currency`/`asset_class`, naming-convention gotcha.
- [Postgres backup to OCI Object Storage](docs/mechanisms/backup-and-ops.md) — issue #106/#76: daily `pg_dump` -> OCI, instance-principal auth, two real restore drills.
- [Cash/wmf holdings exclusion fix](docs/mechanisms/holdings-pipeline.md) — issue #120/PR #121: structured-extraction model put cash amounts in the wrong field; two-layer fix.
- [Accounts table + user_id FKs](docs/mechanisms/holdings-pipeline.md) — issue #129 checkpoint B7: `accounts` table + `holdings.account_id` (additive, text columns unchanged), `holdings`/`reports`/`upload_jobs`/`news_surfaced` gain real `user_id` FKs (`ON DELETE RESTRICT`), SQLAlchemy `relationship()` needed purely for flush-order correctness.
- [FX currency coverage + ticker-normalization consistency](docs/mechanisms/capture-and-reporting.md) — issue #204/PR #253: `_CURRENCY_TO_FX_PAIR`/`fx_fetcher._PAIRS` widened from 3 to all 14 `VALID_CURRENCIES`; `_yfinance._normalize_ticker` composes a ticker-collision override table (`_TICKER_SYMBOL_OVERRIDE`) and a per-ticker price-scale table (`_TICKER_PRICE_SCALE`, GBX→GBP) with the existing HK normalizer; every consumer that derives a lookup/join key from a raw `Holding.ticker` must call it — full list of call sites and the 3 rounds of review findings that surfaced them in the mechanism doc. Deferred to #252: no scheduled capture at all for `market="Other"`.
- [Fund NAV realtime path: Sina Finance fallback](docs/mechanisms/capture-and-reporting.md) — issue #20: Tiantian Fund's realtime endpoint blocked in production, Sina fallback added.
- [Capture layer + incremental reporting](docs/mechanisms/capture-and-reporting.md) — ADR-002: capture nodes, report window, multi-user fan-out (Ring 1 A1).
- [Per-user report cadence (mwf/weekly)](docs/mechanisms/capture-and-reporting.md) — issue #191, cadence follow-up to #221 §8: `_REPORT_CADENCES` is a table of Beat rows each scoping its own `active_user_ids(session, cadence)` fan-out; `weekly` fires Saturday 19:00 ET (`session_node="weekend_snapshot"`), holdings gate loosened only for `weekly`; `users.report_cadence` gains a `CheckConstraint`; new Ops endpoint `POST /admin/users/{user_id}/cadence` (see `docs/mechanisms/admin-surface.md`).
- [L2 shared macro-event cache](docs/mechanisms/capture-and-reporting.md) — Ring 1 stage A3, issue #128: per-event-key cache, two daily budgets.
- [Personalized assembly + fan-out budget fairness](docs/mechanisms/capture-and-reporting.md) — Ring 1 stage A4, issue #128: `report_assembly.py`, `shared_budget.py` fair-share allocation.
- [L3 day-level cross-name synthesis](docs/mechanisms/capture-and-reporting.md) — Ring 1 quality gate, issue #128/PR #167: cross-name mechanism clusters, leak-prevention shape.
- [Narrative-layer redesign: Pass 2 material widening](docs/mechanisms/capture-and-reporting.md) — Ring 1 quality gate, issue #128/PR #168: material sharing not narrative sharing, large no-anomaly holdings get material too.
- [System default analysis framework — B1](docs/mechanisms/identity-and-auth.md) — Ring 1 stage B, issue #129/PR #172: `config/analysis_framework.yml`, injection order, v1->v2, §2 rewrite.
- [Identity seam: current_principal + explicit user_id — B3](docs/mechanisms/identity-and-auth.md) — Ring 1 stage B, issue #129/PR #181.
- [Users, invites, and JWKS auth — B4](docs/mechanisms/identity-and-auth.md) — Ring 1 stage B, issue #129/PR #183: JWKS verification, no `JWT_SECRET`, invite redeem.
- [Idle-timeout server enforcement](docs/mechanisms/identity-and-auth.md) — issue #235: the 15-min auto-logout (issue #207) was client-only and silently defeated by closing the browser; `current_principal` now checks a Redis record keyed by `(user_id, session_id)` (`app/core/idle_activity.py` — `session_id` is a required JWT claim as of round 3, not just a value comparison), fail-open on Redis outage (deliberate departure from rate_limit's fail-closed convention — blast radius, see mechanism doc); absolute session lifetime (Supabase refresh-token expiry) and per-user configurable length are explicitly out of scope here. PR #240 went through 3 review rounds, each catching a real flaw in the previous fix (see mechanism doc for the full history): round 1 — stale idle lock survived re-login, and the 401 never signed the browser out (8 frontend call sites now route through `logout()`); round 2 — round 1's re-login fix compared `iat`, which silent background token refresh also changes; round 3 — round 2's `session_id` comparison was a value check against a still user_id-keyed record, so a re-login's write could resurrect the JWT it superseded; fixed by keying Redis itself on `(user_id, session_id)` with no cross-session comparison logic left at all.
- [Ops user hard-purge](docs/mechanisms/identity-and-auth.md) — issue #199/#225/B7: `DELETE /admin/users/{id}?confirm={email}` hard-deletes one user's own rows (now including `accounts`) and the Supabase Auth account (sequenced before local deletes, 502+no-op on failure); also handles Auth-only orphans with no local row.
- [Signup / invite anti-abuse](docs/mechanisms/identity-and-auth.md) — issue #190: Redis fixed-window limits on `POST /auth/signup` and `POST /admin/invites`; no Turnstile; fail-closed on Redis; known-invite buckets only; Next.js hop forwards XFF; global invite-mint 200/day alert-only.
- [Forgot-password trigger](docs/mechanisms/identity-and-auth.md) — issue #231: `POST /auth/forgot-password` backend-mediated Supabase reset trigger, self-hosted Altcha PoW (no Sentinel, no external CDN — vendored `frontend/public/altcha.js`), IP+email Redis rate limit reusing issue #190's machinery, local-`users`-table exists/not-exists response (deliberate OWASP-enumeration deviation), `/reset-password` client-direct to Supabase (no PoW); link expiry (72h targeted, 24h actual — Supabase's Email OTP Expiration caps at 86400s) and "password changed" email are Supabase Dashboard config, not code.
- [Frontend auth closure — B5](docs/mechanisms/identity-and-auth.md) — Ring 1 stage B, issue #129: `/login`+`/signup`, `src/proxy.ts`, cookie session via `@supabase/ssr`.
- [Investment-style questionnaire — B6](docs/mechanisms/identity-and-auth.md) — Ring 1 stage B, issue #129: `user_investment_context`, 3-layer enum validation, all 8 questionnaire dimensions + `free_text` reach Pass 2 AND assembly behind a SCOPE guardrail (decision point 6, corrected 2026-08-25), `/questionnaire` wizard.
- [Macro keyword theme pool](docs/mechanisms/macro-keywords.md) — issue #129 B1 + issue #175: widened to 17 themes; bare single-word keywords false-fire, always qualify.
- [News dedup ledger](docs/mechanisms/news-dedup.md) — issue #30: `news_surfaced` ledger closes the window-boundary permanent-miss gap; per-user uniqueness.
- [`report_generator.py` module split](docs/mechanisms/report-generator-refactor.md) — issue #37: pure refactor into `report_context`/`report_llm`/`report_serializers`/etc.
- [Report content features](docs/mechanisms/report-content-and-email.md) — Ring0 #1-4 + R-3/R-5/R-6/R-7/R-8: §4.2 anomaly table, confidence labels, §4.4 technical position, §2.5 forward calendar.
- [Report email HTML rendering](docs/mechanisms/report-content-and-email.md) — issue #24/#117: inline styles from `_TAG_STYLES`, bulletproof wrapper table, zebra striping.
- [LLM failure taxonomy](docs/mechanisms/llm-reliability.md) — issue #55: `LLMErrorCode`/`ErrorPolicy`, classification by HTTP status, five real defects fixed.
- [Bounded retry for shared intel caches](docs/mechanisms/llm-reliability.md) — issue #160: `attempt_count` bounds L1/L2 retries instead of a permanent null-marker lock.
- [Reliability mechanisms (window/dedup/LLM-call correctness)](docs/mechanisms/llm-reliability.md) — same-day windows, Pass 2 completeness guard, `_call_llm` retry/backoff, `session_node`.
- [Compliance + ops alerting](docs/mechanisms/compliance-and-classification.md) — forbidden-vocab scan, disclaimer, `send_ops_alert`, GitHub issue auto-creation.
- [Asset classification + fund NAV capture](docs/mechanisms/compliance-and-classification.md) — `asset_class` economic-exposure dimension, `ticker_themes`, fund NAV via lsjz.
- [§1 / distribution / §4.1 read `asset_class`, not sector](docs/mechanisms/compliance-and-classification.md) — 2026-06-19: switched from `sector`/`asset_type`, concentration threshold rules.
- [Asset_class thresholds are admin-configurable](docs/mechanisms/compliance-and-classification.md) — issue #35: `config/asset_class_thresholds.yml`, hot-reloaded, closed taxonomy.

## Language Policy (MANDATORY)

- **All repository content is English**: code, identifiers, comments, commit
  messages, PR descriptions, issue text, README, `docs/`, ADRs, tests.
- **In-product strings are i18n-keyed** and shipped through the translation
  layer, never hardcoded in any single language. Runtime UI locales actually
  exposed to users: English, Simplified Chinese (issue #209 — see
  `frontend/src/locales/README.md` for adding a fourth). Traditional Chinese
  has a catalog (`zh-Hant.json`, structurally locked in sync with the other
  two) but is explicitly gated out of the switcher and out of
  `isLocale()`'s accepted values pending native-speaker review — it is not
  yet a supported locale, just a prepared one (blacktomb42 review, PR #226
  round 2: this line previously overclaimed it as supported while gated).
  Report output languages are separate and narrower: `OUTPUT_LANG` still
  only ships `en`/`zh-Hans` (see System conventions table below) — a UI
  locale is not a report language.
- Translation resources live under a dedicated locales directory
  (`frontend/src/locales/*.json` for UI chrome; `backend/config/
  i18n_glossary.yml` for report output — two mechanisms, not one, see the
  Mechanism deep-dives table) and are the only place where non-English text
  legitimately appears in the repo. A lint rule
  (`i18next/no-literal-string` in `frontend/eslint.config.mjs`) enforces
  this for UI code.

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
  Every report has fixed header + footer disclaimers (EN + zh-CN). AI fills
  only the body region.
- Prompt-level hard constraints (the layer-3 rule + vocabulary blacklist)
  are part of the system prompt for every report and Q&A flow. Do not move
  these constraints to user-tunable prompts.
- **Output-side backstop**: prompt instructions are not a guarantee, so the
  generated body is scanned post-generation (`_scan_forbidden_output`) for
  high-precision advisory phrases. A hit sets the report status to
  `needs_review` and **suppresses email** — content is preserved for
  inspection, never delivered. The scan covers the LLM body only, never the
  template footer (whose disclaimer legitimately contains "buy/sell").
- **Single footer disclaimer, no inline markers** (2026-06-08): the compliance
  base is the one bilingual disclaimer in the footer. The body carries NO
  per-sentence `[For information only…]` suffix and NO bracketed provenance tags
  (the legacy market-data/news/analysis marker tags stripped by
  `report_generator._STRAY_TAGS`, sourced from `i18n_glossary.yml`'s
  `legacy_removed_markers_zh`, or `[S#]`). The system prompt forbids the model from
  emitting them, and `_strip_markers` removes any that slip through. The scan
  backstop above does not depend on the suffix.

### Known-fixed bugs worth remembering (regression notes)

- **Fund NAV lookup**: `compute_portfolio` must look up price data with
  `captured_closes.get(h.ticker or h.fund_code or "")` — fund code-only
  holdings have no `ticker`, and `capture_fund_navs` stores NAV in
  `price_snapshots` keyed by `fund_code`. A ticker-only lookup silently drops
  every fund holding into `stale_tickers` and out of the portfolio. (issue #1)
- **Sector backfill on re-upload**: `confirm_holdings` must call
  `backfill_sectors()` after commit — re-uploading holdings clears all rows,
  and `sector` is otherwise only populated by `POST /admin/portfolio/refresh`
  (moved from `POST /portfolio/refresh`, removed, in issue #129 checkpoint B2)
  or the scheduled capture tasks.
- **Next.js Turbopack + multipart**: Turbopack's `rewrites()` fails on
  `multipart/form-data` POST (ECONNRESET at proxy). Upload routes need a real
  Next.js API Route (`route.ts`) that manually forwards to the backend.
- **`frontend/public/` must stay non-empty**: git doesn't track empty
  directories; `frontend/Dockerfile`'s runner stage does
  `COPY --from=builder /app/public ./public`, which fails hard if the
  directory doesn't exist in the build context at all. Keep at least one
  tracked file there (a `.gitkeep` is fine) even after removing every real
  asset. (issue #100/#101 — this landed with PR #93 and sat undetected on
  `main` through two more PRs, because production hadn't redeployed since;
  `npm run dev`/`next build` don't care about a missing `public/`, only the
  Docker multi-stage build does — see the Quality Gates gap noted below.)
- **Two frontend lockfiles must be regenerated together — MANDATORY, no
  exceptions, until issue #227 is actually fixed**: local dev/CI uses `bun`
  (`bun install`/`bun run lint`/`bun run typecheck`/`bun run test`), but
  `frontend/Dockerfile`'s `npm ci --legacy-peer-deps` reads
  `package-lock.json` — a completely separate lockfile that nothing in the
  local quality gate ever touches. Any change to `frontend/package.json`
  (add/remove/bump a dependency) MUST regenerate **both** lockfiles in the
  same commit — `bun install` for `bun.lock`, `npm install
  --package-lock-only --legacy-peer-deps` for `package-lock.json` — and be
  verified with a real `docker build ./frontend` before pushing (`bun run
  test` never exercises `npm ci` at all). This has silently drifted and
  broken a production deploy **three times** (self-caught before #227
  existed; 2026-08-27's #221 deploy; 2026-08-28's #231 deploy, PR #237 →
  fixed in #244) — "remember to sync two lock files" has now failed as a
  process three times in a row, so this step is not optional discretion,
  it is a required step on every PR that touches `frontend/package.json`.
  This entire dual-lockfile burden is a workaround, not the fix: it stays
  mandatory only **until the product owner resolves issue #227** (migrate
  the Dockerfile to `bun install --frozen-lockfile` since that's the
  lockfile actually kept current, or add a CI check that fails on lockfile
  mismatch) — once #227 lands, delete this bullet along with the
  now-unnecessary second lockfile.

## Architecture

| Layer | Choice |
|-------|--------|
| Frontend | Next.js + shadcn/ui |
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
  ops doc referenced from the deployment section below. (Incident:
  2026-08-06 — the production server's real IP, SSH user, remote path, cloud
  provider, and region sat in `CLAUDE.md` across 3 commits on this public
  repo for ~30 hours before being caught; history was rewritten and
  force-pushed to remove it, but that can't guarantee removal from caches,
  forks, or clones made in that window — treat anything like this as burned,
  not just hidden, once it's been pushed.)

## Data Handling

- User holdings are sensitive. Encrypt at rest. **Never** include raw user
  holdings in training data, LLM fine-tuning datasets, or third-party logs.
- When sending holdings to an external LLM, scope the payload to what the
  current report needs. Do not attach the full portfolio history "just in case".
- **Two-pass isolation (enforced):** Pass 1 (search-query generation, low-cost
  model) must carry only public data — macro themes + news headlines.
  Holdings-derived data, including **price anomalies** (their name/ticker
  reveals a position), belongs only in Pass 2. Regression locked by
  `test_pass1_prompt_excludes_holdings_derived_anomalies` and
  `test_generate_report_pass1_call_has_no_holdings`. Do not reintroduce
  holdings into `_build_pass1_prompt`.
- **`data_collection=deny` is applied to every LLM call by default** (not just
  holdings-bearing ones) as defense in depth: even if holdings leak into Pass 1
  in the future, the call still cannot route to training providers.
  **Exception (issue #78, 2026-08-06):** Pass 1 search-query generation and
  translation render — both on `LOW_COST_LLM_MODEL` — pass
  `enforce_data_collection=False` because they're routed via OpenRouter BYOK
  straight to DeepSeek's own first-party backend (`order=["DeepSeek"]`,
  `_BYOK_PROVIDER_ORDER` in `report_llm.py`), the exact provider `deny`
  exists to exclude. Translation carries holdings-derived report text
  (`with_holdings=True`); this was an explicit, scoped compliance tradeoff the
  product owner accepted for these two call sites only — Pass 2, regenerate,
  and holdings parsing (structured extraction) all keep `deny` enforced
  unchanged. Both call sites also pass `allow_fallbacks=False` (a hard pin,
  not a preference) alongside the `order` pin — since `deny` is off, an open
  fallback on DeepSeek unavailability could otherwise silently reroute the
  payload to an arbitrary marketplace provider that `deny` would normally have
  excluded; the call fails outright instead (PR #79 review finding). Do not
  extend the exception to any other call site without the same explicit
  sign-off, and never drop the `allow_fallbacks=False` pairing if you do.
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

Final gates (CI also enforces):

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
before pushing — `npm run dev`/`next build` do not exercise the same path
and will not catch this class of bug.

## CI-First Protocol (MANDATORY)

> **Ring 0 reality:** there is no automated CI yet — the local quality gate
> (see above), run before every push, stands in for it. There IS a branch +
> PR for every change regardless of Ring (see Branching below); "CI green"
> currently means "local gate green" on the PR's branch.

A task is NOT complete until CI is green.

After every `git push`:

1. Immediately run `gh pr checks --watch` (or `gh run watch`) and block until
   all checks finish.
2. **Green** → task may proceed.
3. **Red** → pull failing logs with `gh run view --log-failed`, fix the root
   cause locally (never retry blindly), commit, push again, re-watch.

Do not declare a task done, close a session, or move to the next task while
CI is red or still running. Leaving a PR red and moving on is the primary
failure mode this protocol exists to prevent.

## Branching

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
  is a known trap** (hit 2026-08-07, PR #93/#95/#96): squash-merging A with
  `--delete-branch` deletes A's branch, and GitHub **auto-closes any open PR
  whose base is that branch** — `gh pr reopen` / `gh pr edit --base` both fail
  once the base ref is gone (no recovery). If A merges before B is done, get
  B's commits onto `main` via `git merge main` (not `git rebase main` —
  replaying B's pre-squash commits against a squash-merged `main` produces
  spurious `add/add` conflicts, and `git rebase --skip` is a history-rewrite
  the auto-mode permission classifier blocks) and open a **fresh PR against
  `main`**, noting in its body which closed PR it supersedes. Also watch for
  a specific `git merge` footgun this surfaces: if B's branch added-then-
  removed something (e.g. moved a component out of a shared layout) before
  merging in A, the 3-way merge can silently **reinstate the removed code**,
  because B's net diff against the merge-base shows no change on those
  lines while A's does — re-check anything B deliberately deleted after
  merging.

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
(added 2026-08-06) is read + PR-review-only, and belongs to a **separate LLM
reviewer** in this project's multi-agent workflow — **this agent (whichever
LLM is doing the dev work) never uses `GITHUB_REVIEWER_TOKEN` itself, for
anything.** Any review or comment authored under that identity is that other
reviewer's independent output: read it, act on its findings, but its
approval is not a substitute for the product owner's own merge
authorization, and does not come from self-review. (Incident: 2026-08-06,
issue #78/PR #79 — this agent used `GITHUB_REVIEWER_TOKEN` to review its own
PR, then treated that as grounds to merge without the product owner's
sign-off. Reverted; see PR #79 for history.)

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

Releases are automated. **Never** bump versions or create tags by hand.
Let CI handle versioning, changelog, tag, and publish.

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
  `TEST_DATABASE_NAME` and migrates to head **once per pytest session**.
  `db_session` opens an outer transaction + SAVEPOINT
  (`join_transaction_mode="create_savepoint"`, `autoflush=False` to match
  production). `alembic_cfg` uses a **separate** database
  (`MIGRATION_DB_NAME`) so the revision walk cannot drop the session DB.
  `SessionLocal` is lazy (`get_engine` / `reset_engine`); under pytest it
  raises if `DB_NAME` is not `TEST_DATABASE_NAME` — a forgotten mock must
  fail the test, not write `portfonia_dev`. Celery task tests still mock
  `SessionLocal` (control flow, not SQL).
- **Test DB names are PID-suffixed, not fixed strings (issue #152)**:
  `TEST_DATABASE_NAME` (`app/core/database.py`) and `MIGRATION_DB_NAME`
  (`app/tests/conftest.py`) are `f"portfonia_test_{roundtrip,alembic}_{os.
  getpid()}"`, computed once at import time — not the literal
  `portfonia_test_roundtrip`/`portfonia_test_alembic` PR #137 originally
  used. Development now happens in isolated git worktrees (one per
  task/PR), so two `pytest` invocations against the same local Postgres can
  run concurrently; a fixed name meant one process's session-scoped
  teardown (`DROP DATABASE`) could drop the database out from under the
  other's still-running suite. Two live processes never share a PID, so
  this is collision-free for the only window that matters (concurrent
  runs); a DB orphaned by a hard-killed run just sits under its now-dead
  PID as harmless clutter — no automatic sweep, clean up manually if it
  ever actually accumulates.
- LLM prompt regressions: keep a small fixture of "input portfolio + expected
  shape of output" so prompt edits don't silently violate the layer-3 rule.
- Never let tests touch the developer's real home directory.
- **`caplog` assertions on an already-imported module's logger silently see
  nothing after the session migrate** (first hit 2026-08-13,
  `test_fund_nav_fetcher.py`; still true after #137 — upgrade now runs once
  per session via `session_test_db`, not per test, but that first
  `command.upgrade` is enough): `alembic/env.py` calls
  `fileConfig(config.config_file_name)` with no `disable_existing_loggers=
  False`, so it disables any logger that was already instantiated (e.g. any
  module-level `logger = logging.getLogger(__name__)` from a test's own
  imports) — `caplog.records` ends up empty with no error, which reads as
  "nothing got logged" rather than "the logger got disabled out from under
  the test". Workaround, scoped to the test file (not `alembic.ini`, which
  would be a wider blast radius than this needs):
  `logging.getLogger("your.module").disabled = False` right before the
  `caplog.at_level(...)` block.

## Documentation

- `README.md` — short, user-facing intro, install, run.
- `docs/` — architecture, ADRs, runbook snippets. All English.
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
