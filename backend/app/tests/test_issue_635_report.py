"""Dedicated per-attempt report acceptance tests."""

from datetime import timedelta
from unittest.mock import patch

import yaml
from sqlalchemy.orm import Session

from app.models.intel import IntelCollectionRun, IntelSlotRun
from app.services import intel_digest as digest
from app.services.macro_detector import _get_keywords_path
from app.services.news_capture import PoolCaptureResult
from app.tasks import intel_tasks as task
from app.tests.test_intel_deepen_rules import NOW
from app.tests.test_intel_paid import slot


def report(session: Session, run: IntelSlotRun) -> tuple[str, str, str]:
    return digest.build_batch_report(session, run)


def collection(
    session: Session,
    run: IntelSlotRun,
    stats: dict[str, object],
    *,
    earlier: bool = False,
    errors: list[str] | None = None,
) -> None:
    session.add(
        IntelCollectionRun(
            kind="instrument",
            slot_run_id=run.id,
            started_at=NOW - timedelta(minutes=1) if earlier else NOW,
            finished_at=NOW + timedelta(seconds=10),
            status="ok",
            stats=stats,
            errors=errors or [],
            instruments_total=1,
            instruments_processed=1,
        )
    )
    session.flush()


def test_635_10_only_current_attempt_runs(db_session: Session) -> None:
    run = slot(db_session)
    run.status = "ok"
    run.finished_at = NOW + timedelta(seconds=10)
    previous = IntelSlotRun(
        slot="post_close",
        run_date=NOW.date() - timedelta(days=1),
        started_at=NOW - timedelta(days=1),
        status="failed",
        details={},
    )
    db_session.add(previous)
    db_session.flush()
    collection(db_session, run, {"finnhub": {"items": 3, "calls": 1}})
    collection(
        db_session,
        previous,
        {"finnhub": {"items": 70, "calls": 7}},
        errors=["finnhub: key not set"],
    )
    collection(
        db_session,
        run,
        {"finnhub": {"items": 900, "calls": 9}},
        earlier=True,
        errors=["finnhub: HTTPStatusError HTTP 503"],
    )
    _, body, severity = report(db_session, run)
    assert "Finnhub ........... 3 (1 requests)" in body
    assert "API key is not configured" not in body and "503" not in body
    assert "Problems: none" in body and severity == "INFO"


def test_635_11_plain_names_and_merged_counts(db_session: Session) -> None:
    run = slot(db_session)
    collection(
        db_session,
        run,
        {
            "cleaning": {
                "unrelated_rule": 2,
                "unrelated_llm": 3,
                "duplicate": 4,
                "duplicate_llm": 5,
                "low_value_rule": 6,
                "promo_llm": 7,
            },
            "classifier": {},
            "cleaning_samples": {
                "unrelated_rule": ["Sample one"],
                "unrelated_llm": ["Sample one", "Sample two"],
            },
        },
    )
    subject, body, _ = report(db_session, run)
    for internal in [
        "duplicate_llm",
        "unrelated_rule",
        "promo_llm",
        "low_value_rule",
        "Per-source",
        "capture-news",
        "cleaning",
        "classifier",
    ]:
        assert internal not in subject + body
    assert "Do not name the company ................ 5" in body
    assert "Same story as another headline, reworded . 9" in body
    assert "Stock picks, promotion, routine price recaps 13" in body
    assert body.count('"Sample one"') == 1


def test_635_12_all_macro_names_ascii_and_displayed(db_session: Session) -> None:
    themes = yaml.safe_load(_get_keywords_path().read_text())["themes"]
    assert len(themes) == 17
    assert all(
        isinstance(t.get("name_en"), str) and t["name_en"] and t["name_en"].isascii()
        for t in themes
    )
    run = slot(db_session)
    run.details = {
        "deepening": {
            "selections": [
                {"kind": "macro", "theme": themes[0]["name"], "reason": "theme 11 items"}
            ],
            "outcomes": [
                {
                    "kind": "macro",
                    "theme": themes[0]["name"],
                    "provider": "tavily",
                    "via": "direct",
                    "accepted": 3,
                }
            ],
        }
    }
    assert "Monetary policy (11 matching headlines)" in report(db_session, run)[1]


def test_635_13_stale_and_rerun_send_distinct_attempt_keys(db_session: Session) -> None:
    early = NOW.replace(hour=14, minute=0)
    with (
        patch.object(task, "SessionLocal", return_value=db_session),
        patch.object(task, "now_et", return_value=early) as clock,
        patch.object(task, "today_et", return_value=NOW.date()),
        patch.object(task, "resolve_profiles", return_value=[]),
        patch.object(task, "capture_news", return_value=PoolCaptureResult(0, [])),
        patch.object(task, "collect_slot_news", return_value=IntelCollectionRun(status="ok")),
        patch.object(task, "load_intel_deepen_config", side_effect=ValueError("fixture")),
        patch.object(task, "send_ops_alert", return_value=True) as send,
    ):
        assert task.intel_slot_task("post_close") == {"status": "stale_trigger"}
        assert send.call_count == 1
        run = db_session.query(IntelSlotRun).one()
        first = send.call_args
        assert first.kwargs["idempotency_key"] == f"intel-report-{run.id}-{int(early.timestamp())}"
        assert first.kwargs["severity"] == "WARNING"
        assert first.args[1].startswith("Batch did not run: the trigger arrived too late (")
        clock.return_value = NOW
        assert task.intel_slot_task("post_close") == {"status": "partial"}
    assert send.call_count == 2 and db_session.query(IntelSlotRun).one().id == run.id
    assert (
        send.call_args.kwargs["idempotency_key"] == f"intel-report-{run.id}-{int(NOW.timestamp())}"
    )
    assert first.kwargs["idempotency_key"] != send.call_args.kwargs["idempotency_key"]


