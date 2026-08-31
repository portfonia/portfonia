# Git workflow and review-identity incidents

Full incident narratives behind CLAUDE.md's Branching, Secrets, and Issue
Tracking one-liners. Read this before touching stacked branches, before
handling a leaked infrastructure identifier, or before using either GitHub
identity in this repo.

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

The product owner explicitly clarified: "blacktomb42 这个账户也是我在用，用来进行 code review 和发布/提交 review comment" — directly contradicting the 2026-08-06 premise above. As-of-now rule (supersedes, does not fully replace, the 2026-08-06 entry — the merge-authorization point below still holds):

1. `GITHUB_REVIEWER_TOKEN`/blacktomb42 is owned and controlled by the product owner, not an independent third-party reviewer identity. When the product owner explicitly instructs using it in the current conversation, do so — no need to ask "is this token mine to use" each time.
2. **Still true, not overturned**: never autonomously decide to review your own just-written code under blacktomb42 and use that as grounds to self-merge — same conclusion as 2026-08-06, different reason now ("same person's two accounts self-approving" rather than "impersonating a third party"). Merge authorization always comes separately, from the product owner's explicit say-so in the current conversation, never from "blacktomb42 approved it."
3. Content posted under this identity must be something the product owner has explicitly given or approved in the conversation — never author your own review content and publish it under blacktomb42 pretending it's independent judgment you didn't produce.

## Further refinement (2026-08-30, PR #263 and #269/#270 review rounds)

Two more corrections stacked on top of the above, both from the same session:

**a. The identity to use for a review-type output depends on whether you're acting as reviewer, not on who wrote the code.** Confirming a fix, verifying findings, judging code quality — all of that is "acting as reviewer" and goes through blacktomb42, even when the code under review was written by a different session/LLM and you never touched it. Using the write identity (`GITHUB_TOKEN`) for that kind of output looks like the same account self-certifying, regardless of whose keyboard produced the original code. `GITHUB_TOKEN` stays correct only for a developer's own factual statement of what they changed and why (commit messages, PR descriptions, "done, see commit X") — not for a judgment about whether code is correct or a fix is adequate.

**b. This applies even when reviewing your own code, and self-review of your own implementation is now off by default.** The product owner extended (a) further: "对你自己进行评审时也有效" (applies when reviewing yourself too) — even your own code, if you're doing an actual review pass (the full cross-check methodology, not just a status update), the output goes through blacktomb42. But more fundamentally: **do not proactively review code you implemented yourself, period.** Switching to blacktomb42 only fixes the GitHub-identity optics of self-review; it does not fix the underlying blind-spot problem (the same mind that wrote the code is judging it). If the product owner explicitly asks for a self-review, don't execute immediately — remind them this is self-review with likely blind spots, and proceed only after their explicit confirmation.

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
