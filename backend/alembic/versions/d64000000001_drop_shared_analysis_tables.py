"""Drop the L1/L2/L3 shared-analysis caches and the report-time search cache.

Issue #640. `ticker_intel`, `macro_event_intel` and `cross_name_intel` held
L1/L2/L3 shared analysis that no shipped report ever read; `search_cache` held
report-time search results, removed in #622. Upgrade deletes their rows
irreversibly. Downgrade recreates the four tables empty with their previous
schema; data is not restored.

Revision ID: d64000000001
Revises: d64400000001
"""

from alembic import op

revision: str = "d64000000001"
down_revision: str | None = "d64400000001"
branch_labels: str | None = None
depends_on: str | None = None

_TABLES = ("cross_name_intel", "macro_event_intel", "ticker_intel", "search_cache")


def upgrade() -> None:
    for table in _TABLES:
        op.drop_table(table)


def downgrade() -> None:
    op.execute(
        """
        CREATE TABLE cross_name_intel (
            id uuid DEFAULT gen_random_uuid() NOT NULL,
            trade_date date NOT NULL,
            prompt_version text NOT NULL,
            input_fingerprint text NOT NULL,
            model text NOT NULL,
            clusters jsonb,
            attempt_count integer DEFAULT 1 NOT NULL,
            facts jsonb NOT NULL,
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            CONSTRAINT pk_cross_name_intel PRIMARY KEY (id),
            CONSTRAINT uq_cross_name_intel_date_version_fingerprint
                UNIQUE (trade_date, prompt_version, input_fingerprint)
        );
        CREATE INDEX ix_cross_name_intel_trade_date ON cross_name_intel (trade_date);

        CREATE TABLE macro_event_intel (
            id uuid DEFAULT gen_random_uuid() NOT NULL,
            event_key text NOT NULL,
            trade_date date NOT NULL,
            prompt_version text NOT NULL,
            model text NOT NULL,
            analysis text,
            affected_asset_classes jsonb NOT NULL,
            facts jsonb NOT NULL,
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            attempt_count integer DEFAULT 1 NOT NULL,
            CONSTRAINT pk_macro_event_intel PRIMARY KEY (id),
            CONSTRAINT uq_macro_event_intel_key_date_version
                UNIQUE (event_key, trade_date, prompt_version)
        );
        CREATE INDEX ix_macro_event_intel_event_key ON macro_event_intel (event_key);
        CREATE INDEX ix_macro_event_intel_trade_date ON macro_event_intel (trade_date);

        CREATE TABLE ticker_intel (
            id uuid DEFAULT gen_random_uuid() NOT NULL,
            identifier text NOT NULL,
            trade_date date NOT NULL,
            prompt_version text NOT NULL,
            model text NOT NULL,
            analysis text,
            facts jsonb NOT NULL,
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            attempt_count integer DEFAULT 1 NOT NULL,
            CONSTRAINT pk_ticker_intel PRIMARY KEY (id),
            CONSTRAINT uq_ticker_intel_identifier_date_version
                UNIQUE (identifier, trade_date, prompt_version)
        );
        CREATE INDEX ix_ticker_intel_identifier ON ticker_intel (identifier);
        CREATE INDEX ix_ticker_intel_trade_date ON ticker_intel (trade_date);

        CREATE TABLE search_cache (
            id uuid DEFAULT gen_random_uuid() NOT NULL,
            query_hash text NOT NULL,
            query text NOT NULL,
            trade_date date NOT NULL,
            results jsonb NOT NULL,
            created_at timestamp with time zone DEFAULT now() NOT NULL,
            CONSTRAINT pk_search_cache PRIMARY KEY (id),
            CONSTRAINT uq_search_cache_query_date UNIQUE (query_hash, trade_date)
        );
        CREATE INDEX ix_search_cache_query_hash ON search_cache (query_hash);
        CREATE INDEX ix_search_cache_trade_date ON search_cache (trade_date);
        """
    )
