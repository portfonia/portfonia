# Documentation governance

Project development and design-authoring guidance lives in the repository,
indexed by [AGENTS.md](../../AGENTS.md). This playbook retains the useful
guidance from the former duplicate Codex configuration/design notes; those
notes are retired by the owner's 2026-09-10 instruction and must not be
recreated without explicit authorization.

## Ownership, language, and scope

- Write repository content, documentation, indexes, and filenames in
  English. Do not copy Chinese note titles or paths into repository files.
- Keep engineering rules in [CLAUDE.md](../../CLAUDE.md), focused mechanisms
  in `docs/mechanisms/`, and operational guidance in `docs/playbooks/`.
  Add or update the relevant AGENTS.md index entry instead of creating a
  duplicate instruction document in the vault.
- Existing Obsidian feature notes may remain the governing product-design
  documents when the user has selected them. That does not authorize a
  second copy of project development conventions in Obsidian.
- Never create an Obsidian file without the user's explicit authorization
  to create that file. A general documentation-update request, an index
  link, a missing target, or a tool's create-on-write behavior does not
  supply that authorization. Verify that an update target exists first.
- Edit existing notes only within the authorized scope. A request affecting
  one feature is not permission to synchronize every note or add adjacent
  features, tooling, cleanup, or a broader test matrix.
- If a target is missing, pause that write and ask before creating it.
  Continue independent authorized work. Do not silently substitute a new
  filename, create an archive copy, or recreate a deliberately deleted note.
- Update active rules in place. Preserve relevant decision provenance and
  clearly label historical evidence; do not leave contradictory active
  rules or append competing versions of the same design.

## Issue and design contracts

Follow the five-comment issue structure in [AGENTS.md](../../AGENTS.md):
`Requirements`, `Reasons`, `Exploration`, `Design`, and
`Contract constraints` are separate comments. The English body is a short
summary, scope, exclusions, and index. Maintain the existing comments on
revision rather than adding a second active contract.

Write for an implementing agent that has not seen the conversation:

| Contract area | Required detail, proportional to the authorized change |
|---|---|
| Requirements | Observable behavior, affected users, scope, and exclusions. |
| Reasons | The concrete problem and the value of the requested change. |
| Exploration | Verified evidence, source/revision, alternatives, owner decisions, and unresolved questions. |
| Design | Module boundaries and caller/writer/reader flow; schema and API names, types and nullability; algorithms, units, normalization and date semantics; operation order; errors and unavailable data; UI states; compatibility and rollout; worked input/output examples; the "Public surfaces affected:" line. |
| Contract constraints | Invariants, dependencies, non-goals, concrete acceptance inputs and expected results, required validation and review, and separate deployment/data-operation authorization. |

Do not replace a design with desired outcomes or leave substantive product
or financial choices to the implementer. Resolve such ambiguity in the
governing document and issue before dependent implementation. Distinguish
the owner's confirmed decisions from a newly authored proposal; drafting a
proposal does not establish owner approval.

An implementation issue is not ready while its Design or Contract
constraints contains an unresolved substantive decision. Before starting
implementation, obtain the owner's explicit resolution of approach,
failure behavior, thresholds/policy, and data-model choices, and record it
in the existing governing comments. A future decision may remain deferred
only when the current scope has no dependency on its outcome. This is the
implementation-entry rule in AGENTS.md, preserved from PR #422; an authored
design with open choices is not an implementation-ready contract.

These dimensions are not a mandate to add subsystems or exhaustive tests.
Keep the contract proportionate to the authorized problem. When more scope
is proposed, present its value, impact/cost, and reason for owner approval
before adding it to the work.

## Public surface sync

