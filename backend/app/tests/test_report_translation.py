"""Tests for report_translation.py.

Split out of test_report_generator.py (#37) — patch targets updated from
`app.services.report_generator.*` to `app.services.report_translation.*`,
the module `_translate_md`/`_translate_chunk`/`_split_sections` now actually
live in.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.services import report_translation as rt
from app.services.report_llm import _BYOK_PROVIDER_ORDER


def _fake_llm_response(content: str | None) -> MagicMock:
    """Shape a minimal OpenAI-style chat completion response."""
    resp = MagicMock()
    resp.model = "fake/model"
    if content is None:
        resp.choices = None
        return resp
    choice = MagicMock()
    choice.finish_reason = "stop"
    choice.message.content = content
    resp.choices = [choice]
    resp.usage = MagicMock(prompt_tokens=1, completion_tokens=1, total_tokens=2, cost=0.0)
    return resp


def test_translate_glossary_maps_confidence_labels_to_chinese() -> None:
    captured: dict[str, str] = {}

    def _fake_call(_client: object, _model: str, system: str, user: str, **_kw: object) -> str:
        captured["system"] = system
        return user

    with (
        patch.object(rt, "_openrouter_client", return_value=MagicMock()),
        patch.object(rt, "_call_llm", side_effect=_fake_call),
    ):
        rt._translate_md("## §4\nNVDA — chip optimism [Established].\n", "zh")
    assert '"[Established]" -> "[确定]"' in captured["system"]
    assert '"[Probable]" -> "[较可能]"' in captured["system"]
    assert '"[Speculative]" -> "[推测]"' in captured["system"]


def test_split_sections_chunks_at_section_and_subsection_headings_and_roundtrips() -> None:
    md = (
        "# Title\n\n> data window\n\n## §1 Snapshot\n\n| a | b |\n\n"
        "## §4 Risk\n\n### 4.1 Concentration\n\nflagged\n\n### 4.2 Anomalies\n\n| t |\n"
    )
    chunks = rt._split_sections(md)
    # preamble + §1 + §4 header + 4.1 + 4.2  → subsections split out
    assert [c.split("\n", 1)[0] for c in chunks] == [
        "# Title",
        "## §1 Snapshot",
        "## §4 Risk",
        "### 4.1 Concentration",
        "### 4.2 Anomalies",
    ]
    # joining with a single newline reproduces the original document exactly
    assert "\n".join(chunks) == md


def test_translate_chunk_uses_byok_hard_pin_no_deny_no_reasoning() -> None:
    """PR #79 review: lock down the exact extra_body shape of the BYOK
    exception (issue #78) end-to-end through the real _call_llm — not just
    that the call site passes the right kwargs, but that they actually produce
    a hard-pinned, deny-off, reasoning-off request. Guards against a future
    edit silently re-enabling deny, dropping the allow_fallbacks=False pin
    (which would reopen the marketplace-fallback compliance gap), or
    re-enabling paid reasoning tokens on this cheap tier."""
    client = MagicMock()
    client.chat.completions.create.return_value = _fake_llm_response(
        "## §3 持仓分析\n\n" + ("该持仓非常重要并值得密切关注。" * 20)
    )
    source = "## §3 Holdings Analysis\n\n" + ("This holding matters. " * 20)
    rt._translate_chunk(client, "m", "sys", source)

    kwargs = client.chat.completions.create.call_args.kwargs
    provider = kwargs["extra_body"]["provider"]
    assert provider["order"] == _BYOK_PROVIDER_ORDER == ["DeepSeek"]
    assert provider["allow_fallbacks"] is False
    assert "data_collection" not in provider
    assert kwargs["extra_body"]["reasoning"] == {"enabled": False}


def test_translate_chunk_falls_back_to_source_when_truncated() -> None:
    source = "## §3 Holdings Analysis\n\n" + ("This holding matters because " * 20)
    calls = {"n": 0}

    def _truncating(_c: object, _m: str, _s: str, _u: str, **_k: object) -> str:
        calls["n"] += 1
        return "持仓"  # always far too short → simulates a dropped/truncated 200

    with patch.object(rt, "_call_llm", side_effect=_truncating):
        out = rt._translate_chunk(MagicMock(), "m", "sys", source)
    assert calls["n"] == 2  # tried once, retried once
    assert out == source  # kept the English source rather than dropping the section


def test_translate_chunk_keeps_good_translation() -> None:
    source = "## §3 Holdings Analysis\n\n" + ("This holding matters. " * 20)

    def _ok(_c: object, _m: str, _s: str, _u: str, **_k: object) -> str:
        return "## §3 持仓分析\n\n" + ("该持仓非常重要并值得密切关注。" * 20)

    with patch.object(rt, "_call_llm", side_effect=_ok):
        out = rt._translate_chunk(MagicMock(), "m", "sys", source)
    assert out.startswith("## §3 持仓分析")
