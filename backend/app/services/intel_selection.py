"""Deterministic global unit selection and provider assignment."""

from dataclasses import dataclass, replace
from datetime import date
from statistics import median

from app.services.intel_deepen_config import DeepenConfig
from app.services.intel_signals import Signal


@dataclass(frozen=True)
class WorkUnit:
    kind: str
    identifier: str = ""
    theme: str = ""
    reason: str = ""
    strength: float = 0
    window_start: date | None = None
    providers: tuple[str, ...] = ()


def select_units(
    signals: dict[str, Signal],
    macro_counts: dict[str, int],
    cfg: DeepenConfig,
    *,
    weekend: bool = False,
    theme_history: dict[str, list[int]] | None = None,
    window_start: date | None = None,
) -> list[WorkUnit]:
    movers = sorted(
        (s for s in signals.values() if s.mover), key=lambda s: (-s.strength, s.identifier)
    )[: cfg.caps.movers]
    quiet = [s for s in signals.values() if not s.mover and (s.filings or s.news_spike or s.near)]

    def rank(s: Signal) -> tuple[int, float, str]:
        tier = 0 if s.near else 1 if s.filings else 2
        strength = s.strength if s.near else float(s.filings) if s.filings else s.spike_ratio
        return tier, -strength, s.identifier

    quiet.sort(key=rank)
    quiet = quiet[: cfg.caps.weekend_quiet if weekend else cfg.caps.quiet]
    units = [
        WorkUnit(
            "mover", s.identifier, reason=s.reason, strength=s.strength, window_start=s.window_start
        )
        for s in movers
    ]
    for s in quiet:
        reason = (
            s.reason
            if s.near
            else "new_filing"
            if s.filings
            else f"news_spike {s.spike_ratio:.1f}x"
        )
        units.append(
            WorkUnit(
                "quiet",
                s.identifier,
                reason=reason,
                strength=-rank(s)[1],
                window_start=s.window_start,
            )
        )
    for theme, count in sorted(macro_counts.items(), key=lambda p: (-p[1], p[0])):
        history = (theme_history or {}).get(theme, [])
        if weekend and (
            count < cfg.thresholds.weekend_macro_min_items
            or (
                len(history) >= cfg.thresholds.weekend_min_history
                and count < cfg.thresholds.news_spike_ratio * median(history)
            )
        ):
            continue
        if len([u for u in units if u.kind == "macro"]) >= (
            cfg.caps.weekend_macro_themes if weekend else cfg.caps.macro_themes
        ):
            break
        units.append(
            WorkUnit(
                "macro",
                theme=theme,
                reason=f"theme {count} items",
                strength=count,
                window_start=window_start,
            )
        )
    return units


def assign_providers(
    units: list[WorkUnit], mode: str, available: set[str], cfg: DeepenConfig
) -> list[WorkUnit]:
    result = []
    alternating = 0
    wanted: tuple[str, ...]
    for index, unit in enumerate(units):
        if mode == "ab" and unit.kind == "mover" and index < cfg.caps.ab_dual_top:
            wanted = ("tavily", "parallel")
        elif mode == "ab":
            wanted = ("tavily" if alternating % 2 == 0 else "parallel",)
            alternating += 1
        else:
            wanted = (mode,)
        providers = []
        for provider in wanted:
            chosen = (
                provider
                if provider in available
                else "parallel"
                if "parallel" in available
                else "tavily"
                if "tavily" in available
                else ""
            )
            if chosen and chosen not in providers:
                providers.append(chosen)
        result.append(replace(unit, providers=tuple(providers)))
    return result
