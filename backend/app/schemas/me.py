from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class MeOut(BaseModel):
    """GET /me response — issue #220, full #221 shape landed in one PR (see
    Ring 1-Onboarding.md §6: don't build a narrow schema now and widen it
    later). `missing` only ever contains "questionnaire"/"holdings" — never
    "tos"; `tos_accepted_at` is audit-only (§2.6)."""

    email: str
    delivery_email: str | None
    tos_accepted_at: datetime | None
    has_questionnaire: bool
    has_holdings: bool
    missing: list[str]
