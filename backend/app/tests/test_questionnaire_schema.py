"""Pydantic boundary validation for the B6 questionnaire (issue #129,
Ring 1-B design.md §8.6): an out-of-taxonomy value must 422, not reach the DB
CHECK constraint and surface as a raw IntegrityError/500."""

import pytest
from pydantic import ValidationError

from app.schemas.questionnaire import InvestmentContextIn, QuestionnaireIn


def _valid_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "asset_scale": "100K_500K",
        "markets": ["US", "HK"],
        "style": "GROWTH",
        "horizon": "LONG",
        "risk_appetite": "BALANCED",
        "sectors_of_interest": ["Technology", "Healthcare"],
        "objective": "GROWTH",
        "intel_focus": "MACRO",
    }
    base.update(overrides)
    return base


def test_valid_questionnaire_round_trips() -> None:
    q = QuestionnaireIn(**_valid_kwargs())
    assert q.style == "GROWTH"
    assert q.markets == ["US", "HK"]


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("asset_scale", "1M_5M"),
        ("style", "MOMENTUM"),
        ("horizon", "VERY_LONG"),
        ("risk_appetite", "YOLO"),
        ("objective", "SPECULATION"),
        ("intel_focus", "ASTROLOGY"),
    ],
)
def test_unrecognized_single_select_value_raises(field: str, bad_value: str) -> None:
    with pytest.raises(ValidationError, match="not in the closed enum"):
        QuestionnaireIn(**_valid_kwargs(**{field: bad_value}))


def test_unrecognized_market_value_raises() -> None:
    with pytest.raises(ValidationError, match="not in the closed enum"):
        QuestionnaireIn(**_valid_kwargs(markets=["US", "Mars"]))


def test_unrecognized_sector_value_raises() -> None:
    with pytest.raises(ValidationError, match="not in the closed enum"):
        QuestionnaireIn(**_valid_kwargs(sectors_of_interest=["Technology", "Crypto"]))


def test_empty_multi_select_is_allowed() -> None:
    """'No particular preference' is itself a valid answer (Concept §4.1's
    sectors example lists "无偏好" as a real option)."""
    q = QuestionnaireIn(**_valid_kwargs(sectors_of_interest=[]))
    assert q.sectors_of_interest == []


def test_investment_context_in_accepts_free_text_none() -> None:
    ctx = InvestmentContextIn(questionnaire=_valid_kwargs(), free_text=None)
    assert ctx.free_text is None


def test_investment_context_in_preserves_free_text_verbatim() -> None:
    """Concept §4.2: free text is stored/read back verbatim, no filtering."""
    text = "  Some legacy positions are inherited, not chosen.  \n多行\n说明"
    ctx = InvestmentContextIn(questionnaire=_valid_kwargs(), free_text=text)
    assert ctx.free_text == text
