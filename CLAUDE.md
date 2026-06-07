# Portfonia — Agent Guidelines

AI-facing guidance for agent tooling working in this repository.
Last updated: 2026-06-07

## Current Development State (update this section when resuming)

| Item | Value |
|------|-------|
| Ring stage | **Ring 0** (local dev machine, single user, no cloud) |
| `main` HEAD | `5765c12` feat(reports): compliance output backstop, quiet-day heartbeat, CN encoding + audit hardening |
| Stages complete | A B C D E F1 F2 F3 G H (+ 3 Codex audit rounds + 1 Opus self-audit applied) |
| Next stage | **I** — 稳定运行验证（2-4 周，每周收到报告后主观评估质量） |
| Backend quality | ruff OK · mypy OK (57 files) · pytest **210 passed** |
| Frontend quality | tsc OK · eslint OK · next build OK |
| LLM model | `deepseek/deepseek-v4-flash` via OpenRouter (provider=DigitalOcean,Venice); `data_collection=deny` on every call |
| Infrastructure | Homebrew PostgreSQL@16 + Redis (native, not Docker); `make infra-up` not needed |
| Prompt version | `f2-v2` (Pass 1 = public macro+news only, no holdings-derived anomalies; f2-v1 = anomalies in Pass 1) |
| Disclaimer version | `f3-bilingual-v1` |
| Report statuses | `success` · `skipped` (quiet day, still emails heartbeat) · `needs_review` (compliance scan hit, NOT emailed) · `failed` · `in_progress` |

### Known technical debt (carry forward until resolved)

| ID | Description | Resolve at |
|----|-------------|-----------|
| F-DEBT-1 | `by_sector` "Other" conflates ETFs with unclassified equities | Ring 1+ |
| D-DEBT-1 | `backfill_sectors` serial O(n) `yf.Ticker().info` calls | Ring 1+ |
| D-DEBT-2 | `compute_portfolio` has no `user_id` filter (loads all holdings) | MVP |
| D-DEBT-3 | 天天基金 OCI reachability unverified | Ring 1 deploy |
| D-DEBT-4 | Price staleness: `stale_tickers` only catches NULL price, not stale-dated prices | TBD |
| G-DEBT-1 | `send_report_email` returns True even if `commit()` of `email_sent_at` fails; Resend Idempotency-Key prevents duplicate delivery but state is unpersisted. Persist a provider message id / use a conditional update. | Ring 1+ |
| G-DEBT-2 | Concurrent-send dedup is best-effort (in-memory `email_sent_at` check + Resend Idempotency-Key), not a DB lock. Add a row lock / conditional update if multi-trigger paths appear. | Ring 1+ |
| G-DEBT-3 | Email HTML uses a `<style>` block (`nth-child`, `max-width`, `border-radius`); not robust in Outlook/Gmail. Inline critical styles / table-based wrapper before real external recipients. | Ring 1+ |
| A-DEBT-1 | No DB domain constraints: `pricing_mode`/`asset_type`/`currency` are free `Text` (no CHECK/enum); `shares`/`current_value` have no `>= 0`. App layer + `ParsedRow` Literals are the only guard. Add CHECK constraints via migration **after** auditing existing dev rows (a CHECK that fails on legacy data blocks upgrade). | Ring 1 |
| A-DEBT-2 | Test suite drops+creates+migrates a fresh DB per `db_session` test (correct for drift detection, but slow). Move to session-scoped migrated DB + per-test SAVEPOINT rollback. | Ring 1 |
| A-DEBT-3 | `core/database.py` builds module-level `engine`/`SessionLocal` bound to the dev DB at import. Test isolation relies on every test overriding `get_session` or patching `SessionLocal` (discipline, not structure). | Ring 1 |
| A-DEBT-4 | `ParsedRow` uses `float` for `shares`/`avg_cost`/`current_value`, bridged to `Decimal` via `Decimal(str(x))` at confirm. Bridge is adequate but inconsistent with the Decimal-everywhere model. | TBD |
| A-DEBT-5 | `/holdings/upload` has no request body-size cap (`await file.read()` loads fully into memory). Local Ring 0 low risk; add a limit before any exposed deployment. | Ring 1 |

## Language Policy (MANDATORY)

- **All repository content is English**: code, identifiers, comments, commit
  messages, PR descriptions, issue text, README, `docs/`, ADRs, tests.
