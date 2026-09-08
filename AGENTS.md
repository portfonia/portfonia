# Portfonia — Codex Project Instructions

Read `CLAUDE.md` for the shared repository engineering, security, testing, and Git conventions. This file adds the Codex issue/design documentation contract.

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

Contract constraints must enumerate invariants, non-goals, dependencies, acceptance tests with concrete expected results, required validation/review gates, and deployment/data-operation authorization boundaries. A list of desired outcomes or “implementer should decide” is not an implementation contract.

Distinguish confirmed product decisions, the authored implementation design, unresolved questions, and shipped behavior. Never invent approval. Resolve substantive financial/product ambiguity in the governing design and issue before dependent implementation.

## Documentation surfaces

For documentation updates, maintain this AGENTS.md, shared CLAUDE.md where applicable, the relevant feature note, and Obsidian `Hermes/Portfonia/Codex开发配置文档.md` / `Hermes/Portfonia/Codex设计文档.md`. Update governing sections in place when a rule changes; append incident evidence without leaving contradictory active rules.

Use the configured Obsidian MCP first. Keep credentials out of repository files, notes, and memory. These issue/design rules are project-wide; do not silently promote them to all projects.

