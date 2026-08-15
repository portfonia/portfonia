"""Tests for report_context.py.

Split out of test_report_generator.py (#37).
"""

from __future__ import annotations

import json
import typing

from app.services import report_context as rc


def test_report_context_to_jsonb_serialisable() -> None:
    ctx = rc.ReportContext(
        pass1_model="deepseek/test",
        search_queries=["foo"],
    )
    data = ctx.to_jsonb()
    assert data["pass1_model"] == "deepseek/test"
    json.dumps(data)  # must not raise


def test_report_inputs_dict_mirrors_report_context_fields() -> None:
    """#39: ReportInputsDict must declare exactly the same keys AND value
    types as ReportContext's dataclass fields. The two are kept in sync by
    hand (see ReportInputsDict's docstring) — this is the regression guard
    against that sync silently drifting, whether by a field being added to
    one but not the other, or by a key staying present on both sides while
    its annotated type quietly diverges (PR #143 review round 1 — a
    key-only comparison would stay green on a type-only drift, and several
    read sites would too, since e.g. `int(...)` accepts a `str` and
    `len(...)` accepts one).

    Uses `typing.get_type_hints()` rather than comparing raw
    `__annotations__`/`dataclasses.fields(...).type` directly: with
    `from __future__ import annotations` active, a dataclass's `.type` is a
    plain string, but a TypedDict's `__annotations__` values are wrapped in
    `typing.ForwardRef` by its metaclass — those two never compare equal
    even when they name the same type, so only the *resolved* hints are
    comparable across both constructs.
    """
    context_hints = typing.get_type_hints(rc.ReportContext)
    typeddict_hints = typing.get_type_hints(rc.ReportInputsDict)
    assert typeddict_hints == context_hints
