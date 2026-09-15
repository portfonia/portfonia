# Full-context code review (mandatory standard for every review mechanism)

Applies to any review performed in this repo, regardless of which tool
runs it — Grok review (`grok -p "/review --pr N" ...`), Claude Code's
`/code-review`, a Codex review, or a manual read-through. The standard is
that a review reads the PR's full context, not only its diff.

Originally authored as a Codex-specific skill
(`portfonia-full-context-review`, stored per-user in
`~/.codex/memories/skills/` — not part of this repo, and not visible to
Grok or Claude sessions on another machine). This doc is the repo-committed,
tool-agnostic version every review mechanism must follow; the Codex skill
may still exist as that tool's own invocation wrapper around it, but this
file is the source of truth for the standard itself.

## Procedure

1. Verify the exact PR head SHA, reviewer identity (`blacktomb42` via this
   project's `.env.local` `GITHUB_REVIEWER_TOKEN` — see CLAUDE.md's Issue
   Tracking section for the identity boundary), and whether an isolated
   worktree/checkout is required.
2. Read current governing docs, the issue and its comments, any prior
   reviews, and the PR description — treat a changed document as newer
   than prior memory or training data, never the reverse.
3. Map the changed code's full path: callers, writers/readers, schema/
   model, transaction ownership, CLI/task entrypoints, and
   data-deletion safety — not just the lines the diff touches.
4. Separate a PR-introduced defect from inherited surrounding behavior
   the PR did not create.
5. Reproduce the important old failure against prior behavior when
   practical, then test the changed behavior against a real dependency
   when risk warrants it (e.g. local Postgres for a SQL parameter-limit
   bug class — see `docs/playbooks/regression-notes.md`'s #194 entry for
   why this bug class needs a real-DB check, not a mock).
6. Test cross-boundary behavior: batching/pagination boundaries,
   idempotent re-runs, empty input, failure rollback, transaction
   ownership, and compatibility of any returned counts/statuses.
7. Run focused tests plus changed-file lint/format/type checks, unless
   the user explicitly says prior quality gates already ran — in that
   case prioritize semantic inspection and targeted behavioral probes
   over re-running generic gates. Report independently-run results
   separately from author-reported ones; never relabel an author's
   "tests pass" claim as independently verified.
8. Publish the actual review (a real GitHub review or PR comment, not
   only a chat summary) under the correct identity, and report its URL.

## Hard boundaries

- Review approval never authorizes merge, deployment, or a production
  data operation — name the remaining acceptance checks instead of
  inferring they are covered.
- Never substitute the repository-owner identity for an explicitly
  requested reviewer identity, or vice versa (CLAUDE.md's Issue Tracking
  section).
- A worktree-creation permission error is not licence to fall back to
  editing the main checkout — find an authorized isolated location
  instead (CLAUDE.md's Isolated worktree requirement).

## Common failure modes this standard exists to prevent

- Diff-only review that misses how the changed code is actually called,
  or that a "fix" broke an assumption a caller three files away relied on.
- Treating an author's claimed test-suite run as independently verified
  without running anything.
- Skipping a real-dependency check for a bug class that only reproduces
  against the real thing (a DB parameter limit, a provider's actual
  response shape) and trusting a mock/fake instead.
- Treating review approval as implicit merge/deploy authorization.
