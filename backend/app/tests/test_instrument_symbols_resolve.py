"""Stage 57-2: `resolve_instrument`/`to_provider_symbol`/provider-plan contract.

Covers the frozen ambiguity/exception matrix (issue #57 frozen design,
https://github.com/portfonia/portfonia/issues/57#issuecomment-5551712656,
section 5) and the provider boundary contract (section 4). These are pure
unit tests — no DB, no network — the DB-integration slice of the same
contract lives in `test_price_capture.py`/`test_holdings_router.py`.
"""

from __future__ import annotations

import pytest

from app.services.instrument_symbols import (
    AssetType,
    InstrumentInput,
    InstrumentKey,
    Market,
    ProviderRequestPlan,
    build_provider_request_plan,
    intelligence_identifier,
    normalize_legacy_ticker,
    resolve_instrument,
    to_provider_symbol,
)


def _auto(
    *,
    ticker: str | None = None,
    fund_code: str | None = None,
    market: Market | None = None,
    currency: str | None = None,
    asset_type: AssetType | None = "stock",
) -> InstrumentInput:
    return InstrumentInput(
        ticker=ticker,
        fund_code=fund_code,
        market=market,
        currency=currency,
        asset_type=asset_type,
        pricing_mode="auto",
    )


class TestAmbiguityMatrix:
    """One test per row of the frozen design's section-5 matrix table."""

    def test_europe_bare_ticker_is_ambiguous(self) -> None:
        r = resolve_instrument(_auto(ticker="ASML", currency="EUR"))
        assert r.status == "ambiguous"
        assert r.market == "Europe"
        assert r.exchange is None
        assert r.capture_supported is False
        assert r.ticker == "ASML"
        assert r.key == InstrumentKey("ticker", "ASML")

    def test_europe_suffixed_ticker_is_resolved(self) -> None:
        r = resolve_instrument(_auto(ticker="ASML.AS", currency="EUR"))
        assert r.status == "resolved"
        assert r.market == "Europe"
        assert r.exchange == "EURONEXT_AMSTERDAM"
        assert r.capture_supported is True
        assert r.ticker == "ASML.AS"

    def test_korea_bare_ticker_is_ambiguous(self) -> None:
        r = resolve_instrument(_auto(ticker="XYZKR", currency="KRW"))
        assert r.status == "ambiguous"
        assert r.market == "Korea"
        assert r.exchange is None
        assert r.capture_supported is False

    def test_a_share_unplaceable_code_is_ambiguous(self) -> None:
        r = resolve_instrument(_auto(ticker="800001", currency="CNY"))
        assert r.status == "ambiguous"
        assert r.market == "A-Share"
        assert r.exchange is None
        assert r.capture_supported is False
        assert r.ticker == "800001"

    def test_bare_us_ticker_with_currency_is_resolved(self) -> None:
        r = resolve_instrument(_auto(ticker="AAPL", currency="USD"))
        assert r.status == "resolved"
        assert r.market == "US"
        assert r.exchange is None
        assert r.capture_supported is True
        assert r.key == InstrumentKey("ticker", "AAPL")

    def test_bare_us_ticker_no_hints_is_resolved(self) -> None:
        r = resolve_instrument(_auto(ticker="AAPL"))
        assert r.status == "resolved"
        assert r.market == "US"
        assert r.exchange is None
        assert r.capture_supported is True

    def test_bare_gbp_ticker_force_suffixes_uk(self) -> None:
        r = resolve_instrument(_auto(ticker="VOD", currency="GBP"))
        assert r.status == "resolved"
        assert r.market == "UK"
        assert r.exchange == "LSE"
        assert r.capture_supported is True
        assert r.ticker == "VOD.L"
        assert r.key == InstrumentKey("ticker", "VOD.L")

    def test_bare_hk_numeric_force_suffixes_hk(self) -> None:
        r = resolve_instrument(_auto(ticker="777", currency="HKD"))
        assert r.status == "resolved"
        assert r.market == "HK"
        assert r.exchange == "HKEX"
        assert r.capture_supported is True
        assert r.ticker == "0777.HK"

    def test_unsupported_suffix_stays_other(self) -> None:
        r = resolve_instrument(_auto(ticker="BHP.AX", currency="AUD"))
        assert r.status == "unsupported"
        assert r.market == "Other"
        assert r.exchange is None
        assert r.capture_supported is False
        assert r.ticker == "BHP.AX"
        assert r.key == InstrumentKey("ticker", "BHP.AX")

    def test_clean_cash_with_identifiers_already_removed_is_not_applicable(self) -> None:
        r = resolve_instrument(
            InstrumentInput(
                ticker=None,
                fund_code=None,
                market=None,
                currency="USD",
                asset_type="cash",
                pricing_mode="manual",
            )
        )
        assert r.status == "not_applicable"
        assert r.market == "Other"
        assert r.capture_supported is True
        assert r.key is None

    def test_manual_label_with_declared_market_is_not_applicable(self) -> None:
        r = resolve_instrument(
            InstrumentInput(
                ticker="HOME",
                fund_code=None,
                market="HK",
                currency="HKD",
                asset_type="other",
                pricing_mode="manual",
            )
        )
        assert r.status == "not_applicable"
        assert r.market == "HK"
        assert r.capture_supported is True
        assert r.key is None
        assert r.ticker == "HOME"

    def test_manual_label_unresolvable_ticker_shape_is_not_applicable(self) -> None:
        r = resolve_instrument(
            InstrumentInput(
                ticker="HOME.AX",
                fund_code=None,
                market=None,
                currency="AUD",
                asset_type="other",
                pricing_mode="manual",
            )
        )
        assert r.status == "not_applicable"
        assert r.market == "Other"
        assert r.capture_supported is False
        assert r.key is None
        assert r.ticker == "HOME.AX"

    def test_fund_only_is_resolved(self) -> None:
        r = resolve_instrument(_auto(fund_code="005827", currency="CNY", asset_type="fund"))
        assert r.status == "resolved"
        assert r.market == "A-Share"
        assert r.exchange is None
        assert r.capture_supported is True
        assert r.key == InstrumentKey("fund_code", "005827")
        assert r.fund_code == "005827"

    def test_ticker_that_looks_like_a_fund_code_is_not_reinterpreted(self) -> None:
        r = resolve_instrument(_auto(ticker="005827", currency="USD"))
        assert r.status == "resolved"
        assert r.market == "US"
        assert r.key == InstrumentKey("ticker", "005827")

    def test_ticker_digit_string_with_a_share_market_gets_suffix(self) -> None:
        r = resolve_instrument(_auto(ticker="005827", market="A-Share", currency="CNY"))
        assert r.status == "resolved"
        assert r.market == "A-Share"
        assert r.exchange == "SZSE"
        assert r.ticker == "005827.SZ"
        assert r.key == InstrumentKey("ticker", "005827.SZ")

    def test_both_ticker_and_fund_code_retain_ticker_precedence_for_the_key(self) -> None:
        """The LOOKUP KEY retains ticker precedence (matches the fund-code-
        absent case's `T(AAPL)` — never a fund_code key when a ticker is
        present) and both original fields are preserved on the row.

        NOTE — documented baseline discrepancy from the frozen design's
        illustrative matrix (section 5), which shows this exact input as
        `resolved | US | null | true`: the actual pre-#57 `_confirmed_market`
        (ported byte-for-byte here, not re-derived) checks `fund_code` before
        the ticker/currency branches, so a row carrying BOTH fields gets its
        MARKET derivation forced onto the fund's A-Share bucket — "AAPL" is
        not a valid A-share code, so `_a_share_suffix` returns None and the
        row lands `ambiguous`/`capture_supported=False`, not `resolved`/`US`.
        This refactor preserves the real, pre-existing behavior rather than
        silently "fixing" it to match the matrix's simplified illustration
        (frozen design section 8: an unexpected baseline discrepancy must be
        surfaced, not silently repaired) — flagged in the 57-2 PR for review.
        """
        r = resolve_instrument(
            _auto(ticker="AAPL", fund_code="005827", currency="USD"),
        )
        assert r.status == "ambiguous"
        assert r.market == "A-Share"
        assert r.capture_supported is False
        assert r.key == InstrumentKey("ticker", "AAPL")
        assert r.ticker == "AAPL"
        assert r.fund_code == "005827"

    def test_psh_no_declared_market_resolves_uk(self) -> None:
        r = resolve_instrument(_auto(ticker="PSH", currency="GBP"))
        assert r.status == "resolved"
        assert r.market == "UK"
        assert r.exchange == "LSE"
        assert r.capture_supported is True
        assert r.ticker == "PSH.L"
        assert r.key == InstrumentKey("ticker", "PSH.L")

    def test_historical_psh_declared_us_keeps_ticker_but_lookup_key_is_lse(self) -> None:
        """Frozen design section 5's deliberate legacy-mismatch row: the
        persisted bucket is never repaired here, but the LEGACY LOOKUP key
        (and its exchange) still reflects the code's real venue."""
        r = resolve_instrument(_auto(ticker="PSH", market="US", currency="GBP"))
        assert r.status == "resolved"
        assert r.market == "US"
        assert r.exchange == "LSE"
        assert r.capture_supported is True
        assert r.ticker == "PSH"  # proposed write value is NOT rewritten
        assert r.key == InstrumentKey("ticker", "PSH.L")  # lookup key still uses the override


