"""Render an assembled report to the output language (#8).

Split out of report_generator.py (#37). Depends on report_llm.py for the
actual OpenRouter call.
"""

from __future__ import annotations

import logging
import time

import openai

from app.core.config import get_settings
from app.services.i18n_glossary import load_i18n_glossary, locale_for_output_lang
from app.services.report_llm import _call_llm, _openrouter_client

logger = logging.getLogger(__name__)

# A translated chunk shorter than this fraction of its source (when the source is
# non-trivial) is treated as a truncated/dropped response.
# Conservative: Chinese renders in roughly half the characters of English, so the
# threshold only catches near-empty / grossly truncated returns, not dense prose.
_TRANSLATION_MIN_RATIO = 0.25
_TRANSLATION_MIN_SOURCE_CHARS = 200

# Pause between per-chunk translation calls. A full report is ~14 chunks, each up
# to 2 calls (retry on truncation) — all against LOW_COST_LLM_MODEL. Spacing them
# out reduces the odds of tripping a shared per-model rate limit on the upstream
# provider pool (observed: 429 'temporarily rate-limited upstream' on
# deepseek-v4-flash after repeated full-pipeline runs).
_TRANSLATION_PACING_SECONDS = 2.0

# Provider preference for the unstructured/BYOK calls — Pass 1 search-query
# generation and translation (issue #78, 2026-08-06). Distinct from
# OPENROUTER_PROVIDER_ORDER (DigitalOcean,Venice — used for pinned Pass2/
# regenerate calls on PRIMARY_LLM_MODEL): pins straight to DeepSeek's own
# first-party backend via OpenRouter BYOK rather than the DigitalOcean/Venice
# marketplace pool. Both call sites also pass enforce_data_collection=False
# (see _call_llm) — a scoped compliance exception, since routing to DeepSeek's
# own API means the general OPENROUTER_DATA_COLLECTION=deny guard (which
# exists specifically to keep calls off DeepSeek's first-party endpoint) would
# otherwise exclude this provider entirely. UNLIKE _call_llm's usual
# allow_fallbacks=True default, both call sites force allow_fallbacks=False —
# a HARD pin, not a preference: since the deny guard is off for these calls,
# an open fallback on DeepSeek unavailability could silently reroute
# holdings-bearing payloads (translation ships with_holdings=True) to an
# arbitrary marketplace provider deny would normally have excluded. Fail the
# call rather than degrade the compliance guarantee (PR #79 review finding).
_BYOK_PROVIDER_ORDER = ["DeepSeek"]


def _build_glossary_instruction(target_lang: str) -> str:
    """Build the LLM glossary-instruction suffix for *target_lang* from i18n_glossary.yml.

    Returns "" for a locale with no glossary entry (mirrors the previous
    hardcoded dict's `.get(target_lang, "")` fallback).
    """
    locale = locale_for_output_lang(target_lang)
    glossary = load_i18n_glossary()
    if locale not in glossary.supported_locales:
        return ""
    pairs = "; ".join(
        f'"{en}" -> "{translations[locale]}"'
        for en, translations in glossary.report_glossary.items()
    )
    forbidden = "; ".join(
        f'"{translations[locale]}"' for translations in glossary.forbidden_renderings.values()
    )
    return (
        f" Use this exact glossary for fixed terms: {pairs}. Never render any word as {forbidden}."
    )


