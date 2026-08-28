"""Walks every Alembic revision through upgrade -> downgrade -> upgrade.

If any migration's downgrade is missing, asymmetric, or breaks the schema
in a way that prevents re-upgrade, this test fails. Guards against the
classic Alembic failure mode of an unverified downgrade path silently
rotting until rollback is needed in production.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alembic import command
from app.core.config import get_settings


def test_migrations_round_trip(alembic_cfg: Config) -> None:
    script = ScriptDirectory.from_config(alembic_cfg)
    revisions = list(script.walk_revisions())[::-1]  # oldest -> newest

    assert revisions, "no migrations discovered under alembic/versions/"

    for rev in revisions:
        command.upgrade(alembic_cfg, rev.revision)
        command.downgrade(alembic_cfg, "-1")
        command.upgrade(alembic_cfg, rev.revision)


def test_news_surfaced_backfill_reconstructs_from_report_history(alembic_cfg: Config) -> None:
    """PR #139 review: migration f1a2b3c4d5e6 must not deploy news_surfaced
    empty — an empty ledger combined with load_news_window's new no-lower-
    bound selection would dump the entire historical `news` table into the
    next report. Seed a DONE report whose report_inputs recorded a news item
    (the shape report_generator.py's _serialize_news actually produces:
    title/source/url/published_at/summary, no url_hash), plus an untouched
    `failed` report and an item with no matching News row, then confirm the
    backfill resolves only the real one — by url, hashed the same way
    news_fetcher._url_hash does — to a news_surfaced row."""
    from app.models.news import News
    from app.models.news_surfaced import NewsSurfaced
    from app.models.report import Report

    command.upgrade(alembic_cfg, "ed3e81d6cccb")  # one revision before f1a2b3c4d5e6

    engine = create_engine(get_settings().database_url)
    user_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    surfaced_url = "https://example.com/fed-raises-rates"
    with Session(engine) as seed_session:
        seed_session.add(
            News(
                url_hash="abc123",  # deliberately NOT the real hash of surfaced_url —
                # the backfill must resolve by re-hashing the stored url, not by
                # trusting any url_hash-shaped field in report_inputs (there isn't
                # one; this proves the lookup path, not a coincidental match).
                title="unrelated",
                source="TEST",
                url="https://example.com/unrelated",
                summary="s",
                published_at=datetime(2026, 6, 4, tzinfo=UTC),
            )
        )
        real_news = News(
            url_hash="placeholder",
            title="Fed raises rates",
            source="TEST",
            url=surfaced_url,
            summary="s",
            published_at=datetime(2026, 6, 4, tzinfo=UTC),
        )
        seed_session.add(real_news)
        seed_session.flush()
        # Overwrite with the real hash the app would have stored — done after
        # insert so the fixture data makes the "resolve by re-hashing" property
        # unambiguous rather than accidentally matching an inserted literal.
        real_news.url_hash = "1fc36081a0529bad"  # md5(surfaced_url)[:16], verified via hashlib
        seed_session.add(
            Report(
                user_id=user_id,
                report_date=datetime(2026, 6, 5).date(),
                report_type="incremental",
                session_node="after_close",
                status="needs_review",
                report_inputs={
                    "news_items": [
                        {
                            "title": "Fed raises rates",
                            "source": "TEST",
                            "url": surfaced_url,
                            "published_at": "2026-06-04T00:00:00+00:00",
                            "summary": "s",
                        }
                    ]
                },
            )
        )
        # A failed report's report_inputs must be ignored (never reached a
        # DONE status, so nothing in it was ever actually shown).
        seed_session.add(
            Report(
                user_id=user_id,
                report_date=datetime(2026, 6, 6).date(),
                report_type="incremental",
                session_node="after_close",
                status="failed",
                report_inputs={
                    "news_items": [
                        {
                            "title": "Fed raises rates",
                            "source": "TEST",
                            "url": surfaced_url,
                            "published_at": "2026-06-04T00:00:00+00:00",
                            "summary": "s",
                        }
                    ]
                },
            )
        )
        seed_session.commit()
        real_news_id = real_news.id

    command.upgrade(alembic_cfg, "f1a2b3c4d5e6")

    with Session(engine) as check_session:
        rows = check_session.query(NewsSurfaced).all()
        assert len(rows) == 1
        assert rows[0].news_id == real_news_id
        assert rows[0].user_id == user_id

    engine.dispose()


def test_users_migration_binds_existing_dev_user(alembic_cfg: Config) -> None:
    """A holdings row for DEV_USER_ID is bound into users; no other id present."""
    from sqlalchemy import text

    from app.core.config import get_settings as _gs
    from app.core.encryption import encrypt_value
    from app.models.user import User

    command.upgrade(alembic_cfg, "d6e7f8a9b0c1")
    engine = create_engine(get_settings().database_url)
    uid = uuid.UUID(_gs().DEV_USER_ID)
    # Raw SQL, not the Holding ORM model (issue #129 B7 review): the model
    # always reflects HEAD's column set (e.g. `account_id`, added by a much
    # later migration than d6e7f8a9b0c1), not whatever this checkpoint's
    # actual DB schema looks like. `name` is Fernet-encrypted at rest as of
    # migration 379fdb627ee8, already applied by this revision — insert
    # ciphertext directly, matching what the ORM's EncryptedString
    # TypeDecorator would have produced.
    with engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO holdings (id, user_id, name, pricing_mode, currency, asset_class) "
                "VALUES (gen_random_uuid(), :user_id, :name, 'auto', 'USD', 'STOCK')"
            ),
            {"user_id": uid, "name": encrypt_value("Seed")},
        )
        conn.commit()

    command.upgrade(alembic_cfg, "e8f9a0b1c2d3")
    # The binding behavior under test is fully decided by this one
    # migration; upgrading the rest of the way to head before the ORM read
    # avoids this test breaking every time a later migration adds a nullable
    # User column (e.g. issue #220's tos_accepted_at) that the ORM model —
    # but not yet the DB at this exact revision — expects to select.
    command.upgrade(alembic_cfg, "head")
    with Session(engine) as check:
        row = check.get(User, uid)
        assert row is not None
        assert row.email == _gs().DEV_USER_EMAIL.strip().lower()
        assert row.auth_subject is None
    engine.dispose()


def test_users_migration_refuses_unexpected_user_ids(alembic_cfg: Config) -> None:
    from sqlalchemy import text

    from app.core.encryption import encrypt_value

    command.upgrade(alembic_cfg, "d6e7f8a9b0c1")
    engine = create_engine(get_settings().database_url)
    # Raw SQL, not the Holding ORM model — see the sibling test above for why.
    with engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO holdings (id, user_id, name, pricing_mode, currency, asset_class) "
                "VALUES (gen_random_uuid(), :user_id, :name, 'auto', 'USD', 'STOCK')"
            ),
            {"user_id": uuid.uuid4(), "name": encrypt_value("Orphan")},
        )
        conn.commit()

    with pytest.raises(Exception, match="unexpected user_id"):
        command.upgrade(alembic_cfg, "e8f9a0b1c2d3")
    engine.dispose()
