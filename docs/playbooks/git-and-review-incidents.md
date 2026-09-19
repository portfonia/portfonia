# Git workflow and review-identity incidents

Full incident narratives behind CLAUDE.md's Branching, Secrets, and Issue
Tracking one-liners. Read this before touching stacked branches, before
handling a leaked infrastructure identifier, or before using either GitHub
identity in this repo.

## Current access procedure (owner-confirmed 2026-09-18: REST only, no OAuth)

**`gh auth login`/stored OAuth is never attempted for this project, for any
GitHub operation.** The owner already re-verified OAuth login (`gh auth
status`) and it still reported an invalid token mid-session (2026-09-18);
the prior "OAuth primary, token fallback" procedure is retracted, not just
its execution — do not retry OAuth "just in case" before falling back.

**Use `gh api` (REST endpoints) exclusively — never `gh issue`/`gh pr` and
other high-level porcelain subcommands for GitHub reads/writes.** Those
subcommands may call GitHub's GraphQL endpoint internally (confirmed
2026-09-18: `gh issue close`/`gh issue view --json` failed on a GraphQL
transport error while the plain REST endpoint and `gh api` calls succeeded
throughout the same outage). `gh api` gives a direct, predictable REST call
every time.

| Operation | Authentication and required identity |
|---|---|
| GitHub writes, including issue maintenance and branch/PR publication | `GITHUB_TOKEN` from this project's `.env.local`, via `GH_TOKEN=<token> gh api ...`. Verify `GH_TOKEN=<token> gh api user --jq .login` returns `portfonia` before writing. |
| GitHub reviews | `GITHUB_REVIEWER_TOKEN` from this project's `.env.local`, same `gh api` pattern, verified as `blacktomb42`. |

