# Portfonia — Agent Guidelines

AI-facing guidance for agent tooling working in this repository.
Last updated: 2026-09-01 (trimmed for size — see `docs/playbooks/`; mechanism
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

- [Frontend chrome (header/nav) convention](docs/mechanisms/frontend-chrome.md) — issue #146/#148: one shared `SiteHeader`, auth-gated Get Started menu; issue #214 (PR #215): unconditional pathname-triggered session re-verification (no throttling — a grace window that shipped in PR #215 was reverted the same day), optimistic logout + login-pending placeholder, bounded `getUser()` timeout/retry, Home menu entry; issue #209: global next-intl message catalog (`frontend/src/locales/{en,zh-Hans,zh-Hant}.json`) replaces `home-messages.ts`/`messages.ts`, `lang` and the locale switcher are no longer home-only, no URL-based locale routing (product decision); issue #220 (PR #228): Home menu entry replaced by Profile (`/profile`), lucide-react icons on every menu entry, new `profile` locale namespace; issue #269 (PR #270): Profile page section reorder (gap card → Email Verification → Account → ... → Change password → Delete account) plus two new `Card` variants — `variant="urgent"` (soft pink) for the gap card/Email Verification, `variant="danger"` (red border only, no fill) for Delete account, not interchangeable.
- [Profile page: GET /me account summary](docs/mechanisms/identity-and-auth.md) — issue #220/PR #228: `/profile` account email + change-password + investment-style link + delivery-email display + non-interactive placeholders; `GET /me` ships the full #221 response shape (`missing` never contains `"tos"`). Issue #269/PR #270 added `email_verified_at`/`delivery_email_verified_at` to `GET /me` — see the Frontend chrome entry above for the page-layout half of that PR.
- [Post-signup onboarding](docs/mechanisms/frontend-chrome.md) — issue #221: ToS gate (client checkbox + backend `Literal[True]`, both required), `signup` redirects to `/questionnaire?onboarding=1` (the only `mode="onboarding"` trigger), `mode` prop on `QuestionnaireForm`/`HoldingsManager` (no `/onboarding/*` route tree), new `/welcome` route with sessionStorage dedupe, Profile gap card reading `GET /me`'s `missing`, `report_cadence` defaults to `weekly`, admin manual-generate's no-holdings 422 removed (`active_user_ids()`/Beat untouched at the time — cadence follow-up landed separately as issue #191, see the entry below), and a real password-leak fix in the 422 secret-redaction handler that adding the required field exposed. Issue #280: onboarding questionnaire Save now routes to `/holdings?onboarding=1` instead of `/welcome` (the §2.2 table inverted the linear ToS → questionnaire → holdings → welcome order; design correction in Ring 1-Onboarding.md §9.1), holdings onboarding gains a Skip link into `/welcome`, `/welcome` gained an unverified-email prompt (`delivery_email ?? email`, per-scope check matching Profile's issue #269 §6 rule); issue #290 lifted the pre-gate register — welcome copy split into holdings status (never mentions send) + delivery claim mirroring Layer 2's send decision (send-stop only when both timestamps are null, naming the address that would actually receive; PR #294 review corrected the round-1 #269 per-shown-address predicate), Profile's `emailVerificationNoRecipient` now states send-stop, and successful login redirects unconditionally to `/profile` (`/login` only serves returning users — signup never passes through it).
- [Async holdings upload](docs/mechanisms/holdings-pipeline.md) — issue #77/#82/#85: `POST /holdings/upload` returns 202 + job id, Celery parses, 45s SLA, two-layer hard-kill resolution.
- [Holdings encryption at rest](docs/mechanisms/holdings-pipeline.md) — issue #31: field-level Fernet via SQLAlchemy `TypeDecorator`, system-wide key, `ORDER BY` moved to Python.
- [Holdings domain CHECK constraints](docs/mechanisms/holdings-pipeline.md) — issue #25: DB-level CHECKs on `pricing_mode`/`asset_type`/`currency`/`asset_class`, naming-convention gotcha.
- [Postgres backup to OCI Object Storage](docs/mechanisms/backup-and-ops.md) — issue #106/#76: daily `pg_dump` -> OCI, instance-principal auth, two real restore drills.
- [Cash/wmf holdings exclusion fix](docs/mechanisms/holdings-pipeline.md) — issue #120/PR #121: structured-extraction model put cash amounts in the wrong field; two-layer fix.
- [Accounts table + user_id FKs](docs/mechanisms/holdings-pipeline.md) — issue #129 checkpoint B7: `accounts` table + `holdings.account_id` (additive, text columns unchanged), `holdings`/`reports`/`upload_jobs`/`news_surfaced` gain real `user_id` FKs (`ON DELETE RESTRICT`), SQLAlchemy `relationship()` needed purely for flush-order correctness.
- [Holdings single-row CRUD + confirm modes](docs/mechanisms/holdings-pipeline.md) — issue #92 / #130 C1, PR #310 (6 review rounds, merged `433703a`): `POST`/`PATCH`/`DELETE /holdings/{id}`, `PATCH /holdings/reorder`, `POST /holdings/confirm?mode=append|replace` (default append), `#####` export/template dialect. Manual-listed export always emits all three numeric slots (shares/avg_cost/current_value) with a `-` placeholder for an unset one — round-4 emitted only present ones, which could round-trip into a fabricated cost basis. `normalize_ticker_and_currency()` shared by file-import and `POST`/`PATCH` so both paths store the same canonical ticker. `ticker_no_suffix` (market undetermined) vs. `ticker_suffix_ambiguous` (market known, suffix ambiguous — Europe/Korea/unplaceable A-share) are separate codes; the latter forces `capture_supported=False` so a bare ticker never defaults to speculative US capture. Issue #319/PR #321 (12 UX follow-ups): nav-entry dedup, inline edit of shares/avg_cost/pricing_mode on `/holdings/edit`, ticker/fund_code merged into one detail-form field, `asset_type`/`market`/`pricing_mode` dropped from export tags (`account`/`portfolio`/`notes` kept — see the mechanism doc for the round-1 review catch that reversed an initial full-removal), `GET /holdings/export`/`GET /holdings/template` gain a UI-locale-overriding `locale` query param, append/replace mode chosen before file selection, sortable `/holdings/edit` headers reusing `PATCH /holdings/reorder`. Issue #323/PR #327 closed item 10's remaining gap: a mode-agnostic, count-free `replaceHint` callout now shows the moment Replace is chosen, before any file is picked — the post-parse `replaceConfirmBody` dialog (real row counts) is unchanged and stays the actual gate.
- [Portfolio overview dashboard — C2](docs/mechanisms/holdings-pipeline.md) — issue #320 (#130 C2), PR #322: `/portfolio` reads `GET /portfolio/summary`; `by_group` aggregates `Holding.portfolio`, `by_account` aggregates `Holding.broker` **not** the normalized `accounts` table (deliberate — `accounts`' dedup key is the `(broker, account, portfolio)` triple, so reading `account_id` would fold the group dimension back into account); P&L (`cost_basis_base`/`unrealized_pnl_base`/`unrealized_pnl_pct`) computed only for `pricing_mode=="auto"` with both `shares` and `avg_cost` and a valuation, `None` (not zero) otherwise — cash/wmf and `capture_supported=False` holdings never get one; frontend partitions the "no market quote" section on `pricing_mode=="auto" and not capture_supported`, never on the broader `market_value_base is None`; `base_currency` widened to all 15 `VALID_CURRENCIES`; `price_as_of_date` is the max captured-close trade date actually used (`None` on a cash-only book — the as-of banner has separate copy for that case, not "no priced holdings").
- [Portfolio overview email — explicit send button](docs/mechanisms/holdings-pipeline.md) — issue #202, PR #329: `POST /portfolio/send-overview`, not tied to `confirm_holdings` (original auto-send-on-confirm design was replaced pre-implementation once #319/#321's incremental editing made "no dedup, fire on every confirm" mean one email per row save); 15-minute per-user Redis `set_nx` cooldown (`check_portfolio_overview_cooldown`/`release_portfolio_overview_cooldown`, deliberately not the abuse-alerting `_enforce_ip`/`_trip` path), reuses `compute_portfolio()` + `report_sections._build_footer`, dedicated locale-aware table renderer (not `_build_section1`, whose headers are English-only pending the full report's LLM translation pass this lightweight email skips), no `reports` row/LLM call. `next_occurrence_for_cadence` (`app/tasks/__init__.py`) is the shared next-fire-time helper.
- [FX currency coverage + ticker-normalization consistency](docs/mechanisms/capture-and-reporting.md) — issue #204/PR #253: `_CURRENCY_TO_FX_PAIR`/`fx_fetcher._PAIRS` widened from 3 to all 14 `VALID_CURRENCIES`; `_yfinance._normalize_ticker` composes a ticker-collision override table (`_TICKER_SYMBOL_OVERRIDE`) with the existing HK normalizer; every consumer that derives a lookup/join key from a raw `Holding.ticker` must call it — full list of call sites and the 3 rounds of review findings that surfaced them in the mechanism doc. The GBX subunit correction that originally lived here as a per-ticker `_TICKER_PRICE_SCALE` table was superseded by #311/PR #312's generic `currency == "GBp"` detection (see the next entry) — `_TICKER_SYMBOL_OVERRIDE` (ticker-collision) is a separate, still-current mechanism. #252 (no scheduled capture for `market="Other"`) closed via #311/PR #312.
- [Multi-market capture: UK/Europe/Japan/Korea + capture_supported](docs/mechanisms/capture-and-reporting.md) — issue #311/PR #312 (merged `5512a14`, resolves #252): `app/services/markets.py`'s `CAPTURE_MARKET_ORDER` widens from 3 markets (US/HK/A-Share) to 7 (adds UK/Europe/Japan/Korea); new `Holding.capture_supported` boolean — unresolvable tickers persist `market="Other"` + `capture_supported=False` rather than being silently mis-bucketed or dropped, never inferred from `market=="Other"` alone; `resolve_holding_market()` recomputes server-side on every write so a client cannot forge `capture_supported=True`. GBX (pence vs. pounds) correction generalized from a one-entry ticker allowlist to reading yfinance's actual fetched `currency == "GBp"` for any `.L` ticker. Issue #313 (6 non-blocking items named in PR #312's approving review 5087667823) closed via PR #314 (merged `6b743b6`): `global_identifier_universe` now gates on `capture_supported`; `_safe_scaled_price`/OHLCV check treat empty-string LSE currency as unknown (falsy, not just `None`); no Alembic head fork exists on `main` (verified, regression-guard test added, no merge revision needed); one-off `app/scripts/backfill_capture_supported.py` re-resolves existing `market="Other"` rows (dry-run by default) mirroring `_apply_write_defaults`'s suffix-forcing sequence — round-1 review caught a real bug in an earlier version that skipped suffix-forcing and would have promoted a bare currency-hinted ticker (e.g. `VOD`+GBP) straight to US, fixed in `636954b`; bare-ticker-with-no-suffix-and-no-hint deferred to Ring-1-C/#204 with an in-repo pointer to PR #310 (merged, resolves the declared-market/currency-hint case already). Round-2 post-merge review (0 bugs) surfaced 3 further non-blocking items, closed by PR #318 (issue #316): the backfill script's scan now excludes `asset_type in ("cash", "wmf")` outright (a legacy pre-#120 row with a spurious ticker, e.g. `"CASH"`, could otherwise still be mis-promoted); the mechanism doc's re-confirm-parity claim corrected — re-confirm only reaches the correct value for an already-suffixed listed-Other row, not a still-bare currency-hinted one, since `_apply_write_defaults` passes the stored `"Other"` straight into `apply_confirmed_exchange_suffix`'s already-confirmed gate, and only the backfill script (which seeds `market=None`) fixes that case; and the self-contradictory line in `_resolve`'s docstring removed.
- [Fund NAV realtime path: Sina Finance fallback](docs/mechanisms/capture-and-reporting.md) — issue #20: Tiantian Fund's realtime endpoint blocked in production, Sina fallback added.
- [Fund NAV staleness observability](docs/mechanisms/capture-and-reporting.md) — issue #298/PR #303: per-fund stale/missing NAV WARNING + ops alert inside `capture_fund_navs` (session-lag threshold, weekend reference, empty-history miss); durable Redis dedup `app/core/alert_dedup.py` (fail-open, key recorded only on confirmed delivery); observability only — beat schedule untouched, #135 root cause still open.
- [Capture layer + incremental reporting](docs/mechanisms/capture-and-reporting.md) — ADR-002: capture nodes, report window, multi-user fan-out (Ring 1 A1).
- [Per-user report cadence (mwf/weekly)](docs/mechanisms/capture-and-reporting.md) — issue #191, cadence follow-up to #221 §8: `_REPORT_CADENCES` is a table of Beat rows each scoping its own `active_user_ids(session, cadence)` fan-out; `weekly` fires Saturday 19:00 ET (`session_node="weekend_snapshot"`), holdings gate loosened only for `weekly`; `users.report_cadence` gains a `CheckConstraint`; new Ops endpoint `POST /admin/users/{user_id}/cadence` (see `docs/mechanisms/admin-surface.md`).
- [Per-user report language](docs/mechanisms/capture-and-reporting.md) — issue #308: `users.locale` gains a `CheckConstraint` (`VALID_REPORT_LANGUAGES = ("en", "zh")`) and becomes the real `output_lang` driver for self-service generate/regenerate and the scheduled fan-out (each recipient read fresh per iteration); self-service `PATCH /me/report-language`, Ops `POST /admin/users/by-email/report-language` (grouped by URL shape next to the by-email purge route, no re-typed-email confirmation); signup accepts an optional `locale`, forwarded from the frontend's UI-locale selection; admin manual-generate endpoint deliberately unchanged (still the global default).
- [L2 shared macro-event cache](docs/mechanisms/capture-and-reporting.md) — Ring 1 stage A3, issue #128: per-event-key cache, two daily budgets.
- [Personalized assembly + fan-out budget fairness](docs/mechanisms/capture-and-reporting.md) — Ring 1 stage A4, issue #128: `report_assembly.py`, `shared_budget.py` fair-share allocation.
- [L3 day-level cross-name synthesis](docs/mechanisms/capture-and-reporting.md) — Ring 1 quality gate, issue #128/PR #167: cross-name mechanism clusters, leak-prevention shape.
- [Narrative-layer redesign: Pass 2 material widening](docs/mechanisms/capture-and-reporting.md) — Ring 1 quality gate, issue #128/PR #168: material sharing not narrative sharing, large no-anomaly holdings get material too.
- [System default analysis framework — B1](docs/mechanisms/identity-and-auth.md) — Ring 1 stage B, issue #129/PR #172: `config/analysis_framework.yml`, injection order, v1->v2, §2 rewrite.
- [Identity seam: current_principal + explicit user_id — B3](docs/mechanisms/identity-and-auth.md) — Ring 1 stage B, issue #129/PR #181.
- [Users, invites, and JWKS auth — B4](docs/mechanisms/identity-and-auth.md) — Ring 1 stage B, issue #129/PR #183: JWKS verification, no `JWT_SECRET`, invite redeem.
- [Idle-timeout server enforcement](docs/mechanisms/identity-and-auth.md) — issue #235: the 15-min auto-logout (issue #207) was client-only and silently defeated by closing the browser; `current_principal` now checks a Redis record keyed by `(user_id, session_id)` (`app/core/idle_activity.py`), fail-open on Redis outage (deliberate departure from rate_limit's fail-closed convention). PR #240 took 3 review rounds, each catching a real flaw in the previous fix (stale lock surviving re-login → an `iat` comparison broken by silent token refresh → a value-comparison still keyed too coarsely) — full round-by-round history in the linked mechanism doc, worth reading before touching this code given how easy each of those three flaws was to miss.
- [Ops user hard-purge](docs/mechanisms/identity-and-auth.md) — issue #199/#225/B7: `DELETE /admin/users/{id}?confirm={email}` hard-deletes one user's own rows (now including `accounts`, `email_verifications`) and the Supabase Auth account (sequenced before local deletes, 502+no-op on failure); also handles Auth-only orphans with no local row. Issue #274/PR #275 added a sibling `DELETE /admin/users/by-email?email={email}&confirm={email}` — same purge logic via a shared `_purge_local_user`, orphan path resolves through a new `get_auth_user_by_email` (GoTrue's `GET /admin/users` ignores an `email` query param and needs its real `filter` param + pagination, never `users[0]`), plus an `auth_subject` occupancy check before any Auth delete on that path.
- [Ops user directory read](docs/mechanisms/identity-and-auth.md) — issue #278/PR #285: `GET /admin/users` is the read-only sibling of the purge routes above, not a generic troubleshooting list — it exists because the delete-by-email pre-delete confirmation policy (report `created_at`/investment-context/holdings-count, then require a human re-typed email) had no API and still needed SSH+psql. Optional filters `email` (exact match), `status`, `report_cadence`, `limit`/`offset`; response is `{id, email, status, created_at, report_cadence, auth_subject_bound, has_investment_context, holdings_count}`.
- [Generic email verification: core mechanism + Ops API](docs/mechanisms/email-verification.md) — issue #260/PR #261: `email_verifications` table, GET-inert status lookup + Altcha PoW + POST confirm, async Resend delivery-status poll, `POST`/`GET /admin/email-verifications`, public `/verify-email` page. Issue #262/PR #263 added the signup hook (async Celery task, never inline — `create_verification` blocks on Resend), `GET /me`'s pending-verification list, and session-authed `POST /email-verifications/{id}/resend`. Existing users need a one-off Ops-triggered backfill script, not a bulk endpoint. Issue #257/PR #279 added HMAC-signed per-address unsubscribe (`GET`/`POST /unsubscribe`, `status=revoked`, public `/unsubscribe` page) plus `text` + `List-Unsubscribe` headers on `send_report_email`; confirm copy revokes verification only. Issue #276/PR #288 (`4bd1a5d`): Layer 1 `active_user_ids()` requires a verified address on every cadence; Layer 2 `recipient_email_with_purpose()` returns only an own-field-verified address (no cross-fallback); admin generate stays exempt from Layer 1. Issue #289/PR #292: expanded report-email unsubscribe footer copy, /unsubscribe page unified on the plain "unsubscribe" register (generic platform phrasing, not claiming verification mail stops), new session-authed `POST /email-verifications` (fresh create for the caller's own `email`/`delivery_email`, shared resend limiter), and Profile `noVerifiedRecipient` gap-card recovery rows (Send hidden per purpose when an actionable row exists). Issue #290/PR #294: ERROR-log split inside the `resolved is None` fork (missing/inactive row keeps ERROR, active-unverified logs WARNING + `email_skip_reason=no_verified_recipient`; ops alerts not downgraded), Welcome/Profile copy lifted to post-gate send-stop (Welcome claim mirrors Layer 2 — send-stop only when both timestamps null, naming the address that would receive), and the admin-generate docstring no longer claims a successful run always emails. Merge ≠ deploy (`b7c8d9e0f1a2`). Full detail in the linked mechanism doc.
- [Signup / invite anti-abuse](docs/mechanisms/identity-and-auth.md) — issue #190: Redis fixed-window limits on `POST /auth/signup` and `POST /admin/invites`; no Turnstile; fail-closed on Redis; known-invite buckets only; Next.js hop forwards XFF; global invite-mint 200/day alert-only.
- [Forgot-password trigger](docs/mechanisms/identity-and-auth.md) — issue #231: `POST /auth/forgot-password` backend-mediated Supabase reset trigger, self-hosted Altcha PoW (no Sentinel, no external CDN — vendored `frontend/public/altcha.js`), IP+email Redis rate limit reusing issue #190's machinery, local-`users`-table exists/not-exists response (deliberate OWASP-enumeration deviation), `/reset-password` client-direct to Supabase (no PoW); link expiry (72h targeted, 24h actual — Supabase's Email OTP Expiration caps at 86400s) and "password changed" email are Supabase Dashboard config, not code.
- [Frontend auth closure — B5](docs/mechanisms/identity-and-auth.md) — Ring 1 stage B, issue #129: `/login`+`/signup`, `src/proxy.ts`, cookie session via `@supabase/ssr`.
- [Investment-style questionnaire — B6](docs/mechanisms/identity-and-auth.md) — Ring 1 stage B, issue #129: `user_investment_context`, 3-layer enum validation, all 8 questionnaire dimensions + `free_text` reach Pass 2 AND assembly behind a SCOPE guardrail (decision point 6, corrected 2026-08-25), `/questionnaire` wizard.
- [Macro keyword theme pool](docs/mechanisms/macro-keywords.md) — issue #129 B1 + issue #175: widened to 17 themes; bare single-word keywords false-fire, always qualify.
- [News dedup ledger](docs/mechanisms/news-dedup.md) — issue #30: `news_surfaced` ledger closes the window-boundary permanent-miss gap; per-user uniqueness.
- [`report_generator.py` module split](docs/mechanisms/report-generator-refactor.md) — issue #37: pure refactor into `report_context`/`report_llm`/`report_serializers`/etc.
- [Report content features](docs/mechanisms/report-content-and-email.md) — Ring0 #1-4 + R-3/R-5/R-6/R-7/R-8: §4.2 anomaly table, confidence labels, §4.4 technical position, §2.5 forward calendar.
- [Report email HTML rendering](docs/mechanisms/report-content-and-email.md) — issue #24/#117: inline styles from `_TAG_STYLES`, bulletproof wrapper table, zebra striping. Issue #257/PR #279 added the `text` alternative and List-Unsubscribe headers (token/confirm-page mechanism is in `email-verification.md`).
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
  Report output languages are separate and narrower: report translation
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
