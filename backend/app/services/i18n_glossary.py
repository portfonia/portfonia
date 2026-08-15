"""Locale-keyed glossary of fixed non-English terms (issue #90).

Loads ``config/i18n_glossary.yml`` — every literal CJK (or other non-English)
string this system renders, forbids, or matches on. English-only prose
(CLAUDE.md, docstrings, comments) references terms by their EN key here
instead of embedding literal characters inline; only this file and the
runtime code paths that actually need to emit/match a translated string
carry non-English characters.

Schema is locale-keyed, not tied to one target language: adding a locale is
a config addition (a new column of human-verified translations plus an
entry in `_OUTPUT_LANG_TO_LOCALE` below), not a schema rewrite. Only
`zh-Hans` is populated today. `OUTPUT_LANG` (env/Settings) keeps its current
`zh` value — this module maps it to the `zh-Hans` glossary key rather than
requiring an env/config rename.

`app/compliance/forbidden_vocab.py` already declares itself the single
source of truth for compliance-scan vocabulary (hand-tuned context-aware
regex); this module does not duplicate it.

**Lifecycle — NOT uniformly "live" (PR #91 review):** `load_i18n_glossary()`
itself always re-reads the YAML fresh (no cache), same as
`asset_class_config.py`. But that parity stops at the call site: functions
that call it *inside their own body* (`_build_footer`, `_stale_ticker_hint`
in `report_sections.py`/`report_prompts.py`, `_build_glossary_instruction`
in `report_translation.py`) pick up an edit on the next call, with no
restart needed. Consumers that instead bake the result into a **module-level
constant** at import (`report_sections._RELEASE_DELAY_TERMS`,
`output_scan._STRAY_TAGS`/`_BODY_DISCLAIMER_RE` in
`app/compliance/output_scan.py`, and the §4.2 cross-reference text folded
into `report_prompts._PASS2_SYSTEM`) freeze whatever the YAML said at
process start — an admin edit to those specific lists needs a
uvicorn/celery restart to take effect, per this project's existing "Dev
process restart" convention in CLAUDE.md. This is a real inconsistency, not
a design choice; if it matters enough to fix, the correct fix is loading
fresh inside those functions too, not patching the docstring further.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.core.config import get_settings

# backend/ = two levels above this file (services/i18n_glossary.py → app/ → backend/)
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_GLOSSARY_FILE = _BACKEND_DIR / "config" / "i18n_glossary.yml"

# OUTPUT_LANG (env/Settings) -> locale key used inside i18n_glossary.yml.
# Extend this map, not OUTPUT_LANG's accepted values, when a new locale
# reuses an existing OUTPUT_LANG code — there is no such case yet.
_OUTPUT_LANG_TO_LOCALE: dict[str, str] = {"zh": "zh-Hans"}

# Sections shaped {key: {locale: str}} — every key must carry every
# supported_locales entry (report_glossary/forbidden_renderings/
# ta_observation_terms/vendor_names all have this shape; `templates` is
# validated separately below since it also carries "en", the implicit
# source locale, not a translation target in supported_locales).
_LOCALE_SECTIONS: tuple[str, ...] = (
    "report_glossary",
    "forbidden_renderings",
    "ta_observation_terms",
    "vendor_names",
)

# Flat matching-term lists: an empty one silently weakens or disables the
# regex/scan it backstops (PR #91 review) rather than raising anywhere near
# the mistake, so these fail loudly at load time instead.
_NON_EMPTY_LIST_FIELDS: tuple[str, ...] = (
    "release_delay_terms_zh",
    "legacy_removed_markers_zh",
    "body_disclaimer_regex_terms_zh",
)


@dataclass(frozen=True)
class I18nGlossary:
    supported_locales: frozenset[str]
    report_glossary: dict[str, dict[str, str]]
    forbidden_renderings: dict[str, dict[str, str]]
    ta_observation_terms: dict[str, dict[str, str]]
    vendor_names: dict[str, dict[str, str]]
    templates: dict[str, dict[str, str]]
    release_delay_terms_zh: tuple[str, ...]
    legacy_removed_markers_zh: tuple[str, ...]
    body_disclaimer_regex_terms_zh: tuple[str, ...]


def locale_for_output_lang(output_lang: str) -> str:
    """Map an OUTPUT_LANG value (e.g. 'zh') to its i18n_glossary.yml locale key."""
    return _OUTPUT_LANG_TO_LOCALE.get(output_lang, output_lang)


def _get_glossary_path() -> Path:
    override = get_settings().I18N_GLOSSARY_PATH
    return Path(override) if override else _DEFAULT_GLOSSARY_FILE


def _validate(raw: dict[str, Any], supported_locales: frozenset[str]) -> None:
    """Fail loudly on structural gaps a bad YAML edit could otherwise introduce silently."""
    for section_name in _LOCALE_SECTIONS:
        section = raw[section_name]
        for key, locales in section.items():
            missing = supported_locales - locales.keys()
            if missing:
                raise ValueError(
                    f"i18n_glossary.yml: {section_name}[{key!r}] is missing "
                    f"locale(s) {sorted(missing)}"
                )
    required_template_locales = supported_locales | {"en"}
    for key, locales in raw["templates"].items():
        missing = required_template_locales - locales.keys()
        if missing:
            raise ValueError(
                f"i18n_glossary.yml: templates[{key!r}] is missing locale(s) {sorted(missing)}"
            )
    for field in _NON_EMPTY_LIST_FIELDS:
        value = raw[field]
        # isinstance(..., list) first: a bare scalar string (e.g. a YAML edit
        # that drops the "- " list-item dash) is iterable too, and "|".join()
        # over it silently character-splits into alternations instead of
        # raising — the exact footgun this validation exists to catch
        # (PR #91 re-review). Same reasoning for rejecting "" elements: a
        # ["", "投资建议"] list is non-empty but still yields a leading empty
        # regex alternative once joined.
        if not isinstance(value, list) or not value:
            raise ValueError(
                f"i18n_glossary.yml: {field} must be a non-empty list — this "
                f"backstops a live regex/scan and an empty or non-list value "
                f"would silently weaken or disable it"
            )
        if not all(isinstance(item, str) and item for item in value):
            raise ValueError(f"i18n_glossary.yml: {field} must contain only non-empty strings")
    # body_disclaimer_regex_terms_zh entries contain regex syntax (alternation,
    # wildcards), unlike the two plain-literal lists — compile eagerly so a
    # malformed edit fails here, not the first time a report body hits it.
    re.compile("|".join(raw["body_disclaimer_regex_terms_zh"]))


def load_i18n_glossary(path: Path | None = None) -> I18nGlossary:
    """Load the i18n_glossary.yml config. Re-reads on every call (Ring 0: no cache)."""
    target = path or _get_glossary_path()
    with target.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    supported_locales = frozenset(raw["supported_locales"])
    _validate(raw, supported_locales)
    return I18nGlossary(
        supported_locales=supported_locales,
        report_glossary=raw["report_glossary"],
        forbidden_renderings=raw["forbidden_renderings"],
        ta_observation_terms=raw["ta_observation_terms"],
        vendor_names=raw["vendor_names"],
        templates=raw["templates"],
        release_delay_terms_zh=tuple(raw["release_delay_terms_zh"]),
        legacy_removed_markers_zh=tuple(raw["legacy_removed_markers_zh"]),
        body_disclaimer_regex_terms_zh=tuple(raw["body_disclaimer_regex_terms_zh"]),
    )
