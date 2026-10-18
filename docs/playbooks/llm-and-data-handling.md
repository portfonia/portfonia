# LLM model selection + data-handling rationale

Routing and data-handling policy behind CLAUDE.md's System conventions and
Data Handling sections. Read when changing model selection or provider routing.

## Model routing (issue #793)

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

**Translation** (`report_translation.py`) uses `LOW_COST_LLM_MODEL`
(`openai/gpt-6-luna`) with `LOW_COST_LLM_REASONING_EFFORT=none`,
`data_collection=deny`, and unpinned providers. `_translate_call` requests
`service_tier=flex`; if that call fails, it makes one standard-tier call
with the same model and reasoning effort, omitting `service_tier`. If the
standard-tier call fails, its exception propagates. Both the initial
translation and truncation retry use this helper; chunking, pacing,
truncation detection and keep-source behavior are unchanged.

**Non-Jade Pass 2 analysis and analyze regenerate** use `PRIMARY_LLM_MODEL`
(`anthropic/claude-haiku-5.5`) with `PRIMARY_LLM_REASONING_EFFORT=medium`,
`data_collection=deny`, and unpinned providers.

**Jade Pass 2 analysis and analyze regenerate** continue to use
`JADE_PASS2_MODEL` (`anthropic/claude-haiku-5.5`) with
`JADE_PASS2_REASONING_EFFORT=high`, `data_collection=deny`, and unpinned
providers, selected by `is_jade(user)` at generation or rerun time (#725).

## Data handling: scheduled intelligence isolation and deny-only routing

**Scheduled intelligence isolation (enforced)**: collection and paid
deepening are performed in the scheduled intel slot. The report path reads
only URL-free accepted article bodies linked to the requesting user's
identifiers and macro themes; it performs no outbound search.
Every LLM call uses `data_collection=deny`; no call site uses BYOK.

History: #793 supersedes the #78 translation exception and #477 fallback.