def _translate_md(md: str, target_lang: str) -> str:
    """Translate an assembled report to *target_lang* (#8).

    The LLM reasons in English upstream; this renders the final text in the
    user's language. Tickers, numbers, and table structure are preserved
    verbatim. 'en' is a no-op (the canonical language).

    Translation runs on the LOW_COST model, not PRIMARY: it is a mechanical
    render of already-reasoned text, so the cheaper model is sufficient and the
    expensive analysis model is reserved for Pass 2.
    """
    if target_lang == "en":
        return md
    lang_name = {"zh": "Simplified Chinese"}.get(target_lang, target_lang)
    glossary = _build_glossary_instruction(target_lang)
    settings = get_settings()
    system = (
        "You are a professional financial translator. Translate the user's Markdown "
        f"report into {lang_name}. STRICT RULES: preserve all Markdown structure, "
        "tables, and numbers exactly; keep ticker symbols, fund codes, and currency "
        "codes verbatim. Translate only natural-language prose. Do not add, remove, "
        "or reorder content, and never introduce advisory or recommendation language." + glossary
    )
    client = _openrouter_client()
    # Translate one (sub)section at a time. A single whole-report request is large
    # enough that the provider intermittently disconnects mid-response or returns a
    # truncated 200; per-(sub)section requests are small and reliable. Chunks are
    # reassembled with the exact original separators, so structure is preserved.
    chunks = _split_sections(md)
    parts: list[str] = []
    for i, chunk in enumerate(chunks):
        if not chunk.strip():
            parts.append(chunk)
            continue
        if i > 0:
            time.sleep(_TRANSLATION_PACING_SECONDS)
        parts.append(_translate_chunk(client, settings.LOW_COST_LLM_MODEL, system, chunk))
    return "\n".join(parts)


def _translate_chunk(client: openai.OpenAI, model: str, system: str, chunk: str) -> str:
    """Translate one chunk, guarding against the model silently dropping content.

    A successful HTTP 200 can still carry a truncated body (provider mid-response
    cut-off, common on the rate-limited free tier). Chinese is denser than English
    so some shrinkage is expected, but a chunk that comes back far shorter than its
    source has almost certainly lost content. Retry once; if it is still short, keep
    the English source for that chunk — a complete English section beats a silently
    dropped one (e.g. §3 vanishing from the report).

    `provider_order=_BYOK_PROVIDER_ORDER`: translation is a mechanical render on
    the low-cost model, routed via OpenRouter BYOK straight to DeepSeek's own
    backend (issue #78) rather than the pinned Pass2 marketplace pool.
    `allow_fallbacks=False` makes that pin a hard requirement — this call
    carries holdings-derived report text (with_holdings=True), so it must fail
    rather than silently reroute to an arbitrary provider if DeepSeek is
    unavailable. `enforce_data_collection=False` and `disable_reasoning=True`
    are part of the same change — see _call_llm docstring.
    """

    def _short(out: str) -> bool:
        return len(chunk) >= _TRANSLATION_MIN_SOURCE_CHARS and len(
            out.strip()
        ) < _TRANSLATION_MIN_RATIO * len(chunk)

    out = _call_llm(
        client,
        model,
        system,
        chunk,
        with_holdings=True,
        pin_provider=False,
        provider_order=_BYOK_PROVIDER_ORDER,
        allow_fallbacks=False,
        enforce_data_collection=False,
        disable_reasoning=True,
    )
    if _short(out):
        logger.warning(
            "translation chunk looked truncated (%d->%d chars); retrying", len(chunk), len(out)
        )
        time.sleep(_TRANSLATION_PACING_SECONDS)
        out = _call_llm(
            client,
            model,
            system,
            chunk,
            with_holdings=True,
            pin_provider=False,
            provider_order=_BYOK_PROVIDER_ORDER,
            allow_fallbacks=False,
            enforce_data_collection=False,
            disable_reasoning=True,
        )
    if _short(out):
        logger.error("translation chunk still truncated after retry; keeping source for this chunk")
        return chunk
    return out


def _split_sections(md: str) -> list[str]:
    """Split a report into translation chunks at section AND subsection headings.

    Breaks at both top-level ('## ') and subsection ('### ') headings, so §4 —
    which now carries two large code-built tables (§4.2 anomalies, §4.4 technical)
    plus prose subsections — becomes several small chunks instead of one oversized
    request. Smaller chunks keep the cheap, occasionally rate-limited translation
    model from returning a truncated 200 that silently drops content. The preamble
    before the first heading is its own chunk; joining the chunks with a single
    newline reproduces the original document.
    """
    chunks: list[str] = []
    current: list[str] = []
    for line in md.split("\n"):
        if (line.startswith("## ") or line.startswith("### ")) and current:
            chunks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        chunks.append("\n".join(current))
    return chunks
