"""Issue #735 English report-language default."""

import pytest

from app.core.config import Settings


def test_acceptance_01_output_language_defaults_to_english(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OUTPUT_LANG", raising=False)
    required_config = Settings().model_dump()
    required_config.pop("OUTPUT_LANG")
    assert Settings(_env_file=None, **required_config).OUTPUT_LANG == "en"
