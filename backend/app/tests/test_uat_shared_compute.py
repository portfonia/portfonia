"""Tests for the one-shot §7.3 production UAT script (issue #128).

The script itself talks to a live container; these tests lock the invariants
that must hold before that happens: fixture shape, fan-out wiring, email
hard-off, SELECT-before-delete cleanup, and no writes to the shared cache
tables.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timezones import ET
from app.models.holding import Holding
from app.models.news import News
from app.models.news_surfaced import NewsSurfaced
from app.models.report import Report
from app.scripts import uat_shared_compute as uat
from app.services.window_data import BOOTSTRAP_WATERMARK, user_watermark
from app.tests.conftest import TEST_USER_ID, U1_USER_ID, U2_USER_ID, U3_USER_ID


def test_synthetic_ids_are_distinct_from_the_pytest_fixture() -> None:
    """Production UAT must not reuse the a1/a2/a3 pytest fixture ids."""
    assert uat.U1_USER_ID != U1_USER_ID
    assert uat.U2_USER_ID != U2_USER_ID
    assert uat.U3_USER_ID != U3_USER_ID
    assert uat.UAT_USER_IDS == (uat.U1_USER_ID, uat.U2_USER_ID, uat.U3_USER_ID)
    assert str(uat.U1_USER_ID).endswith("b1")
    assert str(uat.U2_USER_ID).endswith("b2")
    assert str(uat.U3_USER_ID).endswith("b3")


def test_seed_holdings_matches_design_doc_section_7_1(db_session: Session) -> None:
    uat.seed_holdings(db_session)
    db_session.flush()

    rows = list(db_session.execute(select(Holding)).scalars())
    by_user: dict[uuid.UUID, list[Holding]] = {}
    for row in rows:
        by_user.setdefault(row.user_id, []).append(row)

    assert set(by_user) == set(uat.UAT_USER_IDS)

    u1 = {(h.ticker, h.fund_code, h.asset_class, h.pricing_mode) for h in by_user[uat.U1_USER_ID]}
    assert (
        ("NVDA", None, "EQUITY_US_TECH", "auto") in u1
        and ("AAPL", None, "STOCK", "auto") in u1
        and ("QQQM", None, "EQUITY_US_BROAD", "auto") in u1
        and (None, "110011", "EQUITY_CN", "auto") in u1
    )
    cash = [h for h in by_user[uat.U1_USER_ID] if h.pricing_mode == "manual"]
    assert len(cash) == 1
    assert cash[0].ticker is None and cash[0].fund_code is None
    assert cash[0].current_value is not None and cash[0].current_value > 0
    assert cash[0].asset_class == "CASH_EQUIV"

    u2 = {(h.ticker, h.asset_class, h.currency) for h in by_user[uat.U2_USER_ID]}
    assert ("NVDA", "EQUITY_US_TECH", "USD") in u2
    assert ("AAPL", "EQUITY_US_TECH", "USD") in u2
    assert ("0700.HK", "EQUITY_CN", "HKD") in u2
    u2_cash = [h for h in by_user[uat.U2_USER_ID] if h.pricing_mode == "manual"]
    assert len(u2_cash) == 1
    assert u2_cash[0].currency == "USD"

    u3_tickers = {h.ticker or h.fund_code for h in by_user[uat.U3_USER_ID]}
    assert u3_tickers == {"513650.SS", "019547", "SGOL"}

    # Auto-priced rows must have shares so compute_portfolio can value them.
    auto = [h for h in rows if h.pricing_mode == "auto"]
    assert auto and all(h.shares is not None and h.shares > 0 for h in auto)


def test_same_ticker_different_asset_class_on_u1_and_u2(db_session: Session) -> None:
    """Design doc §7.1 extra requirement: threshold judgment stays per-user."""
    uat.seed_holdings(db_session)
    db_session.flush()
    aapl = [h for h in db_session.execute(select(Holding)).scalars() if h.ticker == "AAPL"]
    classes = {h.user_id: h.asset_class for h in aapl}
    assert classes[uat.U1_USER_ID] != classes[uat.U2_USER_ID]


def test_classify_llm_calls_splits_by_pipeline_order() -> None:
    cheap = "~deepseek/deepseek-v4-flash-latest"
    mid = "deepseek/deepseek-v4-pro"
    primary = "deepseek/deepseek-v4-pro"
    # When mid == primary the shadow mid call is still the last one; the
    # classifier keys off position, not just the model name.
    calls = [
        {"model": cheap, "prompt_tokens": 100, "completion_tokens": 10, "cost": 0},
        {"model": cheap, "prompt_tokens": 200, "completion_tokens": 20, "cost": 0.001},
        {"model": primary, "prompt_tokens": 7000, "completion_tokens": 3000, "cost": 0.01},
        {"model": cheap, "prompt_tokens": 500, "completion_tokens": 400, "cost": 0.002},
        {"model": mid, "prompt_tokens": 500, "completion_tokens": 400, "cost": 0.005},
    ]
    split = uat.classify_llm_calls(calls, primary=primary, cheap=cheap, mid=mid)
    assert [c["model"] for c in split["pass1"]] == [cheap]
    assert [c["model"] for c in split["shared_intel"]] == [cheap]
    assert [c["model"] for c in split["pass2"]] == [primary]
    assert [c["model"] for c in split["shadow_cheap"]] == [cheap]
    assert [c["model"] for c in split["shadow_mid"]] == [mid]


def test_leak_check_flags_a_foreign_holding() -> None:
    reports = {
        uat.U1_USER_ID: {
            "report_md": "NVDA moved. QQQM held.",
            "report_inputs": {"portfolio_summary": {"holdings": [{"ticker": "NVDA"}]}},
        },
        uat.U3_USER_ID: {
            "report_md": "SGOL held.",
            "report_inputs": {
                "portfolio_summary": {"holdings": [{"ticker": "SGOL"}, {"ticker": "NVDA"}]},
            },
        },
    }
    leaks = uat.find_cross_user_leaks(reports)
    assert any("NVDA" in item for item in leaks)
    assert not any("SGOL" in item and "U1" in item for item in leaks)


def test_leak_check_passes_when_exclusive_holdings_stay_put() -> None:
    reports = {
        uat.U1_USER_ID: {
            "report_md": "NVDA and QQQM and 110011",
            "report_inputs": {
                "portfolio_summary": {
                    "holdings": [{"ticker": "NVDA"}, {"ticker": "QQQM"}, {"fund_code": "110011"}]
                }
            },
        },
        uat.U2_USER_ID: {
            "report_md": "NVDA and 0700.HK",
            "report_inputs": {"portfolio_summary": {"holdings": [{"ticker": "0700.HK"}]}},
        },
        uat.U3_USER_ID: {
            "report_md": "SGOL and 019547 and 513650.SS",
            "report_inputs": {"portfolio_summary": {"holdings": [{"ticker": "SGOL"}]}},
        },
    }
    assert uat.find_cross_user_leaks(reports) == []


def test_leak_check_ignores_nvda_in_u3_news_corpus() -> None:
    """Review finding: news_items/search_results are global headlines, not holdings."""
    reports = {
        uat.U1_USER_ID: {
            "report_md": "QQQM held.",
            "report_inputs": {
                "portfolio_summary": {"holdings": [{"ticker": "QQQM"}]},
                "news_items": [{"title": "SGOL rally"}],
            },
        },
        uat.U3_USER_ID: {
            "report_md": "SGOL held.",
            "report_inputs": {
                "portfolio_summary": {"holdings": [{"ticker": "SGOL"}]},
                "news_items": [{"title": "NVDA crushes earnings"}],
                "search_results": [{"title": "AAPL supplier note"}],
                "pass2_prompt": "Headlines mention NVDA and AAPL",
            },
        },
    }
    assert uat.find_cross_user_leaks(reports) == []


def test_leak_check_ignores_nvda_headline_in_u3_report_md() -> None:
    """Round-2 review: shipped Pass 2 / report_md can quote global headlines."""
    reports = {
        uat.U3_USER_ID: {
            "report_md": "Markets: NVDA crushes earnings. We hold SGOL.",
            "report_inputs": {
                "portfolio_summary": {"holdings": [{"ticker": "SGOL"}]},
                "pass2_raw": "NVDA led the tape; AAPL suppliers followed.",
                "assembly_raw": "NVDA mentioned in a headline.",
                "assembly_prompt": "news mentioned NVDA",
            },
        }
    }
    assert uat.find_cross_user_leaks(reports) == []


def test_leak_check_still_flags_nvda_in_u3_ticker_intel() -> None:
    reports = {
        uat.U3_USER_ID: {
            "report_md": "SGOL held.",
            "report_inputs": {
                "portfolio_summary": {"holdings": [{"ticker": "SGOL"}]},
                "ticker_intel": {"NVDA": "shared analysis"},
            },
        }
    }
    assert any("NVDA" in item for item in uat.find_cross_user_leaks(reports))


def test_leak_check_ignores_nvda_in_u3_l1_prose() -> None:
    """Round-4 review: L1/holding_news values are global prose, not identifiers."""
    reports = {
        uat.U3_USER_ID: {
            "report_md": "SGOL held.",
            "report_inputs": {
                "portfolio_summary": {"holdings": [{"ticker": "SGOL"}]},
                "ticker_intel": {"SGOL": "gold bid on an NVDA selloff"},
                "holding_news": {"SGOL": [{"title": "AAPL suppliers and gold"}]},
            },
        }
    }
    assert uat.find_cross_user_leaks(reports) == []


def _holding_count(session: Session, user_id: uuid.UUID) -> int:
    return len(list(session.execute(select(Holding).where(Holding.user_id == user_id)).scalars()))


def test_cleanup_deletes_only_the_three_synthetic_users(db_session: Session) -> None:
    uat.seed_holdings(db_session)
    db_session.add(
        Holding(
            user_id=TEST_USER_ID,
            name="Real User VOO",
            ticker="VOO",
            pricing_mode="auto",
            currency="USD",
            asset_class="EQUITY_US_BROAD",
            shares=Decimal("5"),
        )
    )
    seed = Report(
        user_id=uat.U1_USER_ID,
        report_date=datetime(2026, 8, 14, tzinfo=UTC).date(),
        report_type="incremental",
        session_node="uat_watermark",
        status="success",
        report_md="seed",
        period_start=datetime(2026, 8, 14, tzinfo=UTC),
        period_end=datetime(2026, 8, 14, tzinfo=UTC),
    )
    db_session.add(seed)
    db_session.flush()
    db_session.add(
        NewsSurfaced(
            user_id=uat.U1_USER_ID,
            news_id=uuid.uuid4(),
            report_id=seed.id,
        )
    )
    real_report = Report(
        user_id=TEST_USER_ID,
        report_date=datetime(2026, 8, 14, tzinfo=UTC).date(),
        report_type="incremental",
        session_node="after_close",
        status="success",
        report_md="keep me",
        period_start=datetime(2026, 8, 14, tzinfo=UTC),
        period_end=datetime(2026, 8, 14, tzinfo=UTC),
    )
    db_session.add(real_report)
    db_session.flush()

    deleted = uat.cleanup_synthetic_rows(db_session)
    db_session.flush()

    assert deleted["holdings"] == 12
    assert deleted["reports"] == 1
    assert deleted["news_surfaced"] == 1
    assert _holding_count(db_session, TEST_USER_ID) == 1
    assert (
        db_session.execute(select(Report).where(Report.user_id == TEST_USER_ID))
        .scalar_one()
        .report_md
        == "keep me"
    )
    leftover = list(
        db_session.execute(select(Holding).where(Holding.user_id.in_(uat.UAT_USER_IDS))).scalars()
    )
    assert leftover == []


def test_cleanup_refuses_when_a_row_is_not_a_synthetic_user(db_session: Session) -> None:
    """Defense against a SELECT/DELETE mismatch — never delete first."""
    db_session.add(
        Holding(
            user_id=TEST_USER_ID,
            name="should not be deletable via this path",
            ticker="VOO",
            pricing_mode="auto",
            currency="USD",
            asset_class="EQUITY_US_BROAD",
            shares=Decimal("1"),
        )
    )
    db_session.flush()
    try:
        uat.cleanup_synthetic_rows(
            db_session,
            holding_ids_override=[
                db_session.execute(select(Holding.id)).scalar_one(),
            ],
        )
    except uat.CleanupGuardError as exc:
        assert "not a synthetic" in str(exc).lower() or "user_id" in str(exc).lower()
    else:
        raise AssertionError("cleanup must refuse a non-synthetic row")
    assert _holding_count(db_session, TEST_USER_ID) == 1


def test_run_batch_wires_fan_out_like_the_celery_task(db_session: Session) -> None:
    uat.seed_holdings(db_session)
    db_session.flush()

    seen: list[dict[str, Any]] = []

    def _fake_generate(session: Session, **kwargs: Any) -> MagicMock:
        seen.append(kwargs)
        report = MagicMock()
        report.id = uuid.uuid4()
        report.status = "success"
        report.user_id = kwargs["user_id"]
        report.report_md = "ok"
        report.report_inputs = {"llm_calls": [], "assembly_shadow": {}}
        return report

    batch_now = datetime(2026, 8, 17, 20, 0, tzinfo=UTC)
    # The task module is deliberately not imported: calling
    # generate_incremental_report.run() would fan out to every real user.
    assert not hasattr(uat, "generate_incremental_report")
    with patch("app.scripts.uat_shared_compute.generate_report", side_effect=_fake_generate):
        reports = uat.run_synthetic_batch(db_session, now=batch_now)
    assert len(seen) == 3
    assert [c["user_id"] for c in seen] == list(uat.UAT_USER_IDS)
    assert all(c["session_node"] == "manual" for c in seen)
    assert all(c["now"] is batch_now for c in seen)
    assert [c["users_remaining"] for c in seen] == [3, 2, 1]
    cache = seen[0]["moves_cache"]
    assert seen[1]["moves_cache"] is cache
    assert seen[2]["moves_cache"] is cache
    assert set(reports) == set(uat.UAT_USER_IDS)


def test_install_email_guards_replaces_every_notify_target() -> None:
    sent: list[str] = []

    def _real(*_args: object, **_kwargs: object) -> bool:
        sent.append("sent")
        return True

    patches = [patch(target, _real) for target in uat.EMAIL_GUARD_TARGETS]
    for p in patches:
        p.start()
    restore = uat.install_email_guards()
    try:
        import importlib

        for target in uat.EMAIL_GUARD_TARGETS:
            module_name, attr = target.rsplit(".", 1)
            module = importlib.import_module(module_name)
            patched = vars(module)[attr]
            assert isinstance(patched, uat._NoOpEmail), target
            assert patched("x", "y") is True
        assert sent == []
    finally:
        restore()
        for p in patches:
            p.stop()


def test_one_trading_week_start_is_five_weekdays_back_at_et_midnight() -> None:
    # Monday 17 Aug 2026 23:45 ET → five weekdays back is Monday 10 Aug.
    now = datetime(2026, 8, 17, 23, 45, tzinfo=ET)
    start = uat.one_trading_week_start(now)
    assert start == datetime(2026, 8, 10, 0, 0, tzinfo=ET)
    # Wednesday: skip Tue/Mon/Fri/Thu/Wed → previous Wednesday.
    wed = datetime(2026, 8, 19, 12, 0, tzinfo=ET)
    assert uat.one_trading_week_start(wed) == datetime(2026, 8, 12, 0, 0, tzinfo=ET)


def test_window_alignment_uses_one_trading_week_not_another_users_watermark(
    db_session: Session,
) -> None:
    """No prior report for THIS user → period is one trading week.

    Must not copy another user's watermark or news_surfaced ledger. News
    published at or before the week start is marked surfaced so the no-lower-
    bound selector does not dump the entire capture table into a first report.
    """
    now = datetime(2026, 8, 17, 23, 45, tzinfo=ET)
    week_start = uat.one_trading_week_start(now)
    older = News(
        url_hash="uat-old",
        title="old",
        source="x",
        url="https://example.test/old",
        published_at=datetime(2026, 8, 7, 12, 0, tzinfo=ET),
    )
    inside = News(
        url_hash="uat-new",
        title="new",
        source="x",
        url="https://example.test/new",
        published_at=datetime(2026, 8, 13, 12, 0, tzinfo=ET),
    )
    other_user_report = Report(
        user_id=TEST_USER_ID,
        report_date=datetime(2026, 8, 14).date(),
        report_type="incremental",
        session_node="after_close",
        status="success",
        period_start=datetime(2026, 6, 1, tzinfo=ET),
        period_end=datetime(2026, 8, 14, 21, 0, tzinfo=UTC),
    )
    db_session.add_all([older, inside, other_user_report])
    db_session.flush()
    db_session.add(
        NewsSurfaced(user_id=TEST_USER_ID, news_id=older.id, report_id=other_user_report.id)
    )
    db_session.flush()

    result = uat.seed_window_alignment(db_session, now=now)
    db_session.flush()

    assert result["aligned"] is True
    assert result["source"] == "one_trading_week"
    for uid in uat.UAT_USER_IDS:
        assert user_watermark(db_session, uid, "incremental") == week_start
        assert user_watermark(db_session, uid, "incremental") != BOOTSTRAP_WATERMARK
        assert user_watermark(db_session, uid, "incremental") != other_user_report.period_end
        marked = {
            m.news_id
            for m in db_session.execute(
                select(NewsSurfaced).where(NewsSurfaced.user_id == uid)
            ).scalars()
        }
        assert older.id in marked
        assert inside.id not in marked
    real_marks = list(
        db_session.execute(
            select(NewsSurfaced).where(NewsSurfaced.user_id == TEST_USER_ID)
        ).scalars()
    )
    assert len(real_marks) == 1


def test_apply_runtime_settings_does_not_enable_shared_compute() -> None:
    settings = MagicMock()
    settings.SHARED_COMPUTE_ENABLED = False
    settings.ASSEMBLY_SHADOW_MODELS = ""
    settings.LOW_COST_LLM_MODEL = "~deepseek/deepseek-v4-flash-latest"
    restore = uat.apply_runtime_settings(
        settings, cheap="~deepseek/deepseek-v4-flash-latest", mid="some/mid-model"
    )
    try:
        assert settings.SHARED_COMPUTE_ENABLED is False
        assert settings.ASSEMBLY_SHADOW_MODELS == (
            "~deepseek/deepseek-v4-flash-latest,some/mid-model"
        )
    finally:
        restore()


def test_beat_window_blocks_mon_wed_fri_from_1530_et() -> None:
    assert uat.beat_window_blocks(datetime(2026, 8, 17, 17, 0, tzinfo=ET)) is True
    assert uat.beat_window_blocks(datetime(2026, 8, 17, 16, 40, tzinfo=ET)) is True
    assert uat.beat_window_blocks(datetime(2026, 8, 17, 15, 31, tzinfo=ET)) is True
    assert uat.beat_window_blocks(datetime(2026, 8, 17, 15, 0, tzinfo=ET)) is False
    assert uat.beat_window_blocks(datetime(2026, 8, 17, 17, 31, tzinfo=ET)) is False
    assert uat.beat_window_blocks(datetime(2026, 8, 18, 17, 0, tzinfo=ET)) is False


def test_keep_reports_always_blocked_without_i_know() -> None:
    assert uat.keep_reports_blocked(datetime(2026, 8, 17, 10, 0, tzinfo=ET)) is True
    assert uat.keep_reports_blocked(datetime(2026, 8, 18, 10, 0, tzinfo=ET)) is True


def _complete_users(**statuses: str) -> dict[str, dict[str, str]]:
    return {label: {"status": status} for label, status in statuses.items()}


def test_failed_run_when_leaks_or_rerender_fails() -> None:
    complete = _complete_users(U1="success", U2="success", U3="skipped")
    assert (
        uat.failed_run({"leaks": ["U3 contains NVDA"], "rerender": {"ok": True}, "users": complete})
        is True
    )
    assert uat.failed_run({"leaks": [], "rerender": {"ok": False}, "users": complete}) is True
    assert (
        uat.failed_run({"leaks": [], "rerender": {"ok": None, "skipped": "n/a"}, "users": complete})
        is False
    )
    assert uat.failed_run({"leaks": [], "rerender": {"ok": True}, "users": complete}) is False


def test_failed_run_when_a_synthetic_user_is_missing() -> None:
    two = _complete_users(U1="success", U2="success")
    assert uat.failed_run({"leaks": [], "rerender": {"ok": None}, "users": two}) is True
    none: dict[str, dict[str, str]] = {}
    assert uat.failed_run({"leaks": [], "rerender": {"ok": None}, "users": none}) is True
    failed = _complete_users(U1="success", U2="success", U3="failed")
    assert uat.failed_run({"leaks": [], "rerender": {"ok": True}, "users": failed}) is True


def test_restore_shipped_body_puts_back_zh_markdown(db_session: Session) -> None:
    row = Report(
        user_id=uat.U1_USER_ID,
        report_date=datetime(2026, 8, 17).date(),
        report_type="incremental",
        session_node="manual",
        status="success",
        report_md="EN overwrite",
        report_inputs={"pass2_translated": "en body", "pass2_raw": "raw"},
    )
    db_session.add(row)
    db_session.flush()
    uat.restore_shipped_body(db_session, row, "shipped-zh-body", "zh-translated")
    db_session.refresh(row)
    assert row.report_md == "shipped-zh-body"
    assert row.report_inputs is not None
    assert row.report_inputs["pass2_translated"] == "zh-translated"


def test_verify_rerender_restores_body_if_regenerate_raises(db_session: Session) -> None:
    row = Report(
        user_id=uat.U1_USER_ID,
        report_date=datetime(2026, 8, 17).date(),
        report_type="incremental",
        session_node="manual",
        status="success",
        report_md="shipped-zh-body",
        report_inputs={"pass2_raw": "## §2\n## §3\n## §4", "pass2_translated": "zh-translated"},
    )
    db_session.add(row)
    db_session.flush()

    def _boom(*_args: object, **_kwargs: object) -> Report:
        row.report_md = "EN overwrite"
        db_session.commit()
        raise RuntimeError("render failed")

    with patch("app.scripts.uat_shared_compute.regenerate_report", side_effect=_boom):
        result = uat.verify_rerender_zero_llm(db_session, row)
    db_session.refresh(row)
    assert row.report_md == "shipped-zh-body"
    assert result["ok"] is False


def test_report_has_stored_body_skips_quiet_skipped_rows() -> None:
    skipped = MagicMock()
    skipped.status = "skipped"
    skipped.report_inputs = {"portfolio_summary": {}, "news_items": []}
    assert uat.report_has_stored_body(skipped) is False
    ok = MagicMock()
    ok.status = "success"
    ok.report_inputs = {"pass2_raw": "## §2\n## §3\n## §4"}
    assert uat.report_has_stored_body(ok) is True


def test_evidence_blocks_include_shipped_body_and_both_shadows() -> None:
    blocks = uat.evidence_blocks(
        "U2",
        "shipped md",
        {
            "pass2_raw": "pass2 body",
            "assembly_shadow": {
                "cheap/m": {"raw": "cheap body"},
                "mid/m": {"raw": "mid body"},
            },
        },
        cheap="cheap/m",
        mid="mid/m",
    )
    titles = [title for title, _ in blocks]
    assert any("shipped" in t.lower() or "report_md" in t for t in titles)
    assert any("pass2" in t.lower() for t in titles)
    assert any("cheap" in t for t in titles)
    assert any("mid" in t for t in titles)
    texts = "\n".join(body for _, body in blocks)
    assert "shipped md" in texts
    assert "cheap body" in texts
    assert "mid body" in texts
