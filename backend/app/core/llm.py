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
Obsidian Portfonia archive notes, 2026-06-03 Mempalace learnings, §2.1 / §6.5.
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
# only. Previously pinned to google/gemma-4-31b-it's OpenInference bf16
# endpoint (guarding against precision-degrading quantized resellers on that
# open-weight model — see git history / issue #78 for that reasoning). Issue
# #84 (2026-08-06) moved STRUCTURED_LLM_MODEL to openai/gpt-5.6-luna, which
# routes through OpenAI's own infra rather than a marketplace of third-party
# rehosters, so there is no equivalent quantization-pin concern — open
# provider selection is the right default, not a fallback tier.
def structured_provider() -> dict[str, object]:
    """Provider routing for structured (JSON) extraction calls.

    Unlike ``openrouter_provider()``, this never returns ``None`` — a
    structured-extraction call always carries an explicit provider
    preference (at minimum the data-collection policy).
    """
    settings = get_settings()
    provider: dict[str, object] = {"allow_fallbacks": True}
    if settings.OPENROUTER_DATA_COLLECTION:
        provider["data_collection"] = settings.OPENROUTER_DATA_COLLECTION
    return provider
