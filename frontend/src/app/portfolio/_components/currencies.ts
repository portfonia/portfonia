// Mirrors backend/app/schemas/holdings.py's VALID_CURRENCIES (15 entries) —
// hand-copied, same convention as lib/api.ts's header comment: Ring 1 will
// replace hand-written mirrors with OpenAPI-generated types. Keep in sync.
//
// Where each currency list actually takes effect (issue #354 split this into
// three, after the pre-#354 single BASE_CURRENCIES/BaseCurrency pair had
// silently grown two different real consumers with different currency
// scopes — clarifying this mapping explicitly so a future edit to one
// doesn't accidentally narrow the other):
// - BASE_CURRENCIES / BaseCurrency: the full 15-entry schema-level set. Used
//   as the general currency-code type everywhere, AND as the actual option
//   list for the /profile page's report-currency setting
//   (use-report-currency.ts / profile-page-body.tsx) — a different feature
//   (per-user report currency, issue #350 item 1) that this issue does not
//   touch or narrow.
// - PORTFOLIO_DISPLAY_CURRENCIES (below): what the /portfolio page's
//   CurrencySwitcher lists as menu items.
// - PORTFOLIO_NORMALIZATION_TARGETS (below): the subset of those that are
//   actually clickable in that same switcher.
export const BASE_CURRENCIES = [
  "USD",
  "CNY",
  "CNH",
  "HKD",
  "GBP",
  "EUR",
  "JPY",
  "SGD",
  "AUD",
  "CAD",
  "CHF",
  "KRW",
  "TWD",
  "MOP",
  "NZD",
] as const;

export type BaseCurrency = (typeof BASE_CURRENCIES)[number];

export const DEFAULT_BASE_CURRENCY: BaseCurrency = "USD";

// issue #354: the /portfolio base-currency switcher (CurrencySwitcher) lists
// exactly these 7 currencies — everything else in BASE_CURRENCIES (JPY/SGD/
// AUD/CAD/CHF/KRW/NZD/MOP) is left off the menu entirely for now. This is a
// switcher-menu-contents decision only, scoped to that one control: it does
// not gate by_currency rows, holdings display, or fx_rates_as_of — a holding
// or a fx_rates_as_of entry in one of the other 8 currencies still renders
// normally everywhere else on the page. Product-owner decision (issue #354
// comment 3, item 1), not derived from any technical constraint.
export const PORTFOLIO_DISPLAY_CURRENCIES = [
  "USD",
  "CNY",
  "CNH",
  "GBP",
  "HKD",
  "TWD",
  "EUR",
] as const satisfies readonly BaseCurrency[];

// Of the 7 PORTFOLIO_DISPLAY_CURRENCIES, only these are actually selectable
// as the normalization target today — the other 4 still appear in the
// switcher menu (with a greyed-out flag) but are disabled, not hidden
// (issue #354 comment 3, item 3 + product-owner clarification during this
// issue's implementation: all 7 are listed, only 3 are clickable — corrected
// from an initial USD/CNY-only implementation after review, since HKD's own
// FX pair (USDHKD) is not one of the ones this issue's root-cause fix left
// in question).
export const PORTFOLIO_NORMALIZATION_TARGETS = [
  "USD",
  "CNY",
  "HKD",
] as const satisfies readonly BaseCurrency[];