class TestResolveInstrumentNoNewNoteCodes:
    def test_no_ticker_no_fund_code_auto_is_not_applicable(self) -> None:
        r = resolve_instrument(_auto())
        assert r.status == "not_applicable"
        assert r.key is None

    def test_notes_are_ordered_hk_then_currency_then_suffix(self) -> None:
        r = resolve_instrument(_auto(ticker="02333.hk", currency="USD"))
        codes = [n.code for n in r.notes]
        assert codes == ["ticker_normalized_hk", "currency_corrected"]

    def test_ambiguous_note_names_market_and_legal_suffixes(self) -> None:
        r = resolve_instrument(_auto(ticker="XYZ123", currency="EUR"))
        note = next(n for n in r.notes if n.code == "ticker_suffix_ambiguous")
        assert note.params["market"] == "Europe"
        assert note.params["suffixes"] == ".AS / .PA / .DE"


class TestIntelligenceIdentifier:
    def test_uses_legacy_normalization_plus_upper(self) -> None:
        key = InstrumentKey("ticker", "psh")
        assert intelligence_identifier(key) == normalize_legacy_ticker("psh").upper()
        assert intelligence_identifier(key) == "PSH.L"

    def test_fund_code_identifier_is_bare_string(self) -> None:
        key = InstrumentKey("fund_code", "005827")
        assert intelligence_identifier(key) == "005827"


