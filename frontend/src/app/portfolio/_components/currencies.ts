// Mirrors backend/app/schemas/holdings.py's VALID_CURRENCIES (15 entries) —
// hand-copied, same convention as lib/api.ts's header comment: Ring 1 will
// replace hand-written mirrors with OpenAPI-generated types. Keep in sync.
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
