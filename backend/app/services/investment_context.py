"""Loads the per-user investor-preference slice that's allowed into the
Pass 2 prompt (issue #129 checkpoint B6, decision point 6 — Ring 1-B
design.md §8.5): ONLY `locale` and `intel_focus`. `risk_appetite`/`objective`
are deliberately not read here at all — not filtered out downstream, simply
never fetched — so there is no code path that could accidentally thread them
into a prompt later.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models.user import User
from app.models.user_investment_context import UserInvestmentContext


@dataclass(frozen=True)
class InvestorPreferences:
    """`locale` always has a value (users.locale is NOT NULL). `intel_focus`
    is None when the user has never submitted a questionnaire — §8.6's
    "can be skipped" means no row, not a row of defaults."""

    locale: str
    intel_focus: str | None
    # Full questionnaire answers + version, for the report_inputs audit
    # snapshot only (§8.4) — never passed to _build_pass2_prompt. free_text
    # is deliberately excluded: report_inputs is unencrypted JSONB, and
    # free_text is the one investment-context field encrypted at rest
    # (app/models/user_investment_context.py) — mirroring it in plaintext
    # here would quietly undo that protection.
    questionnaire_snapshot: dict[str, Any] | None
    questionnaire_version: str | None


def load_investor_preferences(session: Session, user_id: uuid.UUID) -> InvestorPreferences:
    user = session.get(User, user_id)
    locale = user.locale if user is not None else "en"
    ctx = session.get(UserInvestmentContext, user_id)
    if ctx is None:
        return InvestorPreferences(
            locale=locale,
            intel_focus=None,
            questionnaire_snapshot=None,
            questionnaire_version=None,
        )
    intel_focus = ctx.questionnaire.get("intel_focus")
    return InvestorPreferences(
        locale=locale,
        intel_focus=intel_focus if isinstance(intel_focus, str) else None,
        questionnaire_snapshot=ctx.questionnaire,
        questionnaire_version=ctx.questionnaire_version,
    )
