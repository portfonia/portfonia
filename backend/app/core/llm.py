"""Shared OpenRouter LLM helpers.

Centralizes provider routing preferences along two axes that matter for
Portfonia's low-cost open-model strategy on a multi-tenant SaaS:

1. Precision — OpenRouter dispatches one model id across providers of differing
   precision; aggressive-quantization resellers (NovitaAI, StreamLake) degrade
   JSON/schema compliance. For DeepSeek V4 Flash pin "DigitalOcean,Venice".
2. Data policy — calls carrying user holdings must route only to providers that
   do not retain/train on the payload. ``data_collection="deny"`` makes
   OpenRouter exclude data-collecting providers, letting us use cheap DeepSeek
   without hitting its first-party API (whose terms allow training).

Reference: Daily_Intel design doc section 8 (LLM selection & cost);
product design doc §8.8 (data not used for training);
Obsidian Hermes/Portfonia/2026-06-03_Mempalace学习要点 §2.1 / §6.5.
"""

from app.core.config import get_settings


def openrouter_provider() -> dict[str, object] | None:
    """Provider routing preference for OpenRouter requests.

    Returns a dict suitable for the ``provider`` field of an OpenRouter request
    body, passed through the OpenAI SDK via ``extra_body={"provider": ...}``.
    Combines an optional precision pin (``order``) with a data-collection policy.
    Returns ``None`` only when neither is configured, in which case callers
    should omit the ``provider`` field and let OpenRouter route by default.
    """
    settings = get_settings()
    order = [p.strip() for p in settings.OPENROUTER_PROVIDER_ORDER.split(",") if p.strip()]
    provider: dict[str, object] = {"allow_fallbacks": settings.OPENROUTER_ALLOW_FALLBACKS}
    if order:
        provider["order"] = order
    if settings.OPENROUTER_DATA_COLLECTION:
        provider["data_collection"] = settings.OPENROUTER_DATA_COLLECTION
    if "order" not in provider and "data_collection" not in provider:
        return None
    return provider


# Structured (JSON-schema-required) extraction — currently holdings parsing
# only. OpenInference is the only endpoint verified (via GET
# /api/v1/models/.../endpoints) to serve google/gemma-4-31b-it at full bf16
# precision; other providers serve fp4/fp8/unknown quantization, a real
# accuracy risk for structured extraction, not just a latency/cost concern.
# Reference: Hermes/Homepage/LLM-No-Reasoning-eval设计与实现.md §19-21
# (PC611-homepage issue #27) — 21 cases x n=10 reps at 100% (210/210),
# including a JSON-structured-output generalization test.
_STRUCTURED_PINNED_ORDER = ["OpenInference"]
_STRUCTURED_PINNED_QUANTIZATIONS = ["bf16"]


def structured_provider(*, pinned: bool) -> dict[str, object]:
    """Provider routing for structured (JSON) extraction calls.

    ``pinned=True`` (first two attempts of a call site's retry sequence): locked
    to the OpenInference bf16 endpoint, hard-fail on that attempt
    (``allow_fallbacks=False``) rather than silently degrade to a
    lower-precision provider mid-attempt — precision here is an accuracy
    guarantee, not a latency knob.

    ``pinned=False`` (final attempt, after both pinned attempts failed): open/
    unpinned provider selection — issue #78 decision (2026-08-06): accept
    graceful degradation over a hard fail for this last resort.

    Unlike ``openrouter_provider()``, this never returns ``None`` — a
    structured-extraction call always carries an explicit provider preference.
    """
    settings = get_settings()
    provider: dict[str, object] = (
        {
            "order": _STRUCTURED_PINNED_ORDER,
            "quantizations": _STRUCTURED_PINNED_QUANTIZATIONS,
            "allow_fallbacks": False,
        }
        if pinned
        else {"allow_fallbacks": True}
    )
    if settings.OPENROUTER_DATA_COLLECTION:
        provider["data_collection"] = settings.OPENROUTER_DATA_COLLECTION
    return provider
