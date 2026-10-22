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

The single mechanism index is `CLAUDE.md`'s "Mechanism deep-dives" list (one line per `docs/mechanisms/` file). Do not duplicate it here.

- [Shared engineering rules and mechanism index](CLAUDE.md): language, security, testing, deployment conventions.
- [Documentation governance](docs/playbooks/documentation-governance.md): document ownership, Obsidian authorization, executable design contracts, evidence labels, note-access conventions and [public surface sync](docs/playbooks/documentation-governance.md#public-surface-sync).
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

- GitHub issue titles, bodies, and comments are English. Internal Obsidian design notes may be Chinese. Record owner-supplied UI wording as English source sentences. Do not paste Chinese or any other non-English paragraph into the issue. Locale translations are written in the locale catalogs at implementation.
- Verify the repository-owner write identity before posting; do not use the reviewer identity for issue maintenance.
- Keep the body short: Summary, In scope, Out of scope, and links/index. End with: `Detail → comments: Requirements, Reasons, Exploration, Design, Contract constraints.`
- Publish **five separate comments**, headed exactly `## Requirements`, `## Reasons`, `## Exploration`, `## Design`, and `## Contract constraints`. Do not combine them in one body or one comment.
- Requirements state required behavior; Reasons explain why; Exploration records evidence, provenance, alternatives, and decisions; Design specifies how to implement; Contract constraints state invariants and execution gates.
- Keep related epic/dependency links explicit. Do not close parent/related issues without authorization. Split distinct urgency/risk scopes when appropriate.
- On revision, update the relevant existing comment and body links; avoid competing versions. Mark superseded rules explicitly.

## Execution gates (mandatory, 2026-10-14)

On 2026-10-14, during the discussion that became #763, an agent opened a worktree and edited the agent page after the owner corrected copy and described a public page. The owner had not asked to implement. The same agent then pasted Simplified Chinese into the GitHub issue. After the owner asked to send a review, a transient rate-limit response was reported as a stop instead of retried until the send was accepted.

- Start implementation, including a task worktree, only when the current message explicitly asks to implement, dispatch, or change product files. Discussing copy, correcting a draft, asking a question, or "open an issue" is not that ask.
- After the owner has asked to send or dispatch, retry a transient transport or rate-limit failure until the send is accepted. Do not end the turn with only the failure report.
- When a PR implements an issue and the owner has not asked to leave that issue open, the PR body and the squash commit message use `Closes #<n>`. Do not write `Refs` unless the owner gives a reason to keep the issue open.

## Implementation-ready design and contract (mandatory)

Write for an implementing LLM that has not seen the conversation. A design must specify affected modules and caller/reader/writer flow; schema/API fields, types and nullability; algorithms/formulas and normalization; operation order; boundary, unavailable-data and error branches; UI states; compatibility/rollout; and worked input/output examples. It ends with a "Public surfaces affected:" line listing every surface outside the paywall the change touches, or "none" (see CLAUDE.md "Public surfaces stay in sync with the product").

Contract constraints must enumerate invariants, non-goals, dependencies, acceptance tests with concrete expected results, required validation/review gates, and deployment/data-operation authorization boundaries. A list of desired outcomes or “implementer should decide” is not an implementation contract. Specify only what the authorized fix needs; these dimensions are not a mandate to add subsystems, operational tooling, broad cleanup, or exhaustive test matrices. The owner decides whether adjacent work has value.

Distinguish confirmed product decisions, the authored implementation design, unresolved questions, and shipped behavior. Never invent approval. Resolve substantive financial/product ambiguity in the governing design and issue before dependent implementation.

**No open design-stage questions at implementation start (2026-09-10).** An issue is not ready for implementation while its Design or Contract constraints comment still contains an unresolved decision — this has repeatedly let an implementing LLM freelance or silently widen scope to fill the gap itself, rather than surfacing the ambiguity. Before implementation begins: get every substantive design decision (approach choices, failure-mode behavior, thresholds/policy values, data-model shape) explicitly resolved by the product owner and recorded in the relevant comment, editing it in place rather than appending a competing version. An issue may still note a deliberately deferred *future* decision (e.g. "escalate later if data warrants it") as long as the issue's own current scope has no dependency on that future decision's outcome — that is not the same as leaving today's implementation with a choice to make on its own.

## Scope discipline for issue authors and implementers (mandatory, 2026-10-13)

The #753/#754/#755 batch shipped and designed mechanisms the owner never selected: whole-PUT idempotency, a valuation preview, version no-op detection, a refactor justified by reuse that never happened, and tests of pre-existing behavior. Its cost was a large share of the implementer's weekly quota (inventory: #759). The completeness checklist above defines what a contract must answer. It is not a list of things to add.

**When writing an issue or design:**

1. **Trace every item to an owner decision.** Each Requirement, Design element and acceptance test must trace to an owner decision recorded in the governing document or the conversation. Cite where it is recorded. A proposal, suggestion or "author recommends" entry is not a decision. Never write "owner confirmed/accepted" for anything not recorded as such.
2. **Take the simplest option for technical defaults the owner did not decide.** Examples: bump a version on every successful write rather than detect no-ops; use one cleanup path rather than several.
3. **Change code in place.** Do not move or refactor code because a later issue might reuse it.
4. **Write acceptance tests only for new or changed behavior.** Use one focused test per behavior. Require real-Postgres or concurrency tests only where this change introduces the race or transaction risk. Never require tests of pre-existing behavior. Keep worked examples to the few that pin ambiguous rules.
5. **Gates are the CLAUDE.md gates for the touched files.** Do not add extra gates, review reruns or evidence formats.
6. **Change public copy only where a statement becomes false.** Never announce unselected or future features.
7. **Do not list exact-copy acceptance tests (2026-10-15, #779).** When an issue carries owner-approved wording, list it under Contract constraints as wording to verify at collection. The dispatcher compares it once with the shipped catalog or copy dict.

**When implementing (the reading LLM):**

1. **Build only what the Design states.** Do not add mechanisms, defensive layers, cleanup paths, abstractions, configuration, documentation sections or "while I'm here" fixes. If something seems missing, report it instead of building it.
2. **Stop and report rather than build** when a Design item cannot be traced to an owner decision, or when a materially simpler implementation satisfies the same Requirement. List all such items at once.
3. **One focused test per acceptance item.** Do not expand into test matrices or counterexample suites the Contract does not list. TDD red/green applies to new behavior only.
4. **Prefer the smallest diff.** Leave untouched code untouched.
5. **No exact-copy tests (2026-10-15, #779).** Do not write a test or assertion whose only failure trigger is a change in prose wording: catalog strings, email or notice copy, alert and digest bodies, documentation text. To check behavior that involves copy, reference the catalog key or copy-dict value. Exceptions: forbidden-vocabulary and compliance scans, public sample figures and their provenance (#741), Paddle's required reseller notice, and structural invariants such as key parity, placeholders and section counts.

## Documentation surfaces

For documentation updates, maintain the relevant repository document; record per-issue history in that document and keep its `CLAUDE.md` index line to the subsystems it covers; update shared CLAUDE.md only when its rules are affected. Edit an existing Obsidian feature note only within the user's authorized scope. Do not automatically create companion notes or synchronize every documentation surface. Update governing sections in place when a rule changes; append incident evidence without leaving contradictory active rules. Detailed contracts and access conventions are in [Documentation governance](docs/playbooks/documentation-governance.md).

Use the configured Obsidian MCP. Keep credentials out of repository files, notes, and memory. These issue/design rules are project-wide; do not silently promote them to all projects.
