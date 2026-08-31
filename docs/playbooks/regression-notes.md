# Regression notes: full incident history

Full backstory for each "known-fixed bug" one-liner in CLAUDE.md's Product
Boundary section. Read this when the one-liner isn't enough context to
avoid re-introducing the bug, or when investigating a suspiciously similar
symptom.

## Fund NAV lookup (issue #1)

`compute_portfolio` must look up price data with `captured_closes.get(h.
ticker or h.fund_code or "")` — fund code-only holdings have no `ticker`,
and `capture_fund_navs` stores NAV in `price_snapshots` keyed by
`fund_code`. A ticker-only lookup silently drops every fund holding into
`stale_tickers` and out of the portfolio.

## Sector backfill on re-upload

`confirm_holdings` must call `backfill_sectors()` after commit —
re-uploading holdings clears all rows, and `sector` is otherwise only
populated by `POST /admin/portfolio/refresh` (moved from `POST
/portfolio/refresh`, removed, in issue #129 checkpoint B2) or the scheduled
capture tasks.

## Next.js Turbopack + multipart

Turbopack's `rewrites()` fails on `multipart/form-data` POST (ECONNRESET at
proxy). Upload routes need a real Next.js API Route (`route.ts`) that
manually forwards to the backend.

## `frontend/public/` must stay non-empty (issue #100/#101)

git doesn't track empty directories; `frontend/Dockerfile`'s runner stage
does `COPY --from=builder /app/public ./public`, which fails hard if the
directory doesn't exist in the build context at all. Keep at least one
tracked file there (a `.gitkeep` is fine) even after removing every real
asset.

This landed with PR #93 and sat undetected on `main` through two more PRs,
because production hadn't redeployed since; `bun run dev`/`next build`
don't care about a missing `public/`, only the Docker multi-stage build
does — see CLAUDE.md's Quality Gates section for the "doesn't actually
build Docker images" gap this exposed.

## Frontend lockfile drift, three production deploy failures (issue #227, PR #255, 2026-08-28)

**Fixed**: frontend has one lockfile (`bun.lock`) — `frontend/Dockerfile`
now runs `bun install --frozen-lockfile` on `oven/bun:1.4.0-alpine` for the
`deps`/`builder` stages (`package-lock.json` deleted, `packageManager`
pinned in `package.json`). The runner stage is unchanged — still
`node:22-alpine`, `CMD ["node", "server.js"]` — this was a package-manager
change only, not a runtime migration. No separate lockfile-sync step is
needed on `frontend/package.json` changes anymore.

**Why this took three separate production deploy failures (issues #226,
#243, PR #230 incident) before it got fixed**: reaching for `npm install`/
`npm ci` out of habit — even "just this once," even inside a Dockerfile
stage — kept reintroducing a second lockfile that drifted from `bun.lock`.
Package manager is `bun`, exclusively — never `npm`/`npx`/`yarn`/`pnpm`,
anywhere: local dev, CI-equivalent local gates, and the Dockerfile build.