class TestToProviderSymbol:
    def test_yfinance_returns_legacy_normalized_code_unchanged(self) -> None:
        assert to_provider_symbol("yfinance", InstrumentKey("ticker", "0777.HK")) == "0777.HK"
        assert to_provider_symbol("yfinance", InstrumentKey("ticker", "PSH.L")) == "PSH.L"
        assert to_provider_symbol("yfinance", InstrumentKey("ticker", "AAPL")) == "AAPL"
        assert to_provider_symbol("yfinance", InstrumentKey("ticker", "BRK.B")) == "BRK.B"

    def test_yfinance_returns_none_for_fund_code(self) -> None:
        assert to_provider_symbol("yfinance", InstrumentKey("fund_code", "005827")) is None

    @pytest.mark.parametrize("provider", ["finnhub", "massive"])
    def test_us_fallbacks_accept_us_syntax_unchanged(self, provider: str) -> None:
        assert to_provider_symbol(provider, InstrumentKey("ticker", "AAPL")) == "AAPL"  # type: ignore[arg-type]
        assert to_provider_symbol(provider, InstrumentKey("ticker", "BRK.B")) == "BRK.B"  # type: ignore[arg-type]

    @pytest.mark.parametrize("provider", ["finnhub", "massive"])
    def test_us_fallbacks_reject_non_us_code(self, provider: str) -> None:
        assert to_provider_symbol(provider, InstrumentKey("ticker", "0700.HK")) is None  # type: ignore[arg-type]
        assert to_provider_symbol(provider, InstrumentKey("ticker", "PSH.L")) is None  # type: ignore[arg-type]

    @pytest.mark.parametrize("provider", ["finnhub", "massive"])
    def test_us_fallbacks_reject_fund_code(self, provider: str) -> None:
        assert to_provider_symbol(provider, InstrumentKey("fund_code", "005827")) is None  # type: ignore[arg-type]


class TestBuildProviderRequestPlan:
    def test_forward_and_inverse_map_agree(self) -> None:
        keys = [InstrumentKey("ticker", "AAPL"), InstrumentKey("ticker", "BRK.B")]
        plan = build_provider_request_plan("yfinance", keys)
        assert set(plan.wire_symbols) == {"AAPL", "BRK.B"}
        assert plan.to_internal == {"AAPL": "AAPL", "BRK.B": "BRK.B"}
        assert plan.unsupported == ()
        assert plan.collisions == {}

    def test_unsupported_codes_produce_zero_wire_symbols(self) -> None:
        keys = [InstrumentKey("fund_code", "005827"), InstrumentKey("ticker", "0700.HK")]
        plan = build_provider_request_plan("finnhub", keys)
        assert plan.wire_symbols == ()
        assert set(plan.unsupported) == {"005827", "0700.HK"}
        assert plan.collisions == {}

    def test_duplicate_input_code_is_deduplicated_not_a_collision(self) -> None:
        keys = [InstrumentKey("ticker", "AAPL"), InstrumentKey("ticker", "AAPL")]
        plan = build_provider_request_plan("yfinance", keys)
        assert plan.wire_symbols == ("AAPL",)
        assert plan.collisions == {}

    def test_distinct_codes_colliding_on_one_wire_symbol_are_excluded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real collision (two DISTINCT internal codes mapping to the same
        wire symbol) cannot happen under today's identity-based providers —
        this proves the exclusion logic itself works, using a stubbed
        `to_provider_symbol` that deliberately collapses two codes."""
        import app.services.instrument_symbols as ins_mod

        def _colliding(provider: str, key: InstrumentKey) -> str | None:
            return "SAME"

        monkeypatch.setattr(ins_mod, "to_provider_symbol", _colliding)
        keys = [InstrumentKey("ticker", "FOO"), InstrumentKey("ticker", "BAR")]
        plan = build_provider_request_plan("yfinance", keys)
        assert plan.wire_symbols == ()
        assert plan.to_internal == {}
        assert plan.collisions == {"SAME": ("FOO", "BAR")}

    def test_plan_is_the_frozen_dataclass_shape(self) -> None:
        plan = build_provider_request_plan("yfinance", [InstrumentKey("ticker", "AAPL")])
        assert isinstance(plan, ProviderRequestPlan)
