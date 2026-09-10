"""Tests for section3_proportionality.py (issue #173).

Covers the module-level pure functions plus the five Contract-constraints
acceptance tests, run directly against `check_section3_proportionality`
rather than through the full report pipeline (that pipeline wiring is
integration-tested in test_report_generator.py).
"""

from __future__ import annotations

import logging

import pytest

from app.services.section3_proportionality import (
    EVIDENCE_CATEGORIES,
    HoldingCheckInput,
    check_section3_proportionality,
    expected_length_range,
    extract_section3,
    score_evidence_strength,
    segment_section3_by_holding,
)


@pytest.fixture(autouse=True)
def _reenable_logger() -> None:
    # docs/playbooks/testing-notes.md: whichever test file's alembic session
    # migrate runs first disables already-imported module loggers, so caplog
    # would otherwise see nothing whenever this file runs after one that
    # triggers a DB-backed fixture (run-order dependent — belt-and-braces
    # here rather than relying on this file always collecting first).
    logging.getLogger("app.services.section3_proportionality").disabled = False


# ---------------------------------------------------------------------------
# score_evidence_strength
# ---------------------------------------------------------------------------


def test_score_evidence_strength_no_material_is_zero() -> None:
    assert score_evidence_strength("") == 0
    assert score_evidence_strength("The stock went up today.") == 0


def test_score_evidence_strength_counts_categories_not_occurrences() -> None:
    # Three separate "customer" mentions (third_party_reliance) still count
    # as ONE category match, not three — Requirements item 2: "not from raw
    # item counts".
    text = "A major customer signed on. Another customer renewed. A third customer expanded."
    assert score_evidence_strength(text) == 1


def test_score_evidence_strength_all_five_categories() -> None:
    text = (
        "A key customer relies on the platform for its supply chain. "
        "The company continued capital expenditure through the downturn. "
        "Insiders disclosed a share buyback this quarter. "
        "The product received regulatory certification, a milestone confirmed this week. "
        "A new competitor entered, shifting market share."
    )
    assert score_evidence_strength(text) == len(EVIDENCE_CATEGORIES) == 5


def test_score_evidence_strength_matches_chinese_terms() -> None:
    text = "公司与主要客户签订长期合作协议。"
    assert score_evidence_strength(text) >= 1


# ---------------------------------------------------------------------------
# extract_section3
# ---------------------------------------------------------------------------


def test_extract_section3_slices_between_headings() -> None:
    md = "## §2 Macro Signals\n\nSome macro text.\n\n## §3 Holdings Analysis\n\nAAPL grew.\n\n## §4 Risk Radar\n\nStuff."
    assert extract_section3(md).strip() == "AAPL grew."


def test_extract_section3_to_end_of_string_when_no_next_heading() -> None:
    md = "## §3 Holdings Analysis\n\nOnly content here."
    assert extract_section3(md).strip() == "Only content here."


def test_extract_section3_missing_heading_returns_empty() -> None:
    assert extract_section3("## §2 Macro Signals\n\nNo §3 here.") == ""


# ---------------------------------------------------------------------------
# segment_section3_by_holding
# ---------------------------------------------------------------------------


def test_segment_attributes_single_holding_paragraph() -> None:
    section3 = "AAPL's supplier relationships deepened this quarter, per the filing."
    segments = segment_section3_by_holding(section3, {"AAPL": ["AAPL", "Apple"]})
    assert segments.by_identifier["AAPL"] == section3
    assert segments.mixed_paragraph_count == 0


def test_segment_excludes_mixed_paragraph_from_any_single_holding() -> None:
    section3 = "Both AAPL and MSFT benefited from the same cloud demand story this period."
    segments = segment_section3_by_holding(section3, {"AAPL": ["AAPL"], "MSFT": ["MSFT"]})
    assert segments.by_identifier["AAPL"] == ""
    assert segments.by_identifier["MSFT"] == ""
    assert segments.mixed_paragraph_count == 1


def test_segment_ignores_paragraph_naming_no_known_holding() -> None:
    section3 = "General market commentary with no holding named."
    segments = segment_section3_by_holding(section3, {"AAPL": ["AAPL"]})
    assert segments.by_identifier["AAPL"] == ""
    assert segments.mixed_paragraph_count == 0


def test_segment_concatenates_multiple_single_holding_paragraphs() -> None:
    section3 = "AAPL's first point.\n\nAAPL's second point, a different paragraph."
    segments = segment_section3_by_holding(section3, {"AAPL": ["AAPL"]})
    assert "first point" in segments.by_identifier["AAPL"]
    assert "second point" in segments.by_identifier["AAPL"]


# ---------------------------------------------------------------------------
# expected_length_range
# ---------------------------------------------------------------------------


def test_expected_length_range_increases_with_weight() -> None:
    low_min, low_max = expected_length_range(0.01, 2)
    high_min, high_max = expected_length_range(0.20, 2)
    assert high_min > low_min
    assert high_max > low_max


def test_expected_length_range_increases_with_evidence_score() -> None:
    low_min, low_max = expected_length_range(0.10, 0)
    high_min, high_max = expected_length_range(0.10, 5)
    assert high_min > low_min
    assert high_max > low_max


def test_expected_length_range_min_never_exceeds_max() -> None:
    for weight in (0.0, 0.05, 0.5, 1.0):
        for evidence in range(6):
            lo, hi = expected_length_range(weight, evidence)
            assert lo <= hi


