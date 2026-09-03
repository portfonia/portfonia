"use client";

import { useTranslations } from "next-intl";

import { CURRENCY_DISPLAY_MODES, type CurrencyDisplayMode } from "./portfolio-helpers";

// Local to the by-currency BreakdownChart card (issue #330) — plain <select>
// to match CurrencySwitcher's pattern, not the page-level base-currency
// control it sits next to.
export function CurrencyModeSwitcher({
  value,
  onChange,
}: {
  value: CurrencyDisplayMode;
  onChange: (mode: CurrencyDisplayMode) => void;
}) {
  const t = useTranslations("portfolio");
  return (
    <label className="flex items-center gap-2 text-sm">
      <span className="text-foreground/80">{t("currencyDisplayModeLabel")}</span>
      <select
        className="rounded-md border border-input bg-background px-2 py-1 text-sm"
        value={value}
        onChange={(e) => onChange(e.target.value as CurrencyDisplayMode)}
      >
        {CURRENCY_DISPLAY_MODES.map((mode) => (
          <option key={mode} value={mode}>
            {t(`currencyDisplayMode.${mode}`)}
          </option>
        ))}
      </select>
    </label>
  );
}
