"""Shared OpenRouter LLM helpers.

Centralizes provider pinning so every structured-extraction call routes to a
high-quality provider. OpenRouter dispatches a single model id across providers
of differing precision; aggressive-quantization providers degrade JSON/schema
compliance and make output non-reproducible. The pinned provider order is passed
via the OpenAI SDK ``extra_body`` as the OpenRouter request ``provider`` field.

Reference: Obsidian Hermes/Portfonia/2026-06-03_Mempalace学习要点 §2.1.
"""

from app.core.config import get_settings


def openrouter_provider() -> dict[str, object]:
    """Provider routing preference for OpenRouter requests.

    Returns a dict suitable for the ``provider`` field of an OpenRouter request
    body, passed through the OpenAI SDK via ``extra_body={"provider": ...}``.
    """
    settings = get_settings()
    order = [p.strip() for p in settings.OPENROUTER_PROVIDER_ORDER.split(",") if p.strip()]
    return {
        "order": order,
        "allow_fallbacks": settings.OPENROUTER_ALLOW_FALLBACKS,
    }
