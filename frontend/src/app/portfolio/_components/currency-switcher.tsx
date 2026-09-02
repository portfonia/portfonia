"use client";

import { useTranslations } from "next-intl";

import { BASE_CURRENCIES, type BaseCurrency } from "./currencies";

export function CurrencySwitcher({
  value,
  onChange,
  disabled,
}: {
  value: BaseCurrency;
  onChange: (currency: BaseCurrency) => void;
  disabled?: boolean;
}) {
  const t = useTranslations("portfolio");
  return (
    <label className="flex items-center gap-2 text-sm">
      <span className="text-foreground/80">{t("currencyLabel")}</span>
      <select
        className="rounded-md border border-input bg-background px-2 py-1 text-sm disabled:opacity-50"
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value as BaseCurrency)}
      >
        {BASE_CURRENCIES.map((currency) => (
          <option key={currency} value={currency}>
            {currency}
          </option>
        ))}
      </select>
    </label>
  );
}
