"""OpenRouter transport: client construction + the retrying chat-completion call.

Split out of report_generator.py (#37). Deliberately has zero report-content
knowledge (no prompts, no serialization) — every other report_* module that
calls an LLM (report_generator itself, report_translation) depends on this
one, so this one must not depend back on them.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import openai

from app.core.config import OR_ATTRIBUTION_HEADERS, get_settings
from app.services.llm_retry_config import load_llm_retry_config

logger = logging.getLogger(__name__)


def _openrouter_client() -> openai.OpenAI:
    settings = get_settings()
    return openai.OpenAI(
        api_key=settings.OPENROUTER_API_KEY.get_secret_value(),
        base_url=settings.OPENROUTER_BASE_URL,
        default_headers=OR_ATTRIBUTION_HEADERS,
    )


class LLMEmptyResponseError(RuntimeError):
    """A 200 response from OpenRouter carried no usable choices.

    Observed once as a malformed 200 (`choices=None`) from a low-cost model via
    DigitalOcean/Venice — `resp.choices[0]` then raised a bare, unclassified
    TypeError. Raising a named error lets callers (and Celery's retry) treat it
    as a transient provider fault rather than a code bug. (I-DEBT-2)
    """


# Provider preference for the unstructured/BYOK calls — Pass 1 search-query
# generation (report_generator.py) and translation (report_translation.py),
# issue #78, 2026-08-06. Lives here (transport/compliance policy), not in
# either call site's own module (PR #150 review): both sites treat this as
# THE shared hard pin, so a translation-only or Pass-1-only home would let a
# future edit to that one leaf ("this module no longer needs BYOK") silently
# break the other site's pin. Distinct from OPENROUTER_PROVIDER_ORDER
# (DigitalOcean,Venice — used for pinned Pass2/regenerate calls on
# PRIMARY_LLM_MODEL): pins straight to DeepSeek's own first-party backend via
# OpenRouter BYOK rather than the DigitalOcean/Venice marketplace pool. Both
# call sites also pass enforce_data_collection=False (see _call_llm) — a
# scoped compliance exception, since routing to DeepSeek's own API means the
# general OPENROUTER_DATA_COLLECTION=deny guard (which exists specifically to
# keep calls off DeepSeek's first-party endpoint) would otherwise exclude this
# provider entirely. UNLIKE _call_llm's usual allow_fallbacks=True default,
# both call sites force allow_fallbacks=False — a HARD pin, not a preference:
# since the deny guard is off for these calls, an open fallback on DeepSeek
# unavailability could silently reroute a payload (translation ships
# with_holdings=True) to an arbitrary marketplace provider deny would
# normally have excluded. Fail the call rather than degrade the compliance
# guarantee (PR #79 review finding).
_BYOK_PROVIDER_ORDER = ["DeepSeek"]

# Backoff sequences inside _call_llm (bounded, then re-raise so the outer
# Celery retry still sees a persistent failure). (I-DEBT-2)
# Values are admin-editable via config/llm_retry.yml (#38), loaded fresh on
# every call by load_llm_retry_config() — see that module's docstring for why
# BYOK provider order is deliberately NOT included in the same config.


def _call_llm(
    client: openai.OpenAI,
    model: str,
    system: str,
    user: str,
    *,
    with_holdings: bool = False,
    pin_provider: bool = True,
    provider_order: list[str] | None = None,
    allow_fallbacks: bool | None = None,
    enforce_data_collection: bool = True,
    disable_reasoning: bool = False,
    usage_sink: list[dict[str, Any]] | None = None,
) -> str:
    """Call an OpenRouter model.  Returns the assistant content string.

    The data_collection policy (deny) is enforced on EVERY call by default as
    defense in depth: although Pass 1 is contractually holdings-free, denying
    training providers unconditionally means an accidental future holdings leak
    is still protected. `with_holdings` is retained only as an explicit intent
    marker for callers (and the test harness) — it no longer gates the data
    policy.

    `enforce_data_collection=False` opts a call OUT of the deny guard — issue
    #78 decision (2026-08-06): a scoped compliance exception for Pass 1 /
    translation only, which route via OpenRouter BYOK straight to DeepSeek's
    own first-party backend (a specific `order` pin, not the general
    marketplace pool), where the data_collection filter would otherwise exclude
    it. Every other call site keeps the default (True). Because this bypasses
    the deny guard, those same call sites MUST also pass
    `allow_fallbacks=False` (see below) — otherwise a DeepSeek outage could
    silently reroute the (holdings-bearing, for translation) payload to an
    arbitrary marketplace provider deny would normally have excluded
    (PR #79 review finding).

    `pin_provider=False` omits OPENROUTER_PROVIDER_ORDER, letting OpenRouter
    route to whichever provider is available — used for translation calls so
    repeated requests aren't all funneled through the same (possibly
    rate-limited) provider pair.

    `provider_order`, when given, sets the provider preference list directly
    (overriding `pin_provider`/`OPENROUTER_PROVIDER_ORDER`) — used to steer a
    call toward providers known to be available when the default pool is
    rate-limited.

    `allow_fallbacks`, when not None, overrides OPENROUTER_ALLOW_FALLBACKS for
    this call. Pass `False` alongside a `provider_order` pin to make that pin a
    hard requirement — the call fails outright rather than silently routing to
    an unpinned provider. Required whenever `enforce_data_collection=False`
    (the BYOK exception above only holds if the pinned provider is the only
    one that can ever be used).

    `disable_reasoning=True` suppresses reasoning/thinking tokens via
    `extra_body={"reasoning": {"enabled": False}}`. Needed for
    `~vendor/model-latest` router aliases (e.g. `~deepseek/deepseek-v4-flash-latest`)
    whose `reasoning.default_enabled` is True unlike their non-aliased
    counterparts — without this a mechanical, low-cost call silently starts
    paying for and waiting on reasoning tokens it never asked for.

    `enforce_data_collection=False` and `allow_fallbacks=False` are a required
    pair, enforced at runtime (not just by docstring/call-site discipline —
    PR #81 review): a caller cannot silently reopen the PR #79
    marketplace-fallback gap by passing the former without the latter.
    """
    extra: dict[str, Any] = {}
    settings = get_settings()
    effective_allow_fallbacks = (
        settings.OPENROUTER_ALLOW_FALLBACKS if allow_fallbacks is None else allow_fallbacks
    )
    if not enforce_data_collection and effective_allow_fallbacks is not False:
        raise ValueError(
            "_call_llm: enforce_data_collection=False requires allow_fallbacks=False "
            "explicitly — an open fallback with the deny guard off can silently reroute "
            "a holdings-bearing payload to an arbitrary marketplace provider (PR #79/#81 review)."
        )
    provider: dict[str, object] = {"allow_fallbacks": effective_allow_fallbacks}
    if provider_order:
        provider["order"] = provider_order
    elif pin_provider:
        order = [p.strip() for p in settings.OPENROUTER_PROVIDER_ORDER.split(",") if p.strip()]
        if order:
            provider["order"] = order
    if enforce_data_collection and settings.OPENROUTER_DATA_COLLECTION:
        provider["data_collection"] = settings.OPENROUTER_DATA_COLLECTION
    # An explicitly-passed allow_fallbacks (a deliberate hard pin, e.g. False)
    # must never be silently dropped just because it's the only key present —
    # that would strip the pin from the actual request (PR #81 review).
    if provider.keys() - {"allow_fallbacks"} or allow_fallbacks is not None:
        extra["provider"] = provider
    if disable_reasoning:
        extra["reasoning"] = {"enabled": False}

    retry_config = load_llm_retry_config()
    resp: Any = None
    _rate_limit_attempts = 0
    _connect_attempts = 0
    for _attempt in range(
        max(
            len(retry_config.ratelimit_backoff_seconds),
            len(retry_config.connect_backoff_seconds),
        )
        + 1
    ):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.3,
                extra_body=extra if extra else None,
            )
            break
        except openai.RateLimitError:
            backoff_idx = _rate_limit_attempts
            _rate_limit_attempts += 1
            if backoff_idx >= len(retry_config.ratelimit_backoff_seconds):
                logger.warning("llm call: model=%s exhausted 429 backoff retries", model)
                raise
            wait = retry_config.ratelimit_backoff_seconds[backoff_idx]
            logger.warning(
                "llm call: model=%s got 429 (attempt %d), backing off %.0fs",
                model,
                backoff_idx + 1,
                wait,
            )
            time.sleep(wait)
        except openai.APIConnectionError:
            backoff_idx = _connect_attempts
            _connect_attempts += 1
            if backoff_idx >= len(retry_config.connect_backoff_seconds):
                logger.warning("llm call: model=%s exhausted connection backoff retries", model)
                raise
            wait = retry_config.connect_backoff_seconds[backoff_idx]
            logger.warning(
                "llm call: model=%s connection error (attempt %d), backing off %.0fs",
                model,
                backoff_idx + 1,
                wait,
            )
            time.sleep(wait)

    # OpenRouter has been observed returning a 200 with choices=None for some
    # providers; guard before indexing so this surfaces as a classified,
    # retryable error rather than a bare TypeError.
    if not resp.choices:
        raise LLMEmptyResponseError(
            f"model={model} resp_model={getattr(resp, 'model', '?')} returned no choices"
        )
    choice = resp.choices[0]
    usage = resp.usage
    logger.info(
        "llm call: model=%s resp_model=%s finish_reason=%s "
        "tokens=prompt:%s,completion:%s,total:%s cost=%s",
        model,
        resp.model,
        choice.finish_reason,
        usage.prompt_tokens if usage else None,
        usage.completion_tokens if usage else None,
        usage.total_tokens if usage else None,
        getattr(usage, "cost", None) if usage else None,
    )
    if choice.finish_reason not in ("stop", None):
        logger.warning(
            "llm call: model=%s finished with reason=%r (possible truncation)",
            model,
            choice.finish_reason,
        )
    if usage_sink is not None:
        usage_sink.append(
            {
                "model": model,
                "resp_model": resp.model,
                "finish_reason": choice.finish_reason,
                "prompt_tokens": usage.prompt_tokens if usage else None,
                "completion_tokens": usage.completion_tokens if usage else None,
                "total_tokens": usage.total_tokens if usage else None,
                "cost": getattr(usage, "cost", None) if usage else None,
            }
        )
    content = choice.message.content or ""
    return content.strip()
