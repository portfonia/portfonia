"""Issue #791 data migration round trip on real Postgres."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from alembic.config import Config
from sqlalchemy import ARRAY, Numeric, Text, bindparam, create_engine, inspect, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from alembic import command
from app.core.config import get_settings
from app.models.macro_coverage import MacroCoverage
from app.models.portfolio_value_snapshot import PortfolioValueSnapshot
from app.models.report import Report
from app.models.upload_job import UploadJob
from app.tests.conftest import TEST_USER_ID, seed_user


def test_derived_encryption_migration(alembic_cfg: Config) -> None:
    command.upgrade(alembic_cfg, "d78300000001")
    engine = create_engine(get_settings().database_url)
    ids = {
        name: [uuid.uuid4(), uuid.uuid4()]
        for name in ("reports", "portfolio_value_snapshots", "upload_jobs", "macro_coverage")
    }
    inputs = {"holdings": [{"ticker": "AAA"}], "label": "café"}
    preview = {"rows": ["AAA"]}
    try:
        with Session(engine) as session:
            seed_user(session, TEST_USER_ID)
            session.commit()
        with engine.begin() as conn:
            for i in range(2):
                conn.execute(
                    text("""INSERT INTO reports
                    (id, user_id, report_date, report_type, session_node, status,
                     report_md, report_html, report_inputs)
                    VALUES (:id, :user, :day, 'daily', :node, 'success', :md, :html, :inputs)
                """).bindparams(bindparam("inputs", type_=JSONB(none_as_null=True))),
                    {
                        "id": ids["reports"][i],
                        "user": TEST_USER_ID,
                        "day": date(2026, 10, 17),
                        "node": str(uuid.uuid4()),
                        "md": "report" if i == 0 else None,
                        "html": "<p>report</p>" if i == 0 else None,
                        "inputs": inputs if i == 0 else None,
                    },
                )
                conn.execute(
                    text("""INSERT INTO portfolio_value_snapshots
                    (id, user_id, snapshot_date, currency, base_currency,
                     market_value, market_value_base, cost_basis_base)
                    VALUES (:id, :user, :day, 'USD', 'USD', :value, :value, :value)
                """),
                    {
                        "id": ids["portfolio_value_snapshots"][i],
                        "user": TEST_USER_ID,
                        "day": date(2026, 10, 17),
                        "value": Decimal("12.34") if i == 0 else None,
                    },
                )
                conn.execute(
                    text("""INSERT INTO upload_jobs
                    (id, user_id, filename, status, raw_text, preview)
                    VALUES (:id, :user, 'holdings.txt', 'success', :raw, :preview)
                """).bindparams(bindparam("preview", type_=JSONB(none_as_null=True))),
                    {
                        "id": ids["upload_jobs"][i],
                        "user": TEST_USER_ID,
                        "raw": "uploaded" if i == 0 else None,
                        "preview": preview if i == 0 else None,
                    },
                )
                conn.execute(
                    text("""INSERT INTO macro_coverage
                    (id, user_id, report_id, development_key, as_of, coverage_mode,
                     depth_tier, affected_identifiers, paragraph_text)
                    VALUES (:id, :user, :report, :key, :asof, 'NEW', 'anchor', :affected, :paragraph)
                """),
                    {
                        "id": ids["macro_coverage"][i],
                        "user": TEST_USER_ID,
                        "report": ids["reports"][i],
                        "key": str(uuid.uuid4()),
                        "asof": datetime.now(UTC),
                        "affected": ["AAA"] if i == 0 else [],
                        "paragraph": "coverage" if i == 0 else None,
                    },
                )
        command.upgrade(alembic_cfg, "head")
        with Session(engine) as session:
            for i in range(2):
                report = session.scalars(select(Report).where(Report.id == ids["reports"][i])).one()
                assert report.report_md == ("report" if i == 0 else None)
                assert report.report_html == ("<p>report</p>" if i == 0 else None)
                assert report.report_inputs == (inputs if i == 0 else None)
                snapshot = session.get(PortfolioValueSnapshot, ids["portfolio_value_snapshots"][i])
                assert snapshot is not None
                for col in ("market_value", "market_value_base", "cost_basis_base"):
                    assert getattr(snapshot, col) == (Decimal("12.34") if i == 0 else None)
                upload = session.get(UploadJob, ids["upload_jobs"][i])
                assert upload is not None
                assert upload.raw_text == ("uploaded" if i == 0 else None)
                assert upload.preview == (preview if i == 0 else None)
                coverage = session.get(MacroCoverage, ids["macro_coverage"][i])
                assert coverage is not None
                assert coverage.paragraph_text == ("coverage" if i == 0 else None)
                assert coverage.affected_identifiers == (["AAA"] if i == 0 else [])
        # Without #791 the preceding reads pass, but the storage remains plaintext.
        with engine.connect() as conn:
            raw = conn.execute(
                text("SELECT report_md FROM reports WHERE id = :id"), {"id": ids["reports"][0]}
            ).scalar_one()
            assert raw != "report"
        command.downgrade(alembic_cfg, "d78300000001")
        expected: dict[str, dict[str, tuple[type[object], object]]] = {
            "reports": {
                "report_md": (Text, "report"),
                "report_html": (Text, "<p>report</p>"),
                "report_inputs": (JSONB, inputs),
            },
            "portfolio_value_snapshots": {
                col: (Numeric, Decimal("12.34"))
                for col in ("market_value", "market_value_base", "cost_basis_base")
            },
            "upload_jobs": {"raw_text": (Text, "uploaded"), "preview": (JSONB, preview)},
            "macro_coverage": {
                "paragraph_text": (Text, "coverage"),
                "affected_identifiers": (ARRAY, ["AAA"]),
            },
        }
        with engine.connect() as conn:
            for table, columns in expected.items():
                schema = {c["name"]: c for c in inspect(engine).get_columns(table)}
                for col, (kind, value) in columns.items():
                    assert isinstance(schema[col]["type"], kind)
                    for i in range(2):
                        raw = conn.execute(
                            text(f"SELECT {col} FROM {table} WHERE id = :id"),
                            {"id": ids[table][i]},
                        ).scalar_one()
                        assert raw == (
                            value if i == 0 else ([] if col == "affected_identifiers" else None)
                        )
                if table == "macro_coverage":
                    assert not schema["affected_identifiers"]["nullable"]
                    default = schema["affected_identifiers"]["default"]
                    assert default is not None and "{}" in default
        command.upgrade(alembic_cfg, "head")
    finally:
        engine.dispose()
