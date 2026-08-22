"""System default analysis framework loader (issue #128 Ring 1 stage B,
checkpoint B1 — decision point 10).

Loads ``config/analysis_framework.yml``: the product's own house analytical
stance, injected into every Pass 2 / assembly system prompt between the
compliance prefix and the shared body rules. Same admin-editable,
reload-per-call contract as ``asset_class_config.load_asset_class_config`` —
an edit takes effect on the next report, no process restart — and the same
loud-failure-over-silent-degradation posture: a report written under a
silently-empty framework would read as neutral with no one noticing.

This is the system-wide default, not a per-user preference (that is B6, a
much smaller injection scoped to ``locale``/``intel_focus`` — Ring 1-B
design.md §8.5/§1.4). ``config/analysis_framework.yml``'s own header carries
the full rationale and a pointer to the bilingual review record.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from app.core.config import get_settings

# backend/ = two levels above this file (services/analysis_framework.py -> app/ -> backend/)
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_FILE = _BACKEND_DIR / "config" / "analysis_framework.yml"


@dataclass(frozen=True)
class AnalysisFramework:
    version: str
    text: str


def _get_config_path() -> Path:
    override = get_settings().ANALYSIS_FRAMEWORK_CONFIG_PATH
    return Path(override) if override else _DEFAULT_CONFIG_FILE


def load_analysis_framework(path: Path | None = None) -> AnalysisFramework:
    """Load + validate the analysis framework config.

    Raises ValueError if the file is missing, empty, or missing/blank
    ``version``/``text`` — a silent empty-string fallback would produce a
    neutral report with no signal that the framework failed to load.
    """
    actual = path or _get_config_path()
    if not actual.exists():
        raise ValueError(f"analysis_framework config not found at {actual}")

    with actual.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"analysis_framework config at {actual} is missing a non-empty 'version'")

    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"analysis_framework config at {actual} is missing non-empty 'text'")

    return AnalysisFramework(version=version, text=text)
