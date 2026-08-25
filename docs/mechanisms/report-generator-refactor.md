# report_generator.py module split

### `report_generator.py` split into modules (issue #37)

`report_generator.py` had grown to 2657 lines mixing prompt construction,
code-built section renderers, the LLM transport, Tavily search, JSONB
serialization, the compliance output scan, translation, and orchestration.
Split, pure refactor (no behavior change — every moved function kept its
exact body, verified by the same test assertions passing before and after):

| Module | Responsibility |
|---|---|
| `app/services/report_context.py` | `ReportContext`/`ReportInputsDict` (the `report_inputs` JSONB shape) |
| `app/services/report_llm.py` | OpenRouter transport: `_openrouter_client`, `_call_llm`, `_BYOK_PROVIDER_ORDER` |
| `app/services/report_serializers.py` | ORM/dataclass → JSONB dict (`_serialize_*`) |
| `app/services/report_search.py` | Tavily search + daily-budget tracking + targeted anomaly queries |
| `app/services/report_prompts.py` | Pass 1 / Pass 2 prompt text (system prompts, `_build_pass1_prompt`/`_build_pass2_prompt`, `_stale_ticker_hint`) |
| `app/services/report_sections.py` | code-built §1/§4.2/§4.4/§2.5/footer/data-window renderers |
| `app/compliance/output_scan.py` | Layer-4 output backstop (`_scan_forbidden_output`, `_strip_markers`, `_strip_body_disclaimer`) — co-located with `forbidden_vocab.py`, not a `report_*` module, since both are the same compliance-scaffolding concern |
| `app/services/report_translation.py` | render-to-output-language pass (`_translate_md`) |
| `app/services/report_generator.py` (stays) | orchestration only — `generate_report`, `regenerate_report`, `_render_full_md`, `_is_short_manual_quiet` |

`report_generator.py` imports from all of the above (one dependency
direction, no cycle) and is still the only module `app/routers/reports.py`
and `app/tasks/report_tasks.py` import `generate_report`/`regenerate_report`
from — `LLMEmptyResponseError` moved with `_call_llm` at the time, so its
import site changed to `app.services.report_llm` (superseded by issue #55,
which moved it again to `app/services/llm_errors.py` — see that section
below for the current home). The old `test_report_generator.py`
(93 tests, 1826 lines) was redistributed to a matching test file per module
(`test_report_context.py`, `test_report_llm.py`, `test_report_serializers.py`,
`test_report_prompts.py`, `test_report_sections.py`, `test_output_scan.py`,
`test_report_translation.py`); `test_report_generator.py` keeps only the
`generate_report`/`regenerate_report` end-to-end tests. `mypy --strict`'s
`no_implicit_reexport` (part of `--strict`) caught one leftover
`rg._BYOK_PROVIDER_ORDER` test reference that only worked because
`report_generator.py` happens to import that name for its own use — fixed to
import the constant directly from its owning module, which is the general
lesson this refactor's design doc (issue #37 comment) called out: don't rely
on a symbol being reachable through another module's unrelated import, reach
for its actual owning module.

**PR #150 review (blacktomb42, Approve, 0 bugs / 3 non-blocking) fixed
`_BYOK_PROVIDER_ORDER`'s home a second time**: the first draft parked it in
`report_translation.py` (Pass 1 in `report_generator.py` then imported it
from that leaf) — the review pointed out it pins BOTH Pass 1 and translation,
so a translation-only home would let a future "translation no longer needs
BYOK" edit silently break Pass 1's hard pin. Moved to `report_llm.py` (next
to `_call_llm`'s deny/`allow_fallbacks` pairing — transport/compliance
policy, not either call site's own concern); both `report_generator.py` and
`report_translation.py` now import it from there. The review also caught a
real transcription gap this refactor's own move-verification missed: a
`limit=126` `Read` call during the test-file split landed exactly at the old
file's second-to-last line, silently dropping the final
`assert "Data Sources & Disclaimer" in report.report_md` from
`test_generate_report_quiet_day_has_footer` — restored, and cross-checked
by diffing every `assert` line across old vs. new test files (235 = 235,
content-identical modulo the `rg.` → per-module alias rename) to rule out
any other chunked-Read truncation. A third finding (stale "…above" wording
in cross-module comments naming symbols that had moved to a different file)
was fixed in `output_scan.py` and, on a proactive re-check for the same
class of staleness, in `report_prompts.py` too (not itself flagged, but the
same bug).


