"""Issue #687 contract: title rules, list residue and per-unit body relevance."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.paid_intel import IntelArticle, IntelArticleLink
from app.services import headline_cleaning as hc
from app.services import intel_deepen as deepen
from app.services.instrument_news_sources import CollectedItem
from app.services.intel_body import body_verdict, clean_body, strip_residue
from app.services.intel_deepen_config import load_intel_deepen_config
from app.services.intel_leads import Lead, accepted_recently, url_key
from app.services.intel_selection import WorkUnit
from app.services.paid_search import PaidResult
from app.tests.test_intel_deepen_rules import NOW
from app.tests.test_intel_deepen_run import factory, settings
from app.tests.test_intel_paid import slot


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("What Will $5,000 Invested in Broadcom Stock Be Worth in 5 Years?", "low_value_rule"),
        ("Alphabet Has the Biggest Upside in the Mag 7 Right Now: 64%", "low_value_rule"),
        ("MSFT Rises 41% in 6 Months on Solid AI Demand: Should You Hold Now?", "low_value_rule"),
        (
            "SPCX Stock Rallies 20% In Three Sessions - Why Does Goldman See Another 30% Upside?",
            None,
        ),
        ("Marvell stock surges after company raises 2028 revenue outlook to $20 billion", None),
    ],
)
def test_687_01_title_rules(title: str, expected: str | None) -> None:
    item = CollectedItem(title, NOW, "https://fixture.example/story")
    for pool in (False, True):
        assert (
            hc.block_reason(
                item,
                ["Broadcom", "Alphabet", "MSFT", "SPCX", "Marvell"],
                [],
                hc.load_cleaning_config(),
                pool=pool,
            )
            == expected
        )


PARAGRAPH = "Coherent announced a new optical platform that expands manufacturing capacity and supports its customers worldwide."
SECOND = "The company said the new factory will deliver additional products and improve supply availability this year."
SELECTOR = (
    "Region * "
    + " ".join(["Canada", "Afghanistan", "Australia"] * 100)
    + " By submitting this form you agree to our Privacy Policy."
)


def test_687_02_list_residue() -> None:
    cfg = load_intel_deepen_config()
    assert (
        strip_residue(SELECTOR + "\n" + PARAGRAPH + "\n" + SECOND, cfg) == PARAGRAPH + "\n" + SECOND
    )
    assert strip_residue(SELECTOR, cfg) == ""
    assert body_verdict(clean_body("https://fixture.example/a", SELECTOR, cfg), cfg) == (
        False,
        "empty",
    )
    assert (
        clean_body("https://fixture.example/a", SELECTOR + "\n" + PARAGRAPH + "\n" + SECOND, cfg)
        == PARAGRAPH + "\n" + SECOND
    )


def test_687_03_quoted_sentence_ends() -> None:
    # Two normal 45-word sentences; the first ends with a closing curly quote.
    first = (
        "The company announced a new optical platform that expands manufacturing capacity "
        "and supports customers worldwide, while management described additional factory "
        "investments, delivery schedules, supply agreements, and engineering milestones "
        "during a public briefing about its operations and the next phase of production "
        "at its international facilities."
    )
    second = (
        "The board approved a financing agreement for the new factory after reviewing "
        "demand from existing customers, and executives outlined the expected construction "
        "schedule, equipment requirements, workforce training program, and delivery "
        "arrangements that underpin the manufacturing expansion discussed in the company "
        "announcement across several regional markets."
    )
    paragraph = first + "\u201d " + second
    assert len(first.split()) == len(second.split()) == 45
    assert len(paragraph.split()) == 90
    assert strip_residue(paragraph, load_intel_deepen_config()) == paragraph


def test_687_02_related_prompt() -> None:
    assert "opinion or thesis pieces about a stock" in hc.RELATED_PROMPT
    assert "return projections" in hc.RELATED_PROMPT
    assert (
        "headlines whose only fact is the related entity's share-price performance"
        in hc.RELATED_PROMPT
    )


@pytest.mark.parametrize("provider", ["tavily", "parallel"])
@pytest.mark.parametrize("kind", ["mover", "quiet", "macro"])
def test_687_05_body_only_relevance(db_session: Session, provider: str, kind: str) -> None:
    cfg = load_intel_deepen_config()
    unit = WorkUnit(
        kind, "SPCX" if kind != "macro" else "", theme="rates" if kind == "macro" else ""
    )
    lead = Lead("https://fixture.example/spcx", "SPCX rallies after new orders", NOW)
    body = (
        "Marvell and Google announced a new chip agreement to expand production capacity around the world. "
        * 5
    )
    with (
        patch.object(deepen, "get_settings", return_value=settings()),
        patch.object(deepen, "SessionLocal", side_effect=lambda: factory(db_session)),
    ):
        worker = deepen.DeepenRun(
            db_session, slot(db_session), cfg, False, NOW, NOW - timedelta(days=1), [], {}
        )
        worker.aliases["SPCX"] = ["SpaceX", "SPCX"]
        with (
            patch.object(worker, "_provider", return_value=provider),
            patch.object(worker.usage, "extract_size", side_effect=lambda p, n: n),
            patch.object(
                worker,
                "_call",
                return_value=(
                    provider,
                    PaidResult(200, Decimal(1), Decimal(".001"), bodies={lead.url: body}),
                ),
            ),
        ):
            worker._extract_batch(provider, [(unit, lead)])
        worker.close()
    article = db_session.scalars(select(IntelArticle)).one()
    links = list(db_session.scalars(select(IntelArticleLink)))
    expected = kind == "macro"
    assert article.status == ("accepted" if expected else "rejected")
    assert article.reject_reason == (None if expected else "off_topic")
    assert (article.record is not None) == expected
    assert len(links) == int(expected)
    assert worker.outcomes[0]["accepted"] == int(expected)
    assert worker.outcomes[0]["rejected"] == ({} if expected else {"off_topic": 1})
    assert worker.metrics[provider]["accepted"] == int(expected)
    assert worker.metrics[provider]["rejected"] == ({} if expected else {"off_topic": 1})
    assert worker.accepted[provider] == ({url_key(lead.url)} if expected else set())
    assert accepted_recently(db_session, lead.url, cfg, NOW) == expected


@pytest.mark.parametrize("provider", ["tavily", "parallel"])
@pytest.mark.parametrize("order", [("AAOI", "LITE"), ("LITE", "AAOI")])
def test_687_06_never_downgrade(db_session: Session, provider: str, order: tuple[str, str]) -> None:
    cfg = load_intel_deepen_config()
    body = (
        "Applied Optoelectronics announced an agreement to expand production capacity and ship additional optical products this year. "
        * 5
    )
    lead = Lead("https://fixture.example/shared", "AAOI announced new orders", NOW)
    with (
        patch.object(deepen, "get_settings", return_value=settings()),
        patch.object(deepen, "SessionLocal", side_effect=lambda: factory(db_session)),
    ):
        worker = deepen.DeepenRun(
            db_session, slot(db_session), cfg, False, NOW, NOW - timedelta(days=1), [], {}
        )
        worker.aliases = {"AAOI": ["Applied Optoelectronics", "AAOI"], "LITE": ["Lumentum", "LITE"]}
        with (
            patch.object(worker, "_provider", return_value=provider),
            patch.object(worker.usage, "extract_size", side_effect=lambda p, n: n),
            patch.object(
                worker,
                "_call",
                return_value=(
                    provider,
                    PaidResult(200, Decimal(1), Decimal(".001"), bodies={lead.url: body}),
                ),
            ),
        ):
            for identifier in order:
                worker._extract_batch(provider, [(WorkUnit("quiet", identifier), lead)])
        worker.close()
    article = db_session.scalars(select(IntelArticle)).one()
    assert article.status == "accepted" and article.reject_reason is None
    assert article.record is not None
    assert article.record["body"] == clean_body(lead.url, body, cfg)
    assert list(db_session.scalars(select(IntelArticleLink.identifier))) == ["AAOI"]
    by_id = {o["identifier"]: o for o in worker.outcomes}
    assert by_id["AAOI"]["accepted"] == 1 and by_id["AAOI"]["rejected"] == {}
    assert by_id["LITE"]["accepted"] == 0 and by_id["LITE"]["rejected"] == {"off_topic": 1}
    assert worker.metrics[provider]["accepted"] == 1
    assert worker.metrics[provider]["rejected"] == {"off_topic": 1}
    assert worker.accepted[provider] == {url_key(lead.url)}
