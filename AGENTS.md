# Portfonia — Codex Project Instructions

Read `CLAUDE.md` for the shared repository engineering, security, testing, and Git conventions. This file adds the Codex issue/design documentation contract.

## No over-engineering (MANDATORY, 2026-09-18)

`CLAUDE.md`'s "No over-engineering" section applies here without change:
requirements analysis, design, and implementation stop at "sufficient for
the actual, current requirement." Do not add a mechanism, abstraction,
validation layer, or defense-in-depth check because it is common practice
or covers a hypothetical future need. When unsure whether something is
required, ask the product owner before designing or implementing it.

## Documentation index

- [Public SEO pages and locale URLs](docs/mechanisms/frontend-chrome.md#public-seo-pages-and-locale-urls) — issue #702: public locale URLs, metadata, sitemap, OG previews, localized 404 and canonical host; issue #706: OG input/briefing layout and corrected home performance sample. #710 adds the protected Jade page, menu texture and Profile coordination. The same mechanism document describes the shared calculating overlay (#716).

- [Documentation governance](docs/playbooks/documentation-governance.md): document ownership, Obsidian authorization, executable design contracts, evidence labels, and note-access conventions.
- [Shared engineering rules](CLAUDE.md): language, security, testing, and deployment conventions.
- [Capture and reporting](docs/mechanisms/capture-and-reporting.md): free instrument collection and paid intel deepening and usage limits (issues #620/#621); paid-search headline filtering and public profile-name consistency (issue #628); body residue cleaning, event-version deduplication, low-value headlines and Google News summary removal (issue #630); headline-only paid resolution, CJK alias matching, and per-attempt plain-English collection reports (issue #635); stock-only headline recall, prompt-label alerts, rolling report anomalies and surfaced-ledger cleanup (issue #639); read-only web report history, Markdown/browser-print PDF downloads, and Ops async generate/poll/send (issue #642); merged Daily/Mon-Wed-Fri weekday batches (#650); no empty EXISTING block and duplicate self-reference handling (issue #653; earnings-date/recap stale checks removed by #697); default-off instrument Google News, current-run links, saved provider errors and wider duplicate comparison (#657; earnings-preview checks removed by #697); deterministic weekly fan-out test batch clock (#664); Parallel extract payload fix, weekend price-signal deepening and login-teaser/aggregator body markers and per-unit batch email layout (#670); removal of L1/L2/L3 shared analysis, the assembly path and report-time search remnants (#640); keep-first direct leads, earlier-batch movers, alias additions, related-company links and the weekly name/relation check (#681); strict relation-config shape validation and serialized weekly-check YAML (#683); exchange-calendar report windows and intel signals, exact-session baselines and comparisons, and position-based per-user market selection and anomaly deduplication (#686); batch macro candidate ranking, event deduplication, importance-ordered report bodies and development-first anchor guidance (#688); China NAV/ETF freshness verification during XSHG calendar gaps (#692); promo related headlines, list-like body residue and per-unit off-topic body rejection (#687); slot-only macro pool labels, development-only weekday counts, fresh-development weekend gating and stored-label reuse (#690); undated paid-search rejection, teaser/paid-release markers, table/caption/navigation residue, removal of #639/#653/#657 earnings-date stale checks and stop-after-resolution search budgeting (#697); two-stage Haiku-screen/Luna-review classification with one retry, fail-open WARNING and the ported LLM JSON parser (#700); widened file captions, Coinbase/Benzinga footer and Read Next card residue, and StockStory benchmarking titles (#699); Barchart trial banners and byline crumbs, interior share buttons, and City AM Featured sidebar residue (#708).
- [Jade portfolio tools](docs/mechanisms/jade-tools.md#holdings-replay-issue-714) — issue #714: shared total-return cache, nightly fill, unchanged-holdings replay, ETF proxies, historical FX, metrics and method disclosure. #716 adds calendar replay spans with a 1Y default, short-span metric rules and the shared calculating overlay.
- [Personal agent API access](docs/mechanisms/agent-api.md): issues #651/#652, report and intelligence pull endpoints, public agent documentation, token settings and authentication, agent-only limits and quiet windows, audit metadata, access notices, signed revoke links and Ops controls.
- [Snapshot export API](docs/mechanisms/portfolio-performance.md#snapshot-export-api): issue #643, caller-only complete daily snapshots, Daily Advanced access (#650), and no Ops read/export path.
- [Self-service account deletion](docs/mechanisms/identity-and-auth.md#self-service-account-deletion-issue-644): issue #644, Profile confirmations, recorded cash relinquishment, JWT-only deletion, shared waitlist/invite cleanup, and retained signup fingerprint.
- [Waitlist automatic invitations](docs/mechanisms/identity-and-auth.md#waitlist-automatic-invitations-issue-672): issue #672, daily 10:00 ET letters, run-start global quota, one-transaction rollback and later retry, locked pending refresh, explicit non-account `ADMIN_ID`, and the ops digest.
- [User referrals](docs/mechanisms/identity-and-auth.md#user-referrals-issue-675) — issue #675: Profile registration, two-tier signup attribution, first-subscription gift and purchase cash rewards, refund clawbacks including negative cash, and deletion guards; accounting rules in [Credit ledger](docs/mechanisms/credit-ledger.md#referral-rewards-and-refund-clawbacks-issue-675).
- [Subscription core](docs/mechanisms/subscription.md): subscription state, monthly credit charges/returns, user operations, quotes, unsubscribe integration, scheduled lifecycle, notices, Profile controls and public-copy integration, and launch activation (issues #595/#596/#597/#600/#610); Daily Advanced and merged weekday dispatch (#650); Advanced menu refresh, notice placement and Chinese every-other-day plan name (#660). #710 adds Jade at 9.99 credits/month, independent briefing cadence, Advanced access and Jade-only subscription management.
- [Git and review identities](docs/playbooks/git-and-review-incidents.md): token-only `gh api` (REST) writes, reviewer-token boundaries, and workflow history — includes why OAuth was retracted 2026-09-18.

## GitHub and Obsidian access

- Never attempt `gh auth login`/stored OAuth for this project (retracted 2026-09-18: reported invalid mid-session despite the owner re-verifying it).
- GitHub writes use `GITHUB_TOKEN` from this project's `.env.local`, verified as `portfonia` (`gh api user --jq .login`), via `gh api` (REST endpoints) only — never `gh issue`/`gh pr` or other high-level porcelain subcommands, which may call GitHub's GraphQL endpoint internally.
- GitHub reviews use `GITHUB_REVIEWER_TOKEN` from this project's `.env.local`; verify the API login is `blacktomb42`. Never use the reviewer token for issue maintenance, pushes, or merges.
- Obsidian operations use the configured MCP. Read the existing target, update in place within scope, and read back. Do not substitute UI automation or REST merely because a command tool fails; a different access method requires an explicit user instruction.
- Load tokens only for the required command; never print or persist their values. Authentication does not authorize a merge, deployment, or self-review.

## Language and Obsidian authorization (mandatory)

- Repository content and filenames must be English. Do not put Chinese prose, note titles, or filenames in repository instructions, documentation, or indexes.
- Obsidian project documentation is written in Chinese; conversation with the product owner defaults to Chinese; a handoff prompt written for a new session is written in Chinese and tells the receiving session to converse in Chinese too. See `CLAUDE.md`'s Language Policy section for the full statement — none of this relaxes the English-only rule above for anything written into the repository itself.
- Never create an Obsidian file without the user's explicit authorization to create that file. A request to update documentation, a missing note, a cross-reference, or a standing synchronization rule is not authorization to create one.
- Update only the relevant existing documents within the authorized scope. If an intended Obsidian target does not exist, stop that write and ask before creating it; continue other authorized work.
- Keep project development conventions and design-authoring guidance in this index and the repository's `docs/` directory. Do not create or recreate parallel Codex configuration/design notes in Obsidian.

## Isolated worktrees and review (mandatory)

- Treat the main checkout as read-only for task work. Every repository change, including documentation, instructions, configuration, and small fixes, starts in a separate git worktree on a task branch based on the current main branch.
- Create the worktree before editing; switching branches in the main checkout is not isolation. Keep the main checkout on main and clean, and preserve unrelated user changes.
- Commit and push the task branch, then open a PR for review. Documentation-only work is not an exception. Do not leave requested repository changes as unsubmitted local edits.
- Merge requires explicit current-conversation owner approval; do not merge or deploy merely because the PR exists or checks pass.
- If task edits accidentally land in main, preserve and verify them in the isolated worktree first, then remove only those task-owned edits from main. Never reset, clean, or discard unrelated work.

## Issue structure (mandatory project-wide convention)

- GitHub issue titles, bodies, and comments are English. Internal Obsidian design notes may be Chinese.
- Verify the repository-owner write identity before posting; do not use the reviewer identity for issue maintenance.
- Keep the body short: Summary, In scope, Out of scope, and links/index. End with: `Detail → comments: Requirements, Reasons, Exploration, Design, Contract constraints.`
- Publish **five separate comments**, headed exactly `## Requirements`, `## Reasons`, `## Exploration`, `## Design`, and `## Contract constraints`. Do not combine them in one body or one comment.
- Requirements state required behavior; Reasons explain why; Exploration records evidence, provenance, alternatives, and decisions; Design specifies how to implement; Contract constraints state invariants and execution gates.
- Keep related epic/dependency links explicit. Do not close parent/related issues without authorization. Split distinct urgency/risk scopes when appropriate.
- On revision, update the relevant existing comment and body links; avoid competing versions. Mark superseded rules explicitly.

## Implementation-ready design and contract (mandatory)

Write for an implementing LLM that has not seen the conversation. A design must specify affected modules and caller/reader/writer flow; schema/API fields, types and nullability; algorithms/formulas and normalization; operation order; boundary, unavailable-data and error branches; UI states; compatibility/rollout; and worked input/output examples.

Contract constraints must enumerate invariants, non-goals, dependencies, acceptance tests with concrete expected results, required validation/review gates, and deployment/data-operation authorization boundaries. A list of desired outcomes or “implementer should decide” is not an implementation contract. Specify only what the authorized fix needs; these dimensions are not a mandate to add subsystems, operational tooling, broad cleanup, or exhaustive test matrices. The owner decides whether adjacent work has value.

Distinguish confirmed product decisions, the authored implementation design, unresolved questions, and shipped behavior. Never invent approval. Resolve substantive financial/product ambiguity in the governing design and issue before dependent implementation.

**No open design-stage questions at implementation start (2026-09-10).** An issue is not ready for implementation while its Design or Contract constraints comment still contains an unresolved decision — this has repeatedly let an implementing LLM freelance or silently widen scope to fill the gap itself, rather than surfacing the ambiguity. Before implementation begins: get every substantive design decision (approach choices, failure-mode behavior, thresholds/policy values, data-model shape) explicitly resolved by the product owner and recorded in the relevant comment, editing it in place rather than appending a competing version. An issue may still note a deliberately deferred *future* decision (e.g. "escalate later if data warrants it") as long as the issue's own current scope has no dependency on that future decision's outcome — that is not the same as leaving today's implementation with a choice to make on its own.

## Documentation surfaces

For documentation updates, maintain the relevant repository document and its AGENTS.md index entry; update shared CLAUDE.md only when its rules are affected. Edit an existing Obsidian feature note only within the user's authorized scope. Do not automatically create companion notes or synchronize every documentation surface. Update governing sections in place when a rule changes; append incident evidence without leaving contradictory active rules. Detailed contracts and access conventions are in [Documentation governance](docs/playbooks/documentation-governance.md).

Use the configured Obsidian MCP. Keep credentials out of repository files, notes, and memory. These issue/design rules are project-wide; do not silently promote them to all projects.