def test_635_14_manual_subject(db_session: Session) -> None:
    run = slot(db_session)
    run.details = {"trigger": "manual"}
    assert "(manual)" in report(db_session, run)[0]


def test_635_15_kept_counts_articles_and_filings(db_session: Session) -> None:
    run = slot(db_session)
    collection(
        db_session,
        run,
        {
            "yahoo": {"inserted": 2},
            "sec": {"inserted": 1},
            "cleaning": {"kept": 5, "filings_stored": 2},
        },
    )
    assert (
        "Kept: 3 headlines and 2 company filings (3 of them new to the database)."
        in report(db_session, run)[1]
    )


def example_report(session: Session) -> str:
    """An English fixture report for the PR, with current-attempt evidence."""
    run = slot(session)
    run.status = "ok"
    run.finished_at = NOW + timedelta(seconds=98)
    collection(
        session,
        run,
        {
            "markets": {"US": {"processed": 1, "total": 1}},
            "finnhub": {"items": 4, "calls": 1, "inserted": 2},
            "cleaning": {"kept": 2, "unrelated_rule": 1, "duplicate_earlier": 1},
            "classifier": {"items": 2, "batches": 1, "cost_usd": 0.001, "failed_batches": 0},
        },
    )
    run.details = {
        "deepening": {
            "selections": [{"kind": "quiet", "identifier": "AAA", "reason": "new_filing"}],
            "outcomes": [
                {
                    "identifier": "AAA",
                    "kind": "quiet",
                    "provider": "tavily",
                    "via": "search",
                    "accepted": 1,
                }
            ],
            "metrics": {"tavily": {"accepted": 1, "cost_usd": 0.016}},
            "usage": {
                "tavily": {"run": 2, "month": 12, "month_limit": 1000},
                "parallel": {"run": 0, "month": 0.03, "month_limit": 5},
            },
        },
        "shared_analysis": {"l1_written": 1, "l1_cache_hits": 0, "l2_written": 0, "l3_clusters": 0},
    }
    subject, body, _ = report(session, run)
    return subject + "\n\n" + body


def test_635_report_fixture_example(db_session: Session) -> None:
    sample = example_report(db_session)
    assert "Kept: 2 headlines and 0 company filings (2 of them new to the database)." in sample
    assert "AAA  Tavily  1 articles kept (headline search)" in sample
    print(sample)


def test_638_task_preserves_trigger_written_after_reset(db_session: Session) -> None:
    def collect(
        session: Session, run: IntelSlotRun, *args: object, **kwargs: object
    ) -> IntelCollectionRun:
        run.details = {"trigger": "manual"}
        return IntelCollectionRun(status="ok")

    with (
        patch.object(task, "SessionLocal", return_value=db_session),
        patch.object(task, "now_et", return_value=NOW),
        patch.object(task, "today_et", return_value=NOW.date()),
        patch.object(task, "resolve_profiles", return_value=[]),
        patch.object(task, "capture_news", return_value=PoolCaptureResult(0, [])),
        patch.object(task, "collect_slot_news", side_effect=collect),
        patch.object(task, "load_intel_deepen_config", side_effect=ValueError("fixture")),
        patch.object(task, "send_ops_alert", return_value=True) as send,
    ):
        task.intel_slot_task("post_close")
    assert "(manual)" in send.call_args.args[0]
    assert db_session.query(IntelSlotRun).one().details["trigger"] == "manual"


def test_638_classifier_error_is_plain_failure() -> None:
    assert digest.problem_lines(["classifier: ValueError"]) == [
        "Problems:",
        "  AI review: AI review failed (1 times)",
    ]


def test_638_documentation_distinguishes_stored_and_rendered_error() -> None:
    from pathlib import Path

    document = (
        Path(__file__).resolve().parents[3] / "docs/mechanisms/capture-and-reporting.md"
    ).read_text()
    assert "stored error remains `deepening: ValueError`" in document
    assert "`Paid deepening: unexpected error (ValueError)`" in document


def test_638_picked_groups_names_by_reason_in_first_seen_order(db_session: Session) -> None:
    themes = yaml.safe_load(_get_keywords_path().read_text())["themes"]
    run = slot(db_session)
    run.details = {
        "deepening": {
            "selections": [
                {"kind": "quiet", "identifier": "0700.HK", "reason": "new_filing"},
                {"kind": "macro", "theme": themes[0]["name"], "reason": "theme 11 items"},
                {"kind": "quiet", "identifier": "2333.HK", "reason": "new_filing"},
            ]
        }
    }
    line = next(
        line for line in report(db_session, run)[1].splitlines() if line.startswith("Picked:")
    )
    assert (
        line
        == "Picked: 0700.HK, 2333.HK (new company filing); Monetary policy (11 matching headlines)"
    )
