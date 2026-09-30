"""Report language model contract for issue #582."""

from typing import cast

from sqlalchemy import CheckConstraint, Table

from app.models.user import VALID_REPORT_LANGUAGES, User


def test_report_language_model_values() -> None:
    assert VALID_REPORT_LANGUAGES == ("en", "zh", "zh-Hant")
    table = cast(Table, User.__table__)
    constraint = next(
        c
        for c in table.constraints
        if isinstance(c, CheckConstraint) and c.name == "ck_users_locale"
    )
    assert "zh-Hant" in str(constraint.sqltext)
