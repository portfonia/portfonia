"""Shared OpenRouter LLM helpers.

Centralizes provider pinning so every structured-extraction call routes to a
high-precision provider. OpenRouter dispatches a single model id across providers
of differing precision; aggressive-quantization resellers (NovitaAI, StreamLake)
degrade JSON/schema compliance and make output non-reproducible. Portfonia runs
low-cost open models (e.g. deepseek/deepseek-v4-flash) for cost reasons; for such
models pinning a high-precision provider is a core requirement, not optional.

Reference: Daily_Intel design doc section 8 (LLM selection & cost);
Obsidian Hermes/Portfonia/2026-06-03_Mempalace学习要点 §2.1 / §6.5.
"""

from app.core.config import get_settings


def openrouter_provider() -> dict[str, object] | None:
    """Provider routing preference for OpenRouter requests.

    Returns a dict suitable for the ``provider`` field of an OpenRouter request
    body, passed through the OpenAI SDK via ``extra_body={"provider": ...}``.
    Returns ``None`` when no provider order is configured, in which case callers
    should omit the ``provider`` field and let OpenRouter route by default.
    """
    settings = get_settings()
    order = [p.strip() for p in settings.OPENROUTER_PROVIDER_ORDER.split(",") if p.strip()]
    if not order:
        return None
    return {
        "order": order,
        "allow_fallbacks": settings.OPENROUTER_ALLOW_FALLBACKS,
    }
