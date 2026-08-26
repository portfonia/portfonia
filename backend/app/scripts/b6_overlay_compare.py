"""One-off script (issue #129 checkpoint B6, §8.5/§10.3): real-report
overlay comparison of the Pass 2 body with vs. without the INVESTOR
PREFERENCES block. Reads a real historical `report_inputs` JSONB (pulled
read-only from production, see Ring1 Dev.md session record), runs Pass 2
twice with the real PRIMARY_LLM_MODEL, writes zero rows anywhere, sends no
email. Delete the input JSON file after use — it is a real user's holdings.
"""

from __future__ import annotations

import json
import sys

from app.core.config import get_settings
from app.services.report_llm import _call_llm, _openrouter_client
from app.services.report_prompts import _build_pass2_prompt, _build_pass2_system


def main(inputs_path: str) -> None:
    with open(inputs_path, encoding="utf-8") as fh:
        inputs = json.load(fh)

    portfolio = inputs["portfolio_summary"]
    macro = inputs["macro_signals"]
    anomalies = inputs["price_anomalies"]
    search_results = inputs["search_results"]
    period_start = inputs["period_start"]
    period_end = inputs["period_end"]
    trading_days = inputs["window_trading_days"]
    holding_news = inputs.get("holding_news", {})
    large_holding_moves = inputs.get("large_holding_moves", {})

    client = _openrouter_client()
    model = get_settings().PRIMARY_LLM_MODEL
    system = _build_pass2_system()

    baseline_prompt = _build_pass2_prompt(
        portfolio,
        macro,
        anomalies,
        search_results,
        period_start,
        period_end,
        trading_days,
        holding_news,
        large_holding_moves=large_holding_moves,
    )
    injected_prompt = _build_pass2_prompt(
        portfolio,
        macro,
        anomalies,
        search_results,
        period_start,
        period_end,
        trading_days,
        holding_news,
        large_holding_moves=large_holding_moves,
        investor_locale="zh",
        investor_intel_focus="GEOPOLITICS",
    )

    print("=== BASELINE (no investor preferences — matches what shipped) ===", file=sys.stderr)
    baseline_body = _call_llm(client, model, system, baseline_prompt, with_holdings=True)
    print(baseline_body)
    print("\n\n=== WITH INVESTOR PREFERENCES (locale=zh, intel_focus=GEOPOLITICS) ===", file=sys.stderr)
    injected_body = _call_llm(client, model, system, injected_prompt, with_holdings=True)
    print(injected_body)

    with open("/tmp/b6_overlay_baseline.md", "w", encoding="utf-8") as fh:
        fh.write(baseline_body)
    with open("/tmp/b6_overlay_injected.md", "w", encoding="utf-8") as fh:
        fh.write(injected_body)
    print("\nWrote /tmp/b6_overlay_baseline.md and /tmp/b6_overlay_injected.md", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1])
