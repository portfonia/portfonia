"""Validated deepening rules, reloaded for every slot."""

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, Field

Positive = Annotated[float, Field(gt=0)]
Count = Annotated[int, Field(gt=0)]


class Thresholds(BaseModel):
    single_day: Positive
    d3: Positive
    d5: Positive
    near_d3: Positive
    near_d5: Positive
    news_spike_min_fresh: Count
    news_spike_ratio: Positive
    news_spike_min_history: Count
    news_spike_history_runs: Count
    weekend_min_history: Count
    weekend_macro_min_items: Count


class Caps(BaseModel):
    movers: Count
    quiet: Count
    macro_themes: Count
    macro_links_per_theme: Count
    leads_per_mover: Count
    leads_per_quiet: Count
    searches_per_run: Count
    ab_dual_top: Count
    accepted_url_skip_days: Count
    weekend_quiet: Count
    weekend_macro_themes: Count
    weekend_searches: Count


class ExtractRules(BaseModel):
    tavily_batch_max_urls: Annotated[int, Field(gt=0, le=20)]
    tavily_chunks_per_source: Annotated[int, Field(gt=0, le=5)]
    body_max_chars: Count
    min_body_chars: Count
    max_boilerplate_ratio: Annotated[float, Field(gt=0, le=1)]


class DeepenConfig(BaseModel):
    thresholds: Thresholds
    caps: Caps
    extract: ExtractRules
    excluded_domains: list[str]
    boilerplate_markers: list[str]
    paywall_markers: list[str]
    yahoo_nav_markers: list[str]


def load_intel_deepen_config(path: Path | None = None) -> DeepenConfig:
    path = path or Path(__file__).resolve().parents[2] / "config/intel_deepen.yml"
    return DeepenConfig.model_validate(yaml.safe_load(path.read_text()))
