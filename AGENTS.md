# Portfonia — Codex Project Instructions

Read `CLAUDE.md` for the shared repository engineering, security, testing, and Git conventions. This file adds the Codex issue/design documentation contract.

## Documentation index

- [Documentation governance](docs/playbooks/documentation-governance.md): document ownership, Obsidian authorization, executable design contracts, evidence labels, and note-access conventions.
- [Shared engineering rules](CLAUDE.md): language, security, testing, and deployment conventions.
- [Git and review identities](docs/playbooks/git-and-review-incidents.md): write/review identity boundaries and workflow history.

## Language and Obsidian authorization (mandatory)

- Repository content and filenames must be English. Do not put Chinese prose, note titles, or filenames in repository instructions, documentation, or indexes.
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

### Vigil documentation

Vigil product requirements and decisions live in Obsidian
`Hermes/Portfonia/Vigil Concept & Design.md`; Ring 0 phase contracts,
dependencies, acceptance evidence, and implementation tracking live in
`Hermes/Portfonia/Vigil_R0_Dev.md`. Maintain the governing sections in place.
Distinguish existing Portfonia capabilities from Vigil integration, authored
design from owner-approved decisions, and merged code from deployment or
real delivery evidence. Do not count reusable modules as completed Vigil
stages or treat a documentation update as authorization to implement,
deploy, or release entrusted files.

For documentation updates, maintain the relevant repository document and its AGENTS.md index entry; update shared CLAUDE.md only when its rules are affected. Edit an existing Obsidian feature note only within the user's authorized scope. Do not automatically create companion notes or synchronize every documentation surface. Update governing sections in place when a rule changes; append incident evidence without leaving contradictory active rules. Detailed contracts and access conventions are in [Documentation governance](docs/playbooks/documentation-governance.md).

Use the configured Obsidian MCP first. Keep credentials out of repository files, notes, and memory. These issue/design rules are project-wide; do not silently promote them to all projects.
