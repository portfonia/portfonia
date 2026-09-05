"use client";

// Issue #354: rebuilt on the same MenuDropdown/MenuItemButton primitives
// LocaleSwitcher already uses (issue #350 item 4) instead of a plain native
// <select> — this was the last currency-facing control on /portfolio still
// visually inconsistent with the rest of the header. Flag icons are
// image-based (flag-icons npm package) for the same cross-platform-emoji-
// rendering reason LocaleSwitcher's header comment documents — not repeated
// here, see that file.
//
// Lists all 7 PORTFOLIO_DISPLAY_CURRENCIES, but only
// PORTFOLIO_NORMALIZATION_TARGETS are clickable — the rest render with a
// greyed-out flag and a disabled menu item (product-owner clarification
// during this issue's implementation: shown, not hidden, since normalization
// support for the other 5 is expected to widen later).
import "flag-icons/css/flag-icons.css";
import { ChevronDown } from "lucide-react";
import { useTranslations } from "next-intl";

import { MenuDropdown, MenuItemButton } from "@/components/ui/menu";
import { cn } from "@/lib/utils";
import {
  type BaseCurrency,
  PORTFOLIO_DISPLAY_CURRENCIES,
  PORTFOLIO_NORMALIZATION_TARGETS,
} from "./currencies";

// Currency -> ISO 3166-1-alpha-2 country code for flag-icons' `.fi-<code>`
// class. CNH (offshore RMB) intentionally maps to `cn` — it is not a
// separate jurisdiction, just an offshore-traded quote of the same
// currency, and flag-icons has no distinct code for it (product-owner
// decision during this issue's design conversation, not a default
// assumption). EUR -> `eu` (the European Union's own flag-icons entry),
// explicitly not a member state's flag — same conversation.
const FLAG_CODE: Record<(typeof PORTFOLIO_DISPLAY_CURRENCIES)[number], string> = {
  USD: "us",
  CNY: "cn",
  CNH: "cn",
  GBP: "gb",
  HKD: "hk",
  TWD: "tw",
  EUR: "eu",
};

function isNormalizationTarget(
  currency: (typeof PORTFOLIO_DISPLAY_CURRENCIES)[number],
): currency is (typeof PORTFOLIO_NORMALIZATION_TARGETS)[number] {
  return (PORTFOLIO_NORMALIZATION_TARGETS as readonly string[]).includes(currency);
}

function Flag({
  currency,
  disabled,
}: {
  currency: (typeof PORTFOLIO_DISPLAY_CURRENCIES)[number];
  disabled?: boolean;
}) {
  return (
    <span
      aria-hidden="true"
      className={cn("fi shrink-0 rounded-[2px]", `fi-${FLAG_CODE[currency]}`, disabled && "grayscale opacity-50")}
    />
  );
}

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
  // Review finding (blacktomb42, PR #355): `value` can be a BaseCurrency
  // outside PORTFOLIO_DISPLAY_CURRENCIES entirely — e.g. GET /portfolio/
  // summary seeding base_currency from the user's own report-currency
  // preference (issue #350 item 1, all 15 BASE_CURRENCIES), which can be
  // JPY or any other currency this switcher doesn't list. Substituting a
  // different currency's label/flag on the trigger in that case would show
  // "USD" while every figure on the page is actually normalized to JPY —
  // silently misleading. Always show the real `value` as text; only render
  // a flag when we actually have one for it.
  const hasFlag = (PORTFOLIO_DISPLAY_CURRENCIES as readonly string[]).includes(value);

  return (
    <MenuDropdown
      trigger={
        <>
          {hasFlag && <Flag currency={value as (typeof PORTFOLIO_DISPLAY_CURRENCIES)[number]} />}
          <span>{value}</span>
          {/* Visually hidden, but still part of the trigger button's
              accessible name — matches LocaleSwitcher's pattern. */}
          <span className="sr-only">{t("currencyLabel")}</span>
          <ChevronDown aria-hidden="true" className="size-4 opacity-80" />
        </>
      }
      disabled={disabled}
    >
      {PORTFOLIO_DISPLAY_CURRENCIES.map((currency) => {
        const selectable = isNormalizationTarget(currency) && !disabled;
        return (
          <MenuItemButton
            key={currency}
            disabled={!selectable}
            onClick={() => {
              if (selectable) onChange(currency);
            }}
          >
            <Flag currency={currency} disabled={!isNormalizationTarget(currency)} />
            {currency}
          </MenuItemButton>
        );
      })}
    </MenuDropdown>
  );
}
