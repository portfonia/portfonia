"""Admin-configurable LLM call retry/backoff tuning (#38).

Loads ``config/llm_retry.yml`` — the in-process backoff sequences
``_call_llm`` (report_llm.py) tries before re-raising a rate-limit or
connection error to the outer Celery retry. Loaded fresh on every call, no
caching, same pattern as ``asset_class_config.py`` (#35): an admin edit takes
effect on the next LLM call, no process restart.

Deliberately NOT covered here: BYOK provider order
(``_BYOK_PROVIDER_ORDER`` in report_translation.py) stays a hardcoded
constant — it is a compliance pin (issue #78/#79), not an operational
tuning knob, and changing it must go through code review + explicit
product-owner sign-off, not an unreviewed config edit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.core.config import get_settings

# backend/ = two levels above this file (services/llm_retry_config.py → app/ → backend/)
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_FILE = _BACKEND_DIR / "config" / "llm_retry.yml"

# Ceiling on any single wait — matches Celery's own default_retry_delay (the
# outer task-retry backoff), so an in-process wait here can never exceed what
# the outer retry itself is willing to wait. Also bounds sequence length: a
# long list of near-ceiling waits could otherwise still pin a worker for a
# very long time across a single _call_llm invocation (PR #142 review).
_MAX_WAIT_SECONDS = 300.0
_MAX_SEQUENCE_LENGTH = 5


@dataclass(frozen=True)
class LLMRetryConfig:
    ratelimit_backoff_seconds: tuple[float, ...]
    connect_backoff_seconds: tuple[float, ...]


def _get_config_path() -> Path:
    override = get_settings().LLM_RETRY_CONFIG_PATH
    return Path(override) if override else _DEFAULT_CONFIG_FILE


def _backoff_tuple(data: dict[str, object], key: str, path: Path) -> tuple[float, ...]:
    if key not in data:
        raise ValueError(f"llm_retry config at {path}: missing required key {key!r}")
    raw = data[key]
    if not isinstance(raw, list):
        raise ValueError(f"llm_retry config at {path}: {key!r} must be a list, got {raw!r}")
    if len(raw) > _MAX_SEQUENCE_LENGTH:
        raise ValueError(
            f"llm_retry config at {path}: {key!r} has {len(raw)} entries, "
            f"exceeds max length {_MAX_SEQUENCE_LENGTH}"
        )
    values: list[float] = []
    for item in raw:
        try:
            v = float(item)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"llm_retry config at {path}: {key!r} entry {item!r} is not a number"
            ) from exc
        if not math.isfinite(v):
            raise ValueError(f"llm_retry config at {path}: {key!r} entry {v} must be finite")
        if v < 0:
            raise ValueError(f"llm_retry config at {path}: {key!r} must not contain negative waits")
        if v > _MAX_WAIT_SECONDS:
            raise ValueError(
                f"llm_retry config at {path}: {key!r} entry {v} exceeds max wait "
                f"{_MAX_WAIT_SECONDS}s"
            )
        values.append(v)
    return tuple(values)


def load_llm_retry_config(path: Path | None = None) -> LLMRetryConfig:
    """Load + validate the LLM retry/backoff config.

    Raises ValueError on a missing key, a non-list value, a non-numeric or
    non-finite entry (inf/nan — PyYAML parses `.inf`/`.nan` as real floats,
    and `time.sleep(inf)` never returns), a negative wait, a wait above
    `_MAX_WAIT_SECONDS`, or a sequence longer than `_MAX_SEQUENCE_LENGTH` —
    a loud failure mode, not a silent fallback to hardcoded defaults.
    """
    actual = path or _get_config_path()
    with actual.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    return LLMRetryConfig(
        ratelimit_backoff_seconds=_backoff_tuple(data, "ratelimit_backoff_seconds", actual),
        connect_backoff_seconds=_backoff_tuple(data, "connect_backoff_seconds", actual),
    )
