"""Closed taxonomy for ``Report.report_type``.

Ring 0 has exactly one report type: the ADR-002 incremental report (window =
since the user's last report of this type). Ring 1 will extend this set with
monthly/weekly/daily/daily_brief cadences, each mapped to its own section set
— see the Obsidian multi-cadence report redesign notes and the "Report
Cadence Design v2" note in ``Hermes/Portfonia/Portfonia Concept & Design.md``.
Adding a new type is a code change here, not a
caller-side string literal, so the value space stays closed and every caller
(API schema, service entry point) validates against the same set.
"""

from __future__ import annotations

VALID_REPORT_TYPES: frozenset[str] = frozenset({"incremental"})


def validate_report_type(report_type: str) -> None:
    if report_type not in VALID_REPORT_TYPES:
        raise ValueError(
            f"invalid report_type {report_type!r}; must be one of {sorted(VALID_REPORT_TYPES)}"
        )
