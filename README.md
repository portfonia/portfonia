# Portfonia

An AI portfolio-intelligence service that maps market events to an individual's actual holdings — and stops there.

## Why

Most market commentary is written for a generic audience. By the time it reaches an individual investor, two layers of work still remain: filtering for what is actually relevant to their positions, and translating a generic narrative into something specific to their exposure. Both layers usually do not happen, which is why people either drown in noise or ignore the market entirely.

Portfonia exists to close that gap with a narrow scope: take a user's real holdings, watch the market, and tell them what is worth noticing — without telling them what to do.

## What It Does

**Holdings.**
- Upload a CSV or Markdown sheet describing positions across US equities, Hong Kong equities, A-shares, public mutual funds, cash, and foreign currency. An LLM normalizes it into structured records.
- Broker/account/portfolio are normalized into a real accounts model (not just free-text tags) with report grouping by custodian. Identity and amount fields are encrypted at rest.

**Multi-user shared intelligence layer.** With more than one user, most of what moves markets is shared across them (the same tickers, the same macro events) — so the pipeline computes it once and personalizes only the last mile, instead of re-running search and analysis per user:
- Global price-anomaly detection runs once across the union of every user's holdings, then fans out per-user against each user's own thresholds.
- Shared caches for per-ticker intel and macro-event research, so two users holding the same name don't pay for the same research twice.
- A cross-name synthesis pass builds the day's market narrative once, then a personalized assembly layer allocates it per user under a fair-share budget.

**Analysis framework baseline.** Every report is generated under a fixed, server-side investment-philosophy baseline (what to pay attention to, not what to do about it) — invisible to the user, layered underneath the compliance boundary described below, never loosening it.

**Market and macro tracking.** Daily price, FX, and curated macro keyword scanning across reputable news sources (English-language primary, with Chinese-language sources for region-specific instruments).

**Personalized incremental briefings.** A scheduled report (per-user cadence — weekly by default, or Mon/Wed/Fri) covers what changed since your last one — tying price moves and macro signals back to your actual holdings, and to an optional investment-style profile the user sets once. Structured sections include a code-built price-anomaly table (§4.2), a descriptive technical-position table (§4.4 — distance to moving averages, 52-week range position, volatility), and a forward calendar (§2.5 — scheduled US macro releases, FOMC dates, and your holdings' earnings dates, mapped to the positions exposed to each). Causal attributions carry an evidence-confidence label (Established / Probable / Speculative) so calibrated uncertainty is legible rather than hidden.

**Three-layer output discipline.** Every AI-generated report stops at Layer 3:
- Layer 1 — what happened (fact)
- Layer 2 — how it relates to your holdings (contextual mapping)
- Layer 3 — signals worth watching (pointer to observation)

**Accounts, not a shared instance.** Invite-only signup, JWKS-verified sessions, server-enforced idle timeout — and user isolation is enforced at the identity layer itself (every service call takes an explicit user id; there is no ambient "current user" a bug could leave unfiltered), not left to per-query `WHERE` clauses to get right every time.

**Admin channel, no UI.** Operational tasks (manual report triggers, user management, cadence overrides) run through a token-authenticated `/admin/*` API, deliberately independent of user auth — it has to keep working even if the user auth system doesn't. No admin UI exists; it isn't needed for the endpoints to be usable.

**Email delivery.** Briefings are sent through a verified transactional sender; no app needs to be opened to read them.

## What It Does NOT Do

Portfonia is an intelligence service, not an advisory service. The following are out of scope by design:

- No buy / sell / hold / reduce / increase / target-price language. Ever.
- No trade execution, no broker integrations beyond ingest-only.
- No tax or capital-gains computation, no P&L from buy/sell history.
- No options, futures, or derivatives.
- No threshold price alerts ("X dropped 5%"). Every broker app already does that.
- No social or sharing features — holdings are sensitive data.

Every report carries a single bilingual disclaimer in its footer (injected at the template layer, never written by the model). Compliance scaffolding — the Layer-3 boundary, a vocabulary blacklist, and a post-generation output scan that holds any offending report for review instead of sending it — is enforced at the template and prompt layer, not left to the model's judgment.

## Status

Ring 1 — invite-only closed beta, multi-user. Ring 0 validated the core hypothesis: an LLM mapping market information onto an individual portfolio produces *cognitive lift* the user does not already get from their broker app, the financial press, or generic newsletters. Ring 1 built what running that for more than one person requires — real accounts, the shared intelligence layer above, and an admin channel to operate it. See the [v0.8.0 release](https://github.com/portfonia/portfonia/releases/tag/v0.8.0) for what shipped.

Public sign-up depends on the beta holding up across real invited users.

## Architecture

| Layer | Choice |
|-------|--------|
| Frontend | Next.js + shadcn/ui |
| Backend | Python FastAPI |
| Database | Self-hosted PostgreSQL (not a managed DB — kept out of the hosting-complexity budget) |
| Auth | Supabase Auth only (JWKS-verified sessions); no other Supabase services in use |
| Task queue | Celery + Redis |
| LLM | Pluggable via OpenRouter — provider and model choice stay swappable per call shape (structured extraction vs. free-text generation vs. primary analysis use different models today) |
| Production | Self-hosted VPS; provider/region/instance details are intentionally not published here |

## License

See `LICENSE`.
