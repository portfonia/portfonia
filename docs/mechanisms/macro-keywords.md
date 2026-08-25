# Macro keyword theme pool

### Macro keyword theme pool — widened to 17 themes (issue #129 B1 + issue #175)

`config/macro_keywords.yml` grew from the Ring 0 starting set of 8 themes
(`Portfonia Concept & Design.md` §7.1.3) to 17, across two PRs in the same
session — the B1 PR itself (7 new themes: tech breakthroughs, US Treasury,
JGB, China macro, Russia-Ukraine, the East Asia alliance, and G7/global
governance, needed because §2's rewrite above now asks the model to
*select* from the candidate pool rather than mechanically cover every
trigger — the selection is only as good as what it can select from) and a
same-day follow-up (issue #175, PR #174: 2 more new themes — US domestic
politics and China-Japan friction — plus targeted keyword additions to
several existing themes, from a product-owner-reviewed candidate list).

- **Recurring lesson across both PRs, caught by Grok review each time: bare
  generic-word keywords false-fire far more than they look like they would.**
  Every one of these was added, then caught and fixed: bare `breakthrough`
  and its Chinese-language counterpart (fires on routine tech-marketing
  headlines / any generic price-threshold phrase), bare `Europe`/`EU`/`yen`
  (fires on routine Europe-market
  roundups / ordinary Japan-FX copy), bare `sanctions` (fires on virtually
  any sanctions headline), bare `Congress` (collides with "National
  People's Congress" / "Indian National Congress"), bare `PLA` (collides
  with "Project Labor Agreement", a real term in the US construction/
  infrastructure business news this product's own RSS feeds carry). The
  fix pattern is always the same: replace the bare word with a compound,
  context-qualified phrase (`scientific breakthrough`, `EU sanctions`,
  `US Congress`, `PLA drills`/`PLA aircraft`/`PLA Navy`, etc.) — **when
  adding any new keyword to this file, default to a qualified phrase, not
  a bare single word, and ask what unrelated headline it could plausibly
  match before adding it.**
- **A single-token match can also be a *fairness* bug, not just a
  false-positive one**: `macro_event_intel.py`'s `theme_keys` are
  `sorted()`, and the daily L2 analysis cap is consumed in that sorted
  order — an ASCII-named theme (e.g. the G7/global-governance theme) sorts
  before every Chinese-named theme, so a keyword that fires too broadly on that theme
  would systematically win the shared daily L2 budget over genuinely
  rarer themes, not just miscategorize one article. Caught on bare
  `sanctions` in PR #174 round 2 review — worth remembering for any future
  ASCII-named theme.
- **`config/macro_keywords.yml` is not under `locales/`, so its Chinese
  keywords are NOT the Language Policy's carved-out exception** (see
  "Language Policy (MANDATORY)" below) — a real gap the product owner
  caught mid-PR #174. Scoped fix so far: **no new Chinese keywords added**
  to that PR's own two new themes (US domestic politics and China-Japan
  friction are English-only).
  Pre-existing Chinese keywords elsewhere in the file (Ring 0 onward,
  including the whole China A-share-policy theme) are **left as-is for now, a known,
  separately-tracked gap** — and, checked against `news_fetcher.py`'s five
  configured RSS sources (NYT/FT/Reuters-via-Google-News/CNBC/Google News
  Business, all `hl=en-US`), **currently match nothing**: there is no
  Chinese-language source in the capture pipeline today, so none of the
  file's existing Chinese keywords are actually reachable in production —
  this lowers the urgency of a cleanup (nothing regresses today either
  way) but does not make the Language Policy violation acceptable to
  extend further.
- **Provenance**: PR #174, two rounds of independent code review
  (blacktomb42) — round 1 found 2 bugs (bare `Europe`, bare `yen`) + 1
  suggestion (bare `EU`), round 2 (after fixes) found 1 bug (bare
  `sanctions`) + 2 suggestions (bare `Congress`, bare `PLA`), both rounds
  fully fixed and verified. Retroactively tracked as issue #175 (filed
  after implementation — this PR started directly from conversation, a gap
  against the Issue Tracking convention below). Merged squash `57b75c7`.
  Deployed to production 2026-08-22 alongside B1.


