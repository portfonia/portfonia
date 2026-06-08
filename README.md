# Portfonia

An AI portfolio-intelligence service that maps market events to an individual's actual holdings — and stops there.

## Why

Most market commentary is written for a generic audience. By the time it reaches an individual investor, two layers of work still remain: filtering for what is actually relevant to their positions, and translating a generic narrative into something specific to their exposure. Both layers usually do not happen, which is why people either drown in noise or ignore the market entirely.

Portfonia exists to close that gap with a narrow scope: take a user's real holdings, watch the market, and tell them what is worth noticing — without telling them what to do.

## What It Does

- **Holdings ingestion.** Upload a CSV or Markdown sheet describing positions across US equities, Hong Kong equities, A-shares, public mutual funds, cash, and foreign currency. An LLM normalizes it into structured records.
- **Market and macro tracking.** Daily price, FX, and curated macro keyword scanning across reputable news sources (English-language primary, with Chinese-language sources for region-specific instruments).
- **Personalized incremental briefings.** A scheduled report (Mon/Wed/Fri) covers what changed since your last one — tying price moves and macro signals back to your actual holdings, with sourcing annotations (`[price]`, `[news]`, `[analysis]`) so any claim is traceable.
- **Three-layer output discipline.** Every AI-generated report stops at Layer 3:
  - Layer 1 — what happened (fact)
  - Layer 2 — how it relates to your holdings (contextual mapping)
  - Layer 3 — signals worth watching (pointer to observation)
- **Email delivery.** Briefings are sent through a verified transactional sender; no app needs to be opened to read them.

## What It Does NOT Do

Portfonia is an intelligence service, not an advisory service. The following are out of scope by design:

- No buy / sell / hold / reduce / increase / target-price language. Ever.
- No trade execution, no broker integrations beyond ingest-only.
- No tax or capital-gains computation, no P&L from buy/sell history.
- No options, futures, or derivatives.
- No threshold price alerts ("X dropped 5%"). Every broker app already does that.
- No social or sharing features in early phases — holdings are sensitive data.

Every AI-generated conclusion is suffixed with `[For information only — not investment advice]`. Compliance scaffolding (disclaimer headers, vocabulary blacklists, sourcing requirements) is enforced at the template and prompt layer, not left to the model's judgment.

## Status

Ring 0 — single-user local prototype. The goal of Ring 0 is to validate one hypothesis: that an LLM mapping market information onto an individual portfolio produces *cognitive lift* the user does not already get from their broker app, the financial press, or generic newsletters.

Public MVP and multi-user rollout depend on that validation holding up over several weeks of real reports.

## Tech Stack

Next.js + shadcn/ui on the front, FastAPI + Celery + Redis + PostgreSQL on the back, pluggable LLM providers (Claude as the primary, with a cheaper model for non-personalized intelligence layers). Local development uses Colima + Docker Compose; production runs on Ampere ARM.

## License

See `LICENSE`.
