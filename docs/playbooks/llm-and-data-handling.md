# LLM model selection + data-handling rationale

Full reasoning behind the model-routing table row and the `data_collection`
exception in CLAUDE.md's System conventions / Data Handling sections.
Read this when changing model selection, provider pinning, or the
`enforce_data_collection` exception — not needed for day-to-day work.

## Model routing (issue #78, 2026-08-06)

**Structured/JSON** (holdings parsing, `holding_parser.py`, the only call
site requiring schema-compliant output) = `STRUCTURED_LLM_MODEL`
(`openai/gpt-5.6-luna`):

- Moved off `google/gemma-4-31b-it` in issue #84, 2026-08-06: the gemma pin
  to OpenInference's bf16 endpoint was itself the latency bottleneck, 371s
  worst case on a 30-row holdings file.
- `gpt-5.6-luna` measured 10.9-13.8s on the same file with 30/30 rows
  correct on manual audit — one manual run, not yet a systematic eval.
- `reasoning_effort=none` (`_STRUCTURED_REASONING_EFFORT` in
  `holding_parser.py`) — this model defaults reasoning to "medium", wasted
  cost/latency for mechanical extraction.
- Open/unpinned provider selection for both of 2 identical attempts
  (`app/core/llm.py:structured_provider`) — no precision-pin concern for
  this model, unlike gemma's third-party quantized resellers.
- `data_collection=deny` applies throughout.

**Unstructured/free-text** (translation render in `report_translation.py`) =
`LOW_COST_LLM_MODEL`
(`~deepseek/deepseek-v4-flash-latest` — leading `~` is OpenRouter's
"-latest" alias convention):

- Routed via OpenRouter BYOK straight to DeepSeek's own backend
  (`order=["DeepSeek"]`, module constant `_BYOK_PROVIDER_ORDER` in
  `report_llm.py`) with `enforce_data_collection=False` — a scoped
  compliance exception for translation only.
- **`allow_fallbacks=False` (hard pin, no marketplace fallback)**: since
  `deny` is off for these calls, an open fallback on DeepSeek unavailability
  could silently reroute the (holdings-bearing, for translation) payload to
  a training-permitting provider `deny` would normally have excluded; the
  call must fail rather than degrade that guarantee (PR #79 review finding).
- Reasoning/thinking tokens are explicitly disabled (`disable_reasoning=
  True`) since this alias defaults reasoning on unlike the non-aliased
  model.

**PRIMARY (Pass 2 analysis + regenerate) = `deepseek/deepseek-v4-pro`**,
unchanged — provider=DigitalOcean,Venice, `data_collection=deny`, no BYOK.
Sonnet/Anthropic models are NOT used here — too expensive (~$0.2/call); if
`PRIMARY_LLM_MODEL` ever shows an `anthropic/*` value it is config drift,
revert it.

## Data handling: scheduled intelligence isolation + the BYOK exception

**Scheduled intelligence isolation (enforced)**: collection and paid
deepening are performed in the scheduled intel slot. The report path reads
only URL-free accepted article bodies linked to the requesting user's
identifiers and macro themes; it performs no outbound search.
**`data_collection=deny` is applied to every report-analysis LLM call by
default**; the report path's only exception is translation.

**Exception (issue #78, 2026-08-06)**: translation render on
`LOW_COST_LLM_MODEL` passes
`enforce_data_collection=False` because they're routed via OpenRouter BYOK
straight to DeepSeek's own first-party backend (`order=["DeepSeek"]`,
`_BYOK_PROVIDER_ORDER` in `report_llm.py`), the exact provider `deny`
exists to exclude. Translation carries holdings-derived report text
(`with_holdings=True`); this was an explicit, scoped compliance tradeoff
the product owner accepted for translation only — Pass 2,
regenerate, and holdings parsing (structured extraction) all keep `deny`
enforced unchanged. Both call sites also pass `allow_fallbacks=False` (a
hard pin, not a preference) alongside the `order` pin — since `deny` is off,
an open fallback on DeepSeek unavailability could otherwise silently reroute
the payload to an arbitrary marketplace provider that `deny` would normally
have excluded; the call fails outright instead (PR #79 review finding). Do
not extend the exception to any other call site without the same explicit
sign-off, and never drop the `allow_fallbacks=False` pairing if you do.

**Second-tier fallback (issue #477):** after that translation BYOK leg exhausts
`_call_llm`'s own retry budget on a retryable error, `_call_llm_byok_with_fallback`
makes one additional independent call to `FALLBACK_LLM_MODEL`
(`openai/gpt-5.6-luna`, `FALLBACK_LLM_REASONING_EFFORT=high`) with
`enforce_data_collection=True` and no BYOK kwargs. This does not loosen the
BYOK pairing; a non-retryable primary failure (e.g. AUTH) is re-raised
without the second call.
