"""Validated deepening rules, reloaded for every slot."""

import re
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, Field, PrivateAttr

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
    headlines_per_unit: Count
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


class BodyCleaning(BaseModel):
    paragraph_min_words: Count
    paragraph_min_cjk_chars: Count
    residue_line_patterns: list[str]
    section_block_headings: list[str]
    _patterns: list[re.Pattern[str]] = PrivateAttr(default_factory=list)
    _headings: re.Pattern[str] = PrivateAttr()

    def compile_patterns(self) -> "BodyCleaning":
        try:
            self._patterns = [re.compile(p, re.IGNORECASE) for p in self.residue_line_patterns]
            self._headings = re.compile(
                r"^#*\s*(" + "|".join(self.section_block_headings) + r")\b", re.IGNORECASE
            )
        except re.error as exc:
            raise ValueError("invalid body cleaning configuration") from exc
        return self

    def residue(self, line: str, paragraph: bool) -> bool:
        return any(
            pattern.search(line)
            for pattern in self._patterns
            if not (paragraph and pattern.pattern == r"^(published|updated)\s+\w+\s+\d")
        )

    def section(self, line: str) -> bool:
        return self._headings.match(line) is not None


class DeepenConfig(BaseModel):
    body_cleaning: BodyCleaning
    thresholds: Thresholds
    caps: Caps
    extract: ExtractRules
    excluded_domains: list[str]
    boilerplate_markers: list[str]
    paywall_markers: list[str]
    yahoo_nav_markers: list[str]


def load_intel_deepen_config(path: Path | None = None) -> DeepenConfig:
    path = path or Path(__file__).resolve().parents[2] / "config/intel_deepen.yml"
    config = DeepenConfig.model_validate(yaml.safe_load(path.read_text()))
    config.body_cleaning.compile_patterns()
    return config