- **In-product strings are i18n-keyed** and shipped through the translation
  layer, never hardcoded in any single language. Supported runtime UI and
  report languages: English and Simplified Chinese (extensible).
- Translation resources live under a dedicated locales directory and are the
  only place where non-English text legitimately appears in the repo.

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
- Each AI conclusion is suffixed with `[For information only — not investment advice]`.

## Architecture

| Layer | Choice |
|-------|--------|
| Frontend | Next.js + shadcn/ui |
| Backend | Python FastAPI |
| Database | PostgreSQL (Supabase managed, includes Auth) |
| Task queue | Celery + Redis |
| LLM | Pluggable (Claude / DeepSeek / etc.) — keep provider-swappable |
| Local dev | Homebrew PostgreSQL 16 + Redis (native); Colima for Hermes gateway only |
| Production | OCI [REDACTED-INSTANCE-SPEC] (Ubuntu 24.04 LTS) |

### Three-layer deployment flow (MANDATORY)

```
Local (~/Portfonia)   →   GitHub   →   VPS (git pull && docker-compose up -d)
   write code             transport      run only
```

- Code authority lives in **local → Git**. The VPS is never an editor.
- The only legitimate VPS-side state outside Git is `.env` (uploaded via `scp`).
- Never edit code on the VPS, never `git commit` on the VPS, never use the VPS
  as a sync hub between machines.

## Secrets and Configuration

- `.env` files are **never** committed. Enforce via `.gitignore` from day one.
- API keys (Claude, Resend, market-data providers) are loaded from `.env` only.
  Never hardcode, never log, never echo to stdout in error paths.
- For test code: never read or write the developer's real `~/.config/...`
  directories. Honor a project-scoped env var (e.g. `PORTFONIA_HOME`) and
  default tests to a temp dir. Direct use of `os.path.expanduser("~")` in
  code that tests will exercise is a bug.

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
- **`data_collection=deny` is applied to every LLM call** (not just
  holdings-bearing ones) as defense in depth: even if holdings leak into Pass 1
  in the future, the call still cannot route to training providers.
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
bun run format           # 1. prettier write
bun run lint:fix         # 2. eslint --fix
bun run typecheck        # 3. tsc --noEmit
bun run test             # 4. tests
```

Final gates (CI also enforces):

- Type check passes (mypy strict, tsc strict).
- Lint passes with zero warnings.
- Format check passes (non-mutating).
- All tests pass.
- No `any` / `Any`, no non-null assertions, no unused exports.

## CI-First Protocol (MANDATORY)

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

```
main (production) ← dev (integration) ← feat/* | fix/* | docs/*
                                          ↑
                                          hotfix/* (only emergencies, from main)
```

- Never commit directly to `main` or `dev`.
- Feature branches start from `dev`. Hotfix branches start from `main`.
- `dev → main` promotion PRs must use `feat:` or `fix:` (a `chore:` title
  will not trigger a release).
- Delete branches after merge.

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
- Integration tests hit a real Postgres (docker-compose service), not a mock.
  The whole point is to catch schema/migration drift.
- LLM prompt regressions: keep a small fixture of "input portfolio + expected
  shape of output" so prompt edits don't silently violate the layer-3 rule.
- Never let tests touch the developer's real home directory.

## Documentation

- `README.md` — short, user-facing intro, install, run.
- `docs/` — architecture, ADRs, runbook snippets. All English.
- Update docs **in the same PR** as the code change that motivates them.
- API-level changes update `--help` text / OpenAPI schema / route docs in
  the same PR. Code and docs out of sync is a defect.

## Out of Scope (do not let scope creep pull this in)

- Trade execution.
- Tax / capital-gains computation.
- Transaction-log tracking (P&L from buy/sell history).
- Options / futures / derivatives.
- Price-only alerts ("ticker dropped 8%") — every broker app does this; we do
  signal-driven alerts, not threshold alerts.
- Social / sharing features (sensitive data — defer until Phase 2 with
  serious anonymization review).

## When Principles Conflict

- **Compliance > everything**. If a feature can't be shipped without crossing
  the layer-3 boundary, the feature does not ship.
- **UX > YAGNI** for user-facing surfaces. If users need it, it's not
  speculative.
- **KISS applies to code AND user journey** — fewer steps, fewer options,
  fewer modes by default.
- **Reversibility check before destructive actions** (DB migrations dropping
  columns, `rm -rf`, force pushes). Confirm with the user before executing.