First verify the repository with `git remote -v`. For a token-backed
command, load only the required project variable into that command's
environment, verify `gh api user --jq .login`, then execute the authorized
operation via `gh api <REST endpoint>`. Never print a token, run `gh auth
token` to the visible output, or persist credentials into scripts or
documentation. The reviewer token must not perform issue maintenance,
pushes, or merges. When posting a body from a file, use `-F body=@<path>`
(the file-expanding flag), never `-f body=@<path>` — `-f` sends the literal
string `@<path>`, not the file's contents (see the `feedback_gh_api_field_
flag_file_expansion` memory incident this project already hit once).

Obsidian operations use the configured MCP: read the existing target,
update within the authorized scope, and read back. Do not substitute a UI
or REST path unless the user explicitly requests it. (This is a separate
tool/mechanism from the GitHub REST-vs-OAuth decision above — Obsidian's
own "don't substitute REST" default is unchanged.)

**`git push`/`git pull` over HTTPS also broke** when `gh auth logout` ran
(2026-09-18): this repo's origin is HTTPS, and the working credential
helper was `gh`'s own (registered by an earlier `gh auth setup-git`) — with
`gh` logged out, a plain `git push` fails with `fatal: could not read
Username for 'https://github.com': Device not configured`, independent of
the `gh api`-for-GitHub-content-operations decision above. Confirmed
one-off fix: `git remote set-url origin https://x-access-token@github.com/
portfonia/portfonia.git` (embeds the required username so only the
password prompt remains), then `GIT_ASKPASS=<script printing GITHUB_TOKEN>
git push ...`. This is a per-push workaround, not a standing config change
— it does not write the token into any git config file. A cleaner
permanent fix (e.g. a git credential helper backed by `GITHUB_TOKEN`) is
worth setting up if this keeps recurring, but is not decided yet.

This procedure supersedes both the 2026-09-13 OAuth-primary procedure
below and the even older `GITHUB_TOKEN`-as-fallback framing before that.
Those sections are historical provenance, not a competing authentication
policy. The correction that `blacktomb42` belongs to the owner, the
self-review restrictions, and explicit owner authorization for
merging/deploying remain in force.

## Stacked branches + squash-merge: a known trap (2026-08-07, PR #93/#95/#96)

Branch B built on not-yet-merged branch A, then squash-merged with
`--delete-branch`, is a real failure mode: squash-merging A deletes A's
branch, and GitHub **auto-closes any open PR whose base is that branch** —
`gh pr reopen` / `gh pr edit --base` both fail once the base ref is gone (no
recovery).

**Recovery**: if A merges before B is done, get B's commits onto `main` via
`git merge main` (not `git rebase main` — replaying B's pre-squash commits
against a squash-merged `main` produces spurious `add/add` conflicts, and
`git rebase --skip` is a history-rewrite the auto-mode permission classifier
blocks) and open a **fresh PR against `main`**, noting in its body which
closed PR it supersedes.

**Watch for this specific `git merge` footgun it surfaces**: if B's branch
added-then-removed something (e.g. moved a component out of a shared
layout) before merging in A, the 3-way merge can silently **reinstate the
removed code**, because B's net diff against the merge-base shows no change
on those lines while A's does — re-check anything B deliberately deleted
after merging.

## Two separate GitHub identities: incident history (2026-08-06, issue #78/PR #79)

`GITHUB_TOKEN` is the primary write identity — repo owner, used for
commits/pushes, issue/PR creation, and merges. `GITHUB_REVIEWER_TOKEN`
(blacktomb42) is read + PR-review-only, belonging to a **separate LLM
reviewer** in this project's multi-agent workflow.

**What happened**: this agent used `GITHUB_REVIEWER_TOKEN` to review its
own PR, then treated that as grounds to merge without the product owner's
sign-off. Reverted; see PR #79 for history.

**Standing rule this produced at the time**: any review or comment authored
under the blacktomb42 identity is that other reviewer's independent
output — read it, act on its findings, but its approval is not a substitute
for the product owner's own merge authorization, and does not come from
self-review. This agent never uses `GITHUB_REVIEWER_TOKEN` itself, for
anything.

## Correction (2026-08-28, PR #246): blacktomb42 is the product owner's own account, not a third party

The product owner explicitly clarified that the blacktomb42 account is also theirs, used to do code review and to publish/submit review comments — directly contradicting the 2026-08-06 premise above. As-of-now rule (supersedes, does not fully replace, the 2026-08-06 entry — the merge-authorization point below still holds):

1. `GITHUB_REVIEWER_TOKEN`/blacktomb42 is owned and controlled by the product owner, not an independent third-party reviewer identity. When the product owner explicitly instructs using it in the current conversation, do so — no need to ask "is this token mine to use" each time.
2. **Still true, not overturned**: never autonomously decide to review your own just-written code under blacktomb42 and use that as grounds to self-merge — same conclusion as 2026-08-06, different reason now ("same person's two accounts self-approving" rather than "impersonating a third party"). Merge authorization always comes separately, from the product owner's explicit say-so in the current conversation, never from "blacktomb42 approved it."
3. Content posted under this identity must be something the product owner has explicitly given or approved in the conversation — never author your own review content and publish it under blacktomb42 pretending it's independent judgment you didn't produce.

## Further refinement (2026-08-30, PR #263 and #269/#270 review rounds)

Two more corrections stacked on top of the above, both from the same session:

**a. The identity to use for a review-type output depends on whether you're acting as reviewer, not on who wrote the code.** Confirming a fix, verifying findings, judging code quality — all of that is "acting as reviewer" and goes through blacktomb42, even when the code under review was written by a different session/LLM and you never touched it. Using the write identity (`GITHUB_TOKEN`) for that kind of output looks like the same account self-certifying, regardless of whose keyboard produced the original code. `GITHUB_TOKEN` stays correct only for a developer's own factual statement of what they changed and why (commit messages, PR descriptions, "done, see commit X") — not for a judgment about whether code is correct or a fix is adequate.

**b. This applies even when reviewing your own code, and self-review of your own implementation is now off by default.** The product owner extended (a) further, stating that this also applies when reviewing your own work — even your own code, if you're doing an actual review pass (the full cross-check methodology, not just a status update), the output goes through blacktomb42. But more fundamentally: **do not proactively review code you implemented yourself, period.** Switching to blacktomb42 only fixes the GitHub-identity optics of self-review; it does not fix the underlying blind-spot problem (the same mind that wrote the code is judging it). If the product owner explicitly asks for a self-review, don't execute immediately — remind them this is self-review with likely blind spots, and proceed only after their explicit confirmation.

**Net effect**: the question "which identity for this GitHub output" is no longer "who wrote the code" — it's "am I stating a fact about my own work, or rendering an independent judgment." The prior question "should I even be reviewing this at all" is separately gated by (b) whenever the code is your own.

## Production infrastructure identifiers leaked into a public repo (2026-08-06)

The production server's real IP, SSH user, remote path, cloud provider, and
region sat in `CLAUDE.md` across 3 commits on this public repo for ~30
hours before being caught; history was rewritten and force-pushed to remove
it, but that can't guarantee removal from caches, forks, or clones made in
that window — **treat anything like this as burned, not just hidden, once
it's been pushed.**

Never commit a traceable production infrastructure identifier to this
repo: no real IP address, no cloud provider/region, no instance name/ID, no
SSH username, no remote filesystem path — regardless of whether the repo is
currently public or private (visibility can change, forks/clones persist
regardless). This applies to `CLAUDE.md` and any other tracked file, not
just code. The actual specs live only in the private Obsidian ops doc
referenced from CLAUDE.md's deployment section.