# ---------------------------------------------------------------------------
# check_section3_proportionality — Contract constraints acceptance tests
# ---------------------------------------------------------------------------


def _full_body(section3_text: str) -> str:
    """Wrap raw §3 prose in the real §2/§3/§4 heading shape — matches what
    `check_section3_proportionality` actually receives in production
    (report_generator.py passes the full rendered body, and the function
    slices §3 out via `extract_section3`)."""
    return (
        "## §2 Macro Signals\n\nNothing notable.\n\n"
        f"## §3 Holdings Analysis\n\n{section3_text}\n\n"
        "## §4 Risk Radar\n\nNothing notable."
    )


def test_acceptance_1_high_weight_low_evidence_short_paragraph_logs_mismatch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    section3 = "AAPL had a quiet day."  # short, no structural evidence
    holding = HoldingCheckInput(
        identifier="AAPL",
        alias_terms=["AAPL"],
        weight=0.30,  # high weight
        material_text="Shares moved slightly.",  # 0-1 categories
    )
    with caplog.at_level(logging.WARNING, logger="app.services.section3_proportionality"):
        mismatches = check_section3_proportionality("report-1", _full_body(section3), [holding])
    assert mismatches == 1
    assert "AAPL" in caplog.text
    assert "report-1" in caplog.text


def test_acceptance_2_low_weight_high_evidence_long_paragraph_logs_mismatch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    long_analysis = (
        "AAPL's customer base continued to rely on its platform this period, and "
        "capital expenditure was sustained through the downturn, with insiders "
        "disclosing a buyback while a new certification milestone was confirmed "
        "and the competitive landscape shifted in its favor. " * 6
    )
    holding = HoldingCheckInput(
        identifier="AAPL",
        alias_terms=["AAPL"],
        weight=0.01,  # low weight
        material_text=(
            "A key customer relies on the platform. Capital expenditure continued "
            "through the downturn. Insiders disclosed a buyback. A certification "
            "milestone was confirmed. A new competitor shifted market share."
        ),  # all 5 categories
    )
    with caplog.at_level(logging.WARNING, logger="app.services.section3_proportionality"):
        mismatches = check_section3_proportionality(
            "report-2", _full_body(long_analysis), [holding]
        )
    assert mismatches == 1
    assert "AAPL" in caplog.text


def test_acceptance_3_in_range_produces_no_log_entry(caplog: pytest.LogCaptureFixture) -> None:
    holding = HoldingCheckInput(
        identifier="AAPL",
        alias_terms=["AAPL"],
        weight=0.10,
        material_text="A key customer relies on the platform for its supply chain.",
    )
    exp_min, exp_max = expected_length_range(0.10, score_evidence_strength(holding.material_text))
    # Fixture length deliberately sits inside [exp_min, exp_max] — pad with
    # filler sentences (no holding identifier, so they stay unattributed to
    # any OTHER holding, but padding lives in the SAME paragraph as "AAPL"
    # so it counts toward AAPL's own attributed length).
    section3 = (
        "AAPL's customer base continued to rely on its platform this period, "
        "and the read stays consistent with the prior structural thread. "
        "Nothing else about the position changed materially, and the supplied "
        "material does not extend the mechanism any further than that one "
        "relationship already described above."
    )
    assert exp_min <= len(section3) <= exp_max, (
        f"fixture length {len(section3)} must fall inside ({exp_min}, {exp_max})"
    )

    with caplog.at_level(logging.WARNING, logger="app.services.section3_proportionality"):
        mismatches = check_section3_proportionality("report-3", _full_body(section3), [holding])
    assert mismatches == 0
    assert "AAPL" not in caplog.text


def test_acceptance_4_explicit_weight_parameter_drives_the_check_not_a_real_weight(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Same §3 text, same material — only the explicit `weight` differs. This
    # simulates #421: a watched holding with weight=0 in reality gets a
    # config-driven target weight instead (issue #173 Design item 4).
    section3 = (
        "AAPL had a quiet day, nothing more to note here really, and the "
        "supplied material offers nothing further to add beyond that one line."
    )
    material = "Shares moved slightly."  # low evidence

    low_weight_holding = HoldingCheckInput(
        identifier="AAPL", alias_terms=["AAPL"], weight=0.001, material_text=material
    )
    high_weight_holding = HoldingCheckInput(
        identifier="AAPL", alias_terms=["AAPL"], weight=0.30, material_text=material
    )

    with caplog.at_level(logging.WARNING, logger="app.services.section3_proportionality"):
        low_mismatches = check_section3_proportionality(
            "report-4a", _full_body(section3), [low_weight_holding]
        )
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.services.section3_proportionality"):
        high_mismatches = check_section3_proportionality(
            "report-4b", _full_body(section3), [high_weight_holding]
        )

    # Identical text and material; only the explicit weight parameter changed
    # the verdict — proves the parameter is actually wired through the check,
    # not merely accepted and ignored.
    assert low_mismatches == 0
    assert high_mismatches == 1


def test_mixed_paragraph_excluded_holding_gets_zero_length_and_can_mismatch() -> None:
    section3 = "Both AAPL and MSFT benefited from the same story."
    holding = HoldingCheckInput(
        identifier="AAPL",
        alias_terms=["AAPL"],
        weight=0.10,
        material_text="A key customer relies on the platform.",
    )
    mismatches = check_section3_proportionality("report-5", _full_body(section3), [holding])
    assert mismatches == 1  # zero attributed length for a real, evidenced holding
