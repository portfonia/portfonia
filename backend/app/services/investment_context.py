"""Loads the per-user investor-preference data used for report generation
(issue #129 checkpoint B6). As of the 2026-08-25 correction to decision
point 6 (Ring 1-B design.md §8.5), ALL 8 questionnaire dimensions plus
locale plus free text are available for injection — the original
2026-08-21 decision to withhold `risk_appetite`/`objective` entirely was a
misreading of the product owner's intent: every stated preference matters
and should be used, with the Layer-3/4 boundary held by the prompt's SCOPE
guardrail and the output-side `_scan_forbidden_output` backstop, not by
discarding user input.
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
    """`locale` always has a value (users.locale is NOT NULL). `questionnaire`
    and `free_text` are None when the user has never submitted a
    questionnaire (§8.6's "can be skipped" means no row, not a row of
    defaults).

    `questionnaire` doubles as both the live injection source AND the value
    written into `report_inputs.investor_questionnaire_snapshot` — same
    closed-enum dict either way. `free_text` is used for live prompt
    injection but is NEVER folded into that snapshot dict: `report_inputs`
    is unencrypted JSONB, and `free_text` is the one field on
    `user_investment_context` encrypted at rest
    (app/models/user_investment_context.py).

    This is a narrower guarantee than "free_text never reaches
    report_inputs" — it inevitably does, inside the stored `pass2_prompt`/
    `assembly_prompt` text once injected, the same way holdings names and
    values already do. What the exclusion actually buys: free_text does not
    ALSO exist as its own plainly-labeled, individually queryable key
    (`investor_questionnaire_snapshot.free_text`) that a broad `report_
    inputs` scan/export could pull in bulk across every report — instead
    it sits, like every other prompt input, embedded in one long
    semi-structured blob per report. Callers must thread `free_text` as its
    own parameter, never fold it into a dict that becomes that snapshot.
    """

    locale: str
    questionnaire: dict[str, Any] | None
    questionnaire_version: str | None
    free_text: str | None


def load_investor_preferences(session: Session, user_id: uuid.UUID) -> InvestorPreferences:
    user = session.get(User, user_id)
    locale = user.locale if user is not None else "en"
    ctx = session.get(UserInvestmentContext, user_id)
    if ctx is None:
        return InvestorPreferences(
            locale=locale, questionnaire=None, questionnaire_version=None, free_text=None
        )
    return InvestorPreferences(
        locale=locale,
        questionnaire=ctx.questionnaire,
        questionnaire_version=ctx.questionnaire_version,
        free_text=ctx.free_text,
    )