Public surfaces are everything a visitor can see without a paid subscription.
The rule in [CLAUDE.md](../../CLAUDE.md#public-surfaces-stay-in-sync-with-the-product-mandatory-2026-10-12)
applies project-wide to every issue and every agent or harness working in the
repository. Any change to behaviour, coverage, cadence, pricing, limits, data
sources, formulas or UI updates every public surface that describes or samples
it in the same PR, in all four UI locales: `en`, `es`, `zh-Hans`, and `zh-Hant`.

### Inventory

| Surface | Source files |
|---|---|
| Homepage | `frontend/src/app/_components/home-*.tsx`, `frontend/src/app/_components/home-risk-sample.ts`, `frontend/src/app/_components/home-real-data.fixture.json`; `home` in `frontend/src/locales/{en,es,zh-Hans,zh-Hant}.json`. |
| About, Pricing, Waitlist, and legal pages | `frontend/src/app/{about,pricing,waitlist,privacy,terms,refund}`; page copy and legal catalogs in `frontend/src/locales/{en,es,zh-Hans,zh-Hant}.json`. |
| Guides index and guide pages | `frontend/src/app/guides`; guide catalogs in `frontend/src/locales/{en,es,zh-Hans,zh-Hant}.json`. |
| Public agent documentation | `frontend/src/app/agent`. |
| Login, signup, forgot-password, reset-password, verify-email, unsubscribe, and localized 404 | `frontend/src/app/{login,signup,forgot-password,reset-password,verify-email,unsubscribe}`, `frontend/src/app/not-found.tsx`; catalogs in `frontend/src/locales/{en,es,zh-Hans,zh-Hant}.json`. |
| OG / social preview images | `frontend/src/app/og/[locale]/route.tsx`; `home` and `seo.og` in `frontend/src/locales/{en,es,zh-Hans,zh-Hant}.json`. |
| SEO metadata | `frontend/src/lib/seo.ts`; `seo.pages` in `frontend/src/locales/{en,es,zh-Hans,zh-Hant}.json`. |
| Pre-subscription emails (waitlist and invitation letters, verification email) and invitation unsubscribe page | `_INVITATION_LETTER_COPY` and `_VERIFICATION_EMAIL_COPY` in `backend/app/services/email_sender.py`; page copy in `backend/app/routers/invitation_letters.py`. |

### Procedure

1. While designing, grep the inventory's source files for terms affected by the
   change: plan names and prices, cadences, markets, benchmarks, formats, limits,
   and formulas. End the issue Design with `Public surfaces affected:` listing
   each affected surface and its change, or `none` when there are no hits that
   need updating.
2. Update the affected surfaces in the same PR, in all four locales. Regenerate
   derived sample data with its documented method instead of hand-editing figures.
3. During review, check the Design line against the diff and grep the same terms
   in the public files left untouched by the diff to identify missed updates.

### Sample data and provenance

Public sample figures trace to real market data, a product formula, or a verified
public event; they are never hand-invented. A constructed sample never claims to
be a real user's report.

The homepage sample uses fictional holdings with real closes and verified public
events, anchored to the 2026-07-31 briefing date. Its provenance, product formulas,
and derivation scripts are recorded in [issue #741 Exploration](https://github.com/portfonia/portfonia/issues/741#issuecomment-6120455248).
Use that documented method when regenerating its derived figures.

## Evidence and implementation tracking

Use explicit states rather than an undifferentiated "done":

| State | Evidence required |
|---|---|
| Confirmed decision | Owner decision and its source. |
| Authored design | The proposed contract, with unresolved choices identified. |
| Existing capability | Current source revision and the actual supported behavior. |
| Integrated implementation | The caller/writer/reader chain implemented for this feature, not just a reusable component. |
| Author validation | Commands, scope, revision, and actual results produced by the implementation author. |
| Independent validation | Separate reviewer evidence, clearly distinguished from author claims. |
| Merged | The merged PR/commit; not proof of deployment. |
| Deployed or exercised | Actual deployment or runtime evidence, with the relevant authorization recorded separately. |

Check the current source and issue decisions before relying on older notes.
Do not copy old test counts, deployment claims, or stage statuses into a new
feature's ledger. If a test was not run or a phase has not started, state
that directly. Obsidian and GitHub document decisions and evidence; they
are not runtime databases or recovery stores.

Feature-specific material remains with its feature. For example, benchmark
history behavior is covered by the portfolio-performance mechanism and its
governing feature design, not by this general playbook. Do not duplicate a
dated feature example into project-wide rules.

## Obsidian access and secret handling

Use the configured Obsidian MCP. Read the existing note before an
update, preserve unrelated content, prefer a bounded edit for a small
change, and read back the result. Check structured success/error fields;
empty content is not automatically a missing note. For an explicitly
authorized deletion, preserve the necessary material at its authorized
destination first, delete only the exact requested paths, and verify their
absence without creating replacement vault files.

Do not switch to UI automation or REST because an unrelated shell/code-mode
tool fails. A different access method requires an explicit user instruction.

The configured MCP uses environment variables such as `OBSIDIAN_API_KEY`,
`OBSIDIAN_BASE_URL`, and `OBSIDIAN_ALLOW_INSECURE`. If explicitly requested,
the local REST alternative
uses `OBSIDIAN_REST_URL` (normally `https://127.0.0.1:27124`) and
`OBSIDIAN_API_KEY`; use vault-relative paths and bearer authentication.
The local endpoint uses a self-signed certificate, so the configured local
REST request may require `curl -k`; do not generalize that exception to
remote services. Sandboxed access to the local endpoint may require
escalation even when the service is healthy.

Store only variable names and usage patterns in documentation. Never print,
log, commit, or store API keys, bearer tokens, or file-retrieval secrets in
repository files, Obsidian, or memory.

## Repository delivery

Follow the isolated-worktree and PR requirements in AGENTS.md for every
repository change, including instructions and documentation. Preserve
unrelated work, keep main read-only, and submit the branch for review.
Review readiness, merge approval, deployment approval, and production data
operations remain distinct. GitHub write/review identity rules are in
[Git and review identities](git-and-review-incidents.md).
