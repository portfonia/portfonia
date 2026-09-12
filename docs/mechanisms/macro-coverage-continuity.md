# Macro coverage: eligibility, composition, and cross-report continuity

### issue #440, PR #441 (merged squash `bddfd52`) — supersedes #171

Replaces the #171-era "select 2-4 themes, each a direct-holdings-linked
paragraph" §2 contract, which the 2026-09-12 historical review (28 retained
reports, issue #440 Exploration) showed had two failure modes at once:
earlier reports stayed shallow-but-repetitive under mechanical short/
medium/long-term headings, later reports (after #171's own length-focused
fix) got shorter by stopping at naming a transmission channel and a
holding, without ever finishing the analysis. Owner-accepted design (issue
#440 Design/Contract, decision record 2026-09-12: proceed with
design-review default leanings, revisit fine thresholds only if real
reports show a problem).

## What changed

**Eligibility is independent of a direct holdings match.** A systemically
important macro development earns §2 space on its own merits;
holdings/watches/preferences now shape depth, selection, and personalized
implications, not whether the topic is eligible at all
(`report_prompts._SECTION2_INSTRUCTIONS`, and the matching inline block in
`report_assembly.build_assembly_prompt`). This closed a real gap in the A4
assembly path specifically: before this issue, assembly had NO
holdings-independent macro material at all — only the L2 shared-event
cache, itself filtered to events overlapping a held asset class
(`macro_event_exposure`). Assembly now also receives a
`_build_macro_signal_themes_block` (extracted as a shared helper in
`report_prompts.py`, used by both Pass 2 and assembly), the same
keyword-recalled candidate pool `macro_detector.py` always produced,
independent of any holdings match.

**Composition**: short current-state overview (2-3 sentences) + ONE
question-led deep anchor (3-5 paragraphs: what changed and against what
baseline, the transmission mechanism/explanation/evidence, countervailing
forces, remaining uncertainty, a concrete observable, and conditional
implications for the reader's style/watches/holdings — roughly two-thirds
macro analysis, one-third personalization) + 0-2 short independent updates
for other themes with real but lesser change. These are editorial trial
proportions (paragraph counts are the enforced, language-neutral proxy —
the zh-Hans character-count trial ranges in Design §1 can't be enforced
directly in the EN-writing prompt, since the body is translated afterward),
never hard validators — the prompt explicitly says not to pad or truncate
to fit them.

