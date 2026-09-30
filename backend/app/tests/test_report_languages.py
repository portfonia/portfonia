"""Report language model contract for issue #582."""

from sqlalchemy import CheckConstraint

from app.models.user import VALID_REPORT_LANGUAGES, User


def test_report_language_model_values() -> None:
    assert VALID_REPORT_LANGUAGES == ("en", "zh", "zh-Hant")
    constraint = next(c for c in User.__table__.constraints if isinstance(c, CheckConstraint) and c.name == "ck_users_locale")
    assert "zh-Hant" in str(constraint.sqltext)
