// Locale/formatting helpers for the performance page (issue #360 Phase 2).
// The UI catalog locales are BCP-47-ish app codes ("zh-Hans"), not valid
// Intl locale tags — map to real tags before handing a date to
// Intl.DateTimeFormat. Percentage fields on the wire are ratios (0.0234 =
// 2.34%), the same convention as the rest of the portfolio API.

import type { Locale } from "@/locales";

export const INTL_LOCALES: Record<Locale, string> = {
  en: "en-US",
  "zh-Hans": "zh-CN",
  "zh-Hant": "zh-TW",
};

function toDate(iso: string): Date {
  // Parse as local midnight, never UTC — a date-only string interpreted as
  // UTC can render as the previous day in negative-offset timezones.
  const [year, month, day] = iso.split("-").map(Number);
  return new Date(year, month - 1, day);
}

export function formatShortDate(iso: string, locale: Locale): string {
  return new Intl.DateTimeFormat(INTL_LOCALES[locale], {
    month: "short",
    day: "numeric",
  }).format(toDate(iso));
}

export function formatFullDate(iso: string, locale: Locale): string {
  return new Intl.DateTimeFormat(INTL_LOCALES[locale], {
    year: "numeric",
    month: "short",
    day: "numeric",
  }).format(toDate(iso));
}

export function toRatio(value: string | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

// "+2.34%" / "-1.00%" — signed, ratio input (same convention as
// portfolio-helpers' formatPercent, kept local because the chart also needs
// the unsigned/plain forms below and importing across _components folders
// for two lines is not worth it).
export function formatSignedPct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const pct = value * 100;
  const sign = pct >= 0 ? "+" : "";
  return `${sign}${pct.toFixed(digits)}%`;
}

// Integer-percent form for axis ticks (no "+", no decimals — a 5-year axis
// with signed 2-decimal labels is unreadable).
export function formatTickPct(value: number): string {
  return `${(value * 100).toFixed(0)}%`;
}
