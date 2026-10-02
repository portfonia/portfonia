"""Issue #620 headline rules and one-shot classifier acceptance."""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import get_settings
from app.services import headline_cleaning as hc
from app.services.instrument_news_sources import CollectedItem
from app.tests.test_intel_records import NOW


@pytest.fixture(autouse=True)
def fixture_classifier_key() -> Iterator[None]:
    with patch.object(
        hc,
        "get_settings",
        return_value=get_settings().model_copy(
            update={"OPENROUTER_API_KEY": SecretStr("fixture-only")}
        ),
    ):
        yield


def lead(i: int, title: str = "Nvidia earnings") -> CollectedItem:
    return CollectedItem(title=title, published_at=NOW, url=f"https://fixture.example/{i}")


def result(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": content}}], "usage": {"cost": 0.001}},
        request=httpx.Request("POST", "https://fixture.example"),
    )


def test_acceptance_16_cleaning_order() -> None:
    config = hc.load_cleaning_config()
    titles: list[str] = []
    cases = [
        (
            CollectedItem(
                title="Nvidia video", published_at=NOW, url="https://fixture.example/video/a"
            ),
            "non_article",
        ),
        (lead(1, "Prediction: Robinhood Stock Sets a New Record"), "unrelated_rule"),
        (lead(2, "Should You Buy Nvidia Stock in October?"), "low_value_rule"),
        (lead(3, "Nvidia shares fall after export curbs"), None),
        (lead(4, "Nvidia shares fall after export curbs"), "duplicate"),
        (lead(5, "Nvidia authorizes $150 billion buyback"), None),
        (
            CollectedItem(
                title="8-K Item 2.02",
                published_at=NOW,
                url="https://fixture.example/filing",
                kind="filing",
                url_kind="filing",
            ),
            None,
        ),
    ]
    for item, expected in cases:
        reason = hc.block_reason(item, ["Nvidia", "NVDA"], titles, config)
        assert reason == expected
        if reason is None:
            titles.append(item.title)


def test_acceptance_17_classifier_mapping() -> None:
    with patch.object(
        httpx,
        "post",
        return_value=result(
            '{"labels":[{"id":0,"label":"keep"},{"id":1,"label":"mention"},{"id":2,"label":"promo"},{"id":3,"label":"unrelated"}]}'
        ),
    ):
        labels, cost, failed = hc.classify_headlines(
            [lead(i) for i in range(4)], "NVDA", ["Nvidia"]
        )
    assert (
        labels == {0: "keep", 1: "mention", 2: "promo", 3: "unrelated"}
        and cost == 0.001
        and not failed
    )


@pytest.mark.parametrize("failure", ["http", "timeout", "json"])
def test_acceptance_18_classifier_failure_no_retry(failure: str) -> None:
    resp = (
        httpx.Response(500, request=httpx.Request("POST", "https://fixture.example"))
        if failure == "http"
        else result("not JSON")
    )
    with patch.object(
        httpx,
        "post",
        side_effect=httpx.ReadTimeout("fixture") if failure == "timeout" else None,
        return_value=resp,
    ) as post:
        labels, _, failed = hc.classify_headlines([lead(0)], "NVDA", ["Nvidia"])
    expected = {
        "http": "classifier: HTTPStatusError HTTP 500",
        "timeout": "classifier: ReadTimeout",
        "json": "classifier: JSONDecodeError",
    }
    assert labels == {} and failed == expected[failure] and post.call_count == 1


def test_acceptance_19_missing_ids() -> None:
    with patch.object(httpx, "post", return_value=result('{"labels":[{"id":0,"label":"keep"}]}')):
        labels, _, failed = hc.classify_headlines([lead(0), lead(1)], "NVDA", ["Nvidia"])
    assert labels.get(1) is None and not failed


def test_acceptance_21_classifier_request() -> None:
    with patch.object(httpx, "post", return_value=result('{"labels":[]}')) as post:
        hc.classify_headlines([lead(0)], "NVDA", ["Nvidia"])
    body = post.call_args.kwargs["json"]
    assert body["reasoning"] == {"effort": "low"} and body["provider"] == {
        "data_collection": "deny"
    }
    assert "https://" not in body["messages"][1]["content"]
    assert post.call_args.kwargs["headers"]["HTTP-Referer"]


def test_acceptance_23_invalid_regex(tmp_path: Path) -> None:
    p = tmp_path / "clean.yml"
    p.write_text("low_value_title_patterns: ['[']")
    with pytest.raises(ValueError):
        hc.load_cleaning_config(p)
