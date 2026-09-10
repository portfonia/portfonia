# Instrument symbol normalization (issue #57)

### Shared `instrument_symbols` module, three sequential stages

Before #57, ticker/fund-code normalization lived only in `_yfinance.py`
(`_normalize_ticker`, HK canonicalization, the `PSH -> PSH.L` collision
override table) and was reimplemented/duplicated at several read sites
(`window_data.py`, `ticker_intel.py`, `report_assembly.py`, `user_scope.py`,
`ticker_leverage.py`), plus a second suffix/market-bucket table inside
`app.services.markets` and a third inside `holding_parser.py`. #57's frozen
design (GitHub issue #57 comment
[5551712656](https://github.com/portfonia/portfonia/issues/57#issuecomment-5551712656))
consolidated all three into `app/services/instrument_symbols.py` as the one
rule-table authority, delivered as exactly three sequential, independently
reviewed PRs — no combined implementation, no reordering:

- **57-1** (PR #359, merged `dd01f0d`): extracted `_yfinance._normalize_ticker`
  verbatim into `instrument_symbols.normalize_legacy_ticker`, with
  `_yfinance._normalize_ticker` kept as a one-line forwarding shim and a
  byte-for-byte golden fixture
  (`app/tests/fixtures/legacy_ticker_normalization_golden.json`, 23 cases)
  recorded from the pre-refactor implementation. No resolver, no consumer
  migration.
- **57-2** (PR #361, merged `a9480c7`): added the typed `resolve_instrument`/
  `to_provider_symbol`/`build_provider_request_plan` contracts and migrated
  the holdings write path and price consumers (`price_capture`,
  `price_fetcher`, `portfolio_calculator`, `technical_position`,
  `routers.holdings` sparse-history, the yfinance/Finnhub/Massive provider
  boundaries, plus Tencent `sh`/`sz` ticker adaptation in issue #389) plus the market/suffix classification tables that used to live
  separately in `app.services.markets` and `holding_parser.py`. Left a
  checked-in allowlist of five still-pending intelligence/report consumers.
- **57-3** (this stage): migrated the remaining five consumers
  (`report_assembly`, `user_scope`, `ticker_leverage`, `ticker_intel`,
  `window_data`) to `instrument_symbols.intelligence_identifier`, removed
  the `_yfinance._normalize_ticker` forwarding shim and the
  `_TICKER_SYMBOL_OVERRIDE` re-export, and tightened the repo-wide
  zero-dependency check (`app/tests/test_issue_57_normalization_migration.py`)
  to an empty allowlist — no business module may import or attribute-access
  a provider-private normalization symbol from `_yfinance` anymore. Also
  widened `frontend/src/lib/api.ts`'s `Market` type from 4 to the full
  8-value backend taxonomy (`US`/`HK`/`A-Share`/`UK`/`Europe`/`Japan`/
  `Korea`/`Other`) as an isolated commit — the holdings form already
  offered all eight values via an `as ParsedRow["market"]` cast, so this was
  a type-accuracy fix, not a behavior change.

### Ownership matrix (frozen, unchanged by any stage)

| Interface | Owns | Must not own |
|---|---|---|
| `resolve_instrument` | Pure composition of the write-time rules: HK canonicalization, confirmed suffix decision, market/support resolution, ambiguity override, typed lookup key. | ORM/network access, persistence, reclassification of historical rows on reads. |
| `normalize_legacy_ticker` | Exact old `_yfinance._normalize_ticker` behavior: PSH override then HK normalization, pass-through otherwise. | New casing/currency/suffix rules. |
| `intelligence_identifier` | The intelligence-cache convention: `normalize_legacy_ticker(key.code).upper()`, bare string for either kind — this is what 57-3's five consumers now call instead of `_normalize_ticker(...).upper()`. | Writes, new kind-prefix casing, directory lookups. |
| `markets.resolve_holding_market` | Compatibility facade delegating to one pure policy implementation. | A second suffix table. |
| `holding_parser`'s two wrappers | Thin compatibility surfaces preserving `emit_note`/note ordering. | Independent inference tables or a duplicate HK implementation. |

`intelligence_identifier` ignores `InstrumentKey.kind` — the returned string
is identical whether the caller passes `"ticker"` or `"fund_code"` for a
given code, since `normalize_legacy_ticker` only inspects the code string.
Every 57-3 call site therefore passes `InstrumentKey("ticker", raw)`
regardless of whether `raw` actually came from `Holding.ticker` or
`Holding.fund_code` (both did, via the existing `h.ticker or h.fund_code`
pattern) — this is correct, not a kind-tracking bug, precisely because the
function does not use `kind`.

### Known baseline discrepancy preserved as-is (not a #57 bug)

The frozen design's ambiguity matrix illustrates "both `ticker=AAPL` and
`fund_code=005827`" resolving to `US`/`resolved`. The actual pre-#57
`_confirmed_market` checks `fund_code` before `ticker`/`currency`, so this
exact input resolves `ambiguous`/`A-Share`/`capture_supported=False` in the
shipped 57-2 implementation — only the *lookup key* keeps ticker precedence
(`InstrumentKey("ticker", "AAPL")`). Per the frozen design's "surface a
baseline discrepancy, don't silently repair it" instruction, this is
preserved exactly as shipped; see
`test_both_ticker_and_fund_code_retain_ticker_precedence_for_the_key` in
`app/tests/test_instrument_symbols_resolve.py`. Any fix to
`_confirmed_market`'s field-priority order is a separate, later product
decision, not part of #57.

### Zero-dependency check, and the two ways a "provider-private import" happens

`app/tests/test_issue_57_normalization_migration.py`'s AST-based checker
looks for two distinct patterns, because the pre-57-3 codebase actually used
both: `from app.services._yfinance import _normalize_ticker` (a direct
`ImportFrom`), and `from app.services import _yfinance` followed by
`_yfinance._normalize_ticker(...)` (a module-alias attribute access — this
is how `test_instrument_symbols.py`'s now-removed shim tests exercised it).
The checker's `_ALLOWED_REMAINING_IMPORTERS` allowlist went from the five
57-3 consumers (57-2 state) to empty (57-3 state); a future reintroduction
of either import form anywhere outside `app/tests/` fails this test
immediately.

### What #57 deliberately does not do

No security-existence directory, no new `Holding`/API fields, no schema or
storage-key migration, no historical-row rewrite, no cache purge. `status`/
`capture_supported` describe format resolvability, never security
existence — that is issue #58's scope, tracked separately and not advanced
by any #57 stage.

### Removal of the PSH ticker-collision override (issue #417)

The `_TICKER_SYMBOL_OVERRIDE` table this section's history above describes
(`{"PSH": "PSH.L"}`, added by issue #204 to stop a bare "PSH" holding
resolving to an unrelated US-listed security on yfinance) was deliberately
removed, along with its read sites in `normalize_legacy_ticker` and
`_confirmed_market`. Product decision, not a bug fix: a per-ticker
exception hardcoded into `instrument_symbols.py` doesn't scale (every future
collision needs its own table entry) and is asymmetric risk (any other
user genuinely holding the collision security would be silently
misclassified). It is also inconsistent with how the module already treats
every *other* dual-listed ADR/ordinary collision (VOD, BP, RIO, ...) —
issue #313 item 5 deliberately never added those to a table; a bare ticker
with no declared market/currency defaults to `market_from_ticker`'s
"no suffix -> US" fallback and is left for the user to notice (via the
holdings preview/confirm review, or the holdings table afterward) and
correct, by declaring a market/currency or editing the stored ticker. PSH
now gets exactly the same treatment, no exception.

**What did not change**: the general currency/market-declared suffix-forcing
path in `_confirmed_market` (GBP/HKD/CNY/EUR/JPY/KRW -> the matching
market, then suffix). A holding entered with `currency="GBP"` (or
`market="UK"`) still resolves to `PSH.L`/UK exactly as before — that path
never depended on the removed table. **What did change**: a fully bare
ticker with *neither* market nor currency declared no longer gets rescued;
`resolve_instrument`'s `key` and its write-fields agree in every case now
(previously, a holding with `market="US"` explicitly declared kept `PSH`
as its stored ticker while the internal lookup `key` still silently
resolved to `PSH.L` — a documented "historical mismatch" row in the frozen
design that no longer exists as a distinct case).

Considered and declined alternatives (full reasoning, evidence, and the
production-data audit that ruled out a migration: GitHub issue #417's
Exploration/Reasons comments): moving the table to a DB row mirroring
`ticker_leverage_overrides` (issue #87's shipped per-ticker-exception
precedent) — doesn't address the actual objection, which is "no ticker
gets special treatment," not "don't hardcode it in Python specifically";
and adding a forced-confirmation warning for any bare ticker with no
declared market/currency — would add friction to the common,
already-correct bare-US-ticker path (AAPL, MSFT, ...).

No production data migration: the one production holding referencing PSH
was already stored as `PSH.L`/`UK` at the time of this change (created via
the general currency-declared path, not the removed override), so removal
has zero effect on existing rows.