**Quiet-path narrowing** (owner decision record's explicit in-scope item):
`generate_report`'s `not macro_signals.has_any_hit and not anomalies`
shortcut to the canned quiet body now also requires zero window news AND
zero eligible prior coverage. A zero-keyword-hit report period alone no
longer qualifies as "no macro developments" — Requirements point 1 ("a
zero-theme result... does not establish that the world has no macro
developments"). In practice this makes the shortcut fire only on a
genuinely empty capture window; the canned body's own copy was rewritten
to match ("no keyword theme matched, no window news was captured, and no
continuing topic was due for revisit" — PR #441 review, blacktomb42: the
old "No macro keyword themes triggered" copy was misleading framing once
the gate stopped meaning "quiet macro world").

## Cross-report continuity: the `macro_coverage` table

New table (`app/models/macro_coverage.py`, migration
`7b5e371448f9_add_macro_coverage.py`), one row per `(report_id,
development_key)`. `app/services/macro_coverage.py` owns three
responsibilities:

1. **Read** (`load_recent_macro_coverage`): a user's own last 8 successful
   reports' coverage, most-recent row per `development_key`, self-excluding
   a given `report_id`. **Eligibility is a READ-time policy**
   (`Report.status == "success"`), not a write-time one — a
   `needs_review`/`failed` report's coverage rows are still written (for
   audit uniformity) but never surface as continuity. `development_key`
   identity is exact-match on a normalized (lowercased/trimmed)
   model-supplied slug; no fuzzy/semantic merge in this slice (Design §4
   explicitly left this "pending" — a routine, revisitable engineering
   choice, not a silently invented product threshold).
2. **Render** (`render_macro_continuity_block`): the `MACRO COVERAGE
   CONTINUITY` prompt block both Pass 2 and assembly inject, capped at 10
   development_keys / a 320-char prior-paragraph excerpt / 8 items per list
   field. Tells the model what was already covered and instructs it to say
   what changed rather than repeat the earlier treatment or manufacture
   novelty.
3. **Write**: a structured sidecar contract, not hidden inline markers
   (Design D7) — the model appends exactly one
   `<!--MACRO_COVERAGE ... MACRO_COVERAGE-->` block after §2/§3/§4
   (`build_macro_sidecar_instruction`), extracted and stripped by
   `extract_macro_sidecar` at the single render entry point in both
   `_finish_report` and `regenerate_report`, BEFORE any rendering or
   translation ever sees it. A missing sidecar (an older report, or a model
   that ignored the instruction) degrades to "no coverage, body unchanged"
   — this is also the defined compatibility path for every historical row,
   no backfill. A malformed sidecar is stripped anyway and logged, never
   raises. `persist_macro_coverage` is delete-then-insert scoped to
   `report_id`, called before `session.commit()` in the same transaction as
   the report's own status/body write — atomic, and idempotent for every
   caller (fresh generation, the #61 resume-from-prior-attempt path, and
   `regenerate_report(mode="analyze")`).

**PR #441 review fix (blacktomb42)**: the sidecar strip originally used
`re.search` (first match only) — a stray second block from the model could
survive into the rendered report. Fixed to `finditer`/`re.sub` over every
delimited block, while still parsing only the first for items (the
contract remains one sidecar per body).

## Regenerate semantics

`mode="analyze"`: self-excludes its own `report_id` from the continuity
read (Design §5 — a report can't cite its own prior coverage as history),
re-derives coverage from the fresh body pass, and REPLACES (not upserts)
existing rows for that `report_id` — verified by
`test_regenerate_analyze_replaces_macro_coverage_dropping_removed_topics`,
so a topic dropped on reanalysis leaves no stale row. `mode="render"`
touches neither research nor coverage (`test_regenerate_render_does_not_
touch_macro_coverage`) but still strips any sidecar present in the stored
body before re-rendering.

## Explicitly out of scope for this PR

- `analysis_framework.yml` (the house philosophy text) is untouched — the
  observation/supported-conditional-implication/named-unknown distinction
  (Design D6) lives entirely in the task-instruction layer instead
  (`_SECTION2_INSTRUCTIONS`). That file's own header calls for a separate
  product-owner sign-off before editing.
- No new supplementary-research trigger or Tavily budget change — the
  quiet-path was narrowed to run the EXISTING pipeline more often, not
  given a new bounded-search step of its own.
- No English/weekly/short-manual-specific budget exception, no
  `macro_coverage` retention/cleanup job, no admin/ops endpoint over a
  user's coverage history.

## Provenance

Issue #440's Design/Contract comments carry the full authored decision
register (D1-D7) and an "Implementation resolution" appendix with the
per-knob resolution detail. Independent review (blacktomb42): Approve, one
follow-up comment after a mid-review docs incident (see below), two soft
findings both fixed same-session. Full backend suite green (2387 passed, 3
skipped) plus `ruff format/check` + `mypy --strict`; no real-LLM evaluation
or production report access performed.

**Incident, fixed same session**: `gh api -f body=@file` does not expand
`@file` (only `-F`/`--field` does — `-f`/`--raw-field` posts the literal
string). A PATCH meant to update #440's Design/Contract comments with an
implementation-resolution appendix instead wiped both to the literal path
string; caught by the reviewer reading the live comment, restored in place
and verified by reading the comments back afterward. PR #441's code was
never affected, only two GitHub comments' text.
