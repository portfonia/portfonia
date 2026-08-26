"""One-off script (issue #129 checkpoint B6, §8.5/§10.3): real-report
overlay comparison of the Pass 2 body with vs. without the INVESTOR
PREFERENCES block. Reads a real historical `report_inputs` JSONB (pulled
read-only from production, see Ring1 Dev.md session record), runs Pass 2
twice with the real PRIMARY_LLM_MODEL, writes zero rows anywhere, sends no
email.

PRIVACY (PR #212 review finding): both the INPUT JSON and the two OUTPUT
report bodies are derived from a real user's holdings. The outputs are
written to a fresh `tempfile.mkdtemp()` directory (mode 0700, not the
predictable `/tmp/b6_overlay_*` paths an earlier version used), and this
script never leaves it lying around — the printed path is for immediate use
in this session only. Delete the input JSON file AND the printed output
directory once you are done reading the results; `/tmp` persists across
reboots and is typically world-readable by default umask.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.services.report_llm import _call_llm, _openrouter_client
from app.services.report_prompts import _build_pass2_prompt, _build_pass2_system

# Representative example answers for the "injected" run — not any real
# user's questionnaire (none exists in production as of this script's
# authoring). Exercises all 8 dimensions, including the two
# (risk_appetite/objective) the 2026-08-21 decision originally withheld.
_EXAMPLE_QUESTIONNAIRE: dict[str, Any] = {
    "asset_scale": "500K_2M",
    "markets": ["US", "HK"],
    "style": "GROWTH",
    "horizon": "LONG",
    "risk_appetite": "AGGRESSIVE",
    "sectors_of_interest": ["Technology"],
    "objective": "GROWTH",
    "intel_focus": "GEOPOLITICS",
}
_EXAMPLE_FREE_TEXT = (
    "I've been adding to my AI infrastructure names on purpose — that's not "
    "a mistake, it's the core thesis. I'm less interested in dividend plays."
)


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
        investor_questionnaire=_EXAMPLE_QUESTIONNAIRE,
        investor_free_text=_EXAMPLE_FREE_TEXT,
    )

    print("=== BASELINE (no investor preferences — matches what shipped) ===", file=sys.stderr)
    baseline_body = _call_llm(client, model, system, baseline_prompt, with_holdings=True)
    print(baseline_body)
    print("\n\n=== WITH INVESTOR PREFERENCES (all 8 dimensions + free text) ===", file=sys.stderr)
    injected_body = _call_llm(client, model, system, injected_prompt, with_holdings=True)
    print(injected_body)

    out_dir = Path(tempfile.mkdtemp(prefix="b6_overlay_"))
    out_dir.chmod(0o700)
    (out_dir / "baseline.md").write_text(baseline_body, encoding="utf-8")
    (out_dir / "injected.md").write_text(injected_body, encoding="utf-8")
    print(f"\nWrote {out_dir}/baseline.md and {out_dir}/injected.md", file=sys.stderr)
    print(
        "Delete this directory once you are done reading — it holds a real "
        "user's holdings-derived report content.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main(sys.argv[1])
