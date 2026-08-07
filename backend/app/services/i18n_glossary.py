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
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from app.core.config import get_settings

# backend/ = two levels above this file (services/i18n_glossary.py → app/ → backend/)
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_GLOSSARY_FILE = _BACKEND_DIR / "config" / "i18n_glossary.yml"

# OUTPUT_LANG (env/Settings) -> locale key used inside i18n_glossary.yml.
# Extend this map, not OUTPUT_LANG's accepted values, when a new locale
# reuses an existing OUTPUT_LANG code — there is no such case yet.
_OUTPUT_LANG_TO_LOCALE: dict[str, str] = {"zh": "zh-Hans"}


@dataclass(frozen=True)
class I18nGlossary:
    supported_locales: frozenset[str]
    report_glossary: dict[str, dict[str, str]]
    forbidden_renderings: dict[str, dict[str, str]]
    ta_observation_terms: dict[str, dict[str, str]]
    vendor_names: dict[str, dict[str, str]]

    def term(self, section: dict[str, dict[str, str]], key: str, locale: str) -> str:
        """Look up one term in *section* (e.g. `self.vendor_names`) for *locale*.

        Raises KeyError if the key or locale isn't defined — a missing
        translation should fail loudly, not silently fall back to English.
        """
        if locale not in self.supported_locales:
            raise ValueError(f"locale {locale!r} is not in supported_locales")
        return section[key][locale]


def locale_for_output_lang(output_lang: str) -> str:
    """Map an OUTPUT_LANG value (e.g. 'zh') to its i18n_glossary.yml locale key."""
    return _OUTPUT_LANG_TO_LOCALE.get(output_lang, output_lang)


def _get_glossary_path() -> Path:
    override = get_settings().I18N_GLOSSARY_PATH
    return Path(override) if override else _DEFAULT_GLOSSARY_FILE


def load_i18n_glossary(path: Path | None = None) -> I18nGlossary:
    """Load the i18n_glossary.yml config. Re-reads on every call (Ring 0: no cache)."""
    target = path or _get_glossary_path()
    with target.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return I18nGlossary(
        supported_locales=frozenset(raw["supported_locales"]),
        report_glossary=raw["report_glossary"],
        forbidden_renderings=raw["forbidden_renderings"],
        ta_observation_terms=raw["ta_observation_terms"],
        vendor_names=raw["vendor_names"],
    )
