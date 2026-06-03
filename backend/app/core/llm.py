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
