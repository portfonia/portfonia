"""Usage-context propagation across the named production paths (#886)."""

import uuid
from contextlib import ExitStack
from datetime import date
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy.orm import Session

from app.services import report_generator as rg
from app.services.usage_recording import LlmUsageContext
from app.tests.conftest import seed_user
from app.tests.test_report_generator import (
    _FAKE_LLM_PASS2_F2,
    _normal_path_patches,
)
from app.tests.test_usage_recording_886 import response


def generation_stack(stack: ExitStack, body: str = _FAKE_LLM_PASS2_F2) -> MagicMock:
    for p in _normal_path_patches()[:-1]:
        stack.enter_context(p)  # type: ignore[arg-type]
    client = MagicMock()
    result = response()
    result.choices[0].message.content = body
    client.chat.completions.create.return_value = result
    stack.enter_context(patch.object(rg, "_openrouter_client", return_value=client))
    return stack.enter_context(patch("app.services.report_llm.record_llm_call"))


def test_05_ops_job_and_beat_pass2_triggers(db_session: Session) -> None:
    from app.tasks.report_tasks import generate_incremental_report, generate_report_job
    from app.tests.test_report_jobs import _make_job

    uid = uuid.uuid4()
    user = seed_user(db_session, uid)
    user.locale = "en"
    job = _make_job(db_session, user_id=uid)
    with ExitStack() as stack:
        recorder = generation_stack(stack)
        generate_report_job.run(str(job.id), report_date="2026-06-04")
        assert [
            (c.args[0].purpose, c.args[0].trigger_source, c.args[0].user_id)
            for c in recorder.call_args_list
        ] == [("report_pass2", "ops", uid)]
        recorder.reset_mock()
        stack.enter_context(patch("app.tasks.report_tasks.cadence_is_due", return_value=True))
        stack.enter_context(patch("app.services.user_scope.active_users", return_value=[user]))
        stack.enter_context(patch("app.services.subscription.run_cadence_checks"))
        stack.enter_context(
            patch("app.tasks.report_tasks._REPORT_CADENCES", [("mwf", "after_close", None)])
        )
        generate_incremental_report.run(cadences=["mwf"])
        assert [
            (c.args[0].purpose, c.args[0].trigger_source, c.args[0].user_id)
            for c in recorder.call_args_list
        ] == [("report_pass2", "scheduled", uid)]


def test_06_render_translation_uses_report_user(db_session: Session) -> None:
    from app.services import report_translation as rt

    uid = uuid.uuid4()
    seed_user(db_session, uid)
    with ExitStack() as stack:
        recorder = generation_stack(stack)
        client = MagicMock()
        result = response()
        result.choices[0].message.content = "Translated content. " * 200
        client.chat.completions.create.return_value = result
        stack.enter_context(patch.object(rt, "_openrouter_client", return_value=client))
        stack.enter_context(patch("app.services.report_translation.time.sleep"))
        report = rg.generate_report(
            db_session, user_id=uid, report_date=date(2026, 6, 4), output_lang="zh"
        )
        translations = [
            c.args[0] for c in recorder.call_args_list if c.args[0].purpose == "translation"
        ]
        assert translations and all(
            c.user_id == uid and c.trigger_source == "scheduled" for c in translations
        )
        recorder.reset_mock()
        rg.regenerate_report(db_session, report.id, user_id=uid, mode="render", output_lang="zh")
        assert recorder.call_count > 0 and all(
            c.args[0] == LlmUsageContext(uid, "translation", "ops") for c in recorder.call_args_list
        )


def test_07_analyze_regenerate_records_ops(db_session: Session) -> None:
    uid = uuid.uuid4()
    seed_user(db_session, uid)
    with ExitStack() as stack:
        recorder = generation_stack(stack)
        report = rg.generate_report(db_session, user_id=uid, report_date=date(2026, 6, 4))
        recorder.reset_mock()
        rg.regenerate_report(db_session, report.id, user_id=uid, mode="analyze")
        assert recorder.call_count == 1 and recorder.call_args.args[0] == LlmUsageContext(
            uid, "report_regenerate", "ops"
        )


def test_08_holdings_attempt_user_and_usage_request() -> None:
    from app.services import holding_parser as hp

    uid = uuid.uuid4()
    client = MagicMock()
    client.chat.completions.create.return_value = response()
    with patch.object(hp, "record_llm_call", create=True) as recorder:
        hp._parse_attempt(client, "m", {"data_collection": "deny"}, "input", user_id=uid)
    assert recorder.call_count == 1 and recorder.call_args.args[0] == LlmUsageContext(
        uid, "holdings_parse", "user"
    )
    assert client.chat.completions.create.call_args.kwargs["extra_body"]["usage"] == {
        "include": True
    }


def test_09_shared_usage_before_content_validation() -> None:
    from app.services import headline_cleaning as hc

    resp = httpx.Response(
        200,
        json={
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.01},
            "choices": [{"message": {"content": "not json"}}],
        },
        request=httpx.Request("POST", "https://openrouter.test"),
    )
    with (
        patch("app.services.headline_cleaning.httpx.post", return_value=resp),
        patch.object(hc, "record_llm_call", create=True) as recorder,
        pytest.raises(ValueError),
    ):
        hc.openrouter_json("system", "content", purpose="intel_name_check")
    assert recorder.call_count == 2
    assert all(
        c.args[0] == LlmUsageContext(None, "intel_name_check", "scheduled") and c.args[-1] == 0.01
        for c in recorder.call_args_list
    )
