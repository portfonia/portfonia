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
from app.services.report_llm import (
    _call_llm,  # noqa: F401 — existing tests patch this module attribute
    _call_llm_byok_with_fallback,
    _openrouter_client,
)

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

# _BYOK_PROVIDER_ORDER lives in report_llm.py, next to _call_llm's deny/
# allow_fallbacks pairing (PR #150 review) — it pins BOTH Pass 1 search-query
# generation (report_generator.py) and translation (this module) via
# `_call_llm_byok_with_fallback`. `_call_llm` remains imported so existing
# tests can patch this module's name; production call sites use the helper.


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

    Routed through `_call_llm_byok_with_fallback`: the primary leg is the
    issue #78 BYOK pin (order=["DeepSeek"], allow_fallbacks=False, deny off,
    reasoning off). After that leg exhausts its retry budget on a retryable
    error, issue #477 makes one additional deny-gated marketplace call.
    Translation carries holdings-derived report text (with_holdings=True).
    """

    def _short(out: str) -> bool:
        return len(chunk) >= _TRANSLATION_MIN_SOURCE_CHARS and len(
            out.strip()
        ) < _TRANSLATION_MIN_RATIO * len(chunk)

    # allow_empty_content=True: this function already runs its own
    # truncation-detection retry (below) with a fall-back to the English
    # source (_short()) — _call_llm raising on a blank body by default
    # (PR #161 review) would replace that graceful degradation with a hard
    # failure of the whole translation pass over one chunk.
    out = _call_llm_byok_with_fallback(
        client,
        model,
        system,
        chunk,
        with_holdings=True,
        allow_empty_content=True,
    )
    if _short(out):
        logger.warning(
            "translation chunk looked truncated (%d->%d chars); retrying", len(chunk), len(out)
        )
        time.sleep(_TRANSLATION_PACING_SECONDS)
        out = _call_llm_byok_with_fallback(
            client,
            model,
            system,
            chunk,
            with_holdings=True,
            allow_empty_content=True,
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
