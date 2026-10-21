"use client";

import { useState } from "react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useLocalizedHref } from "@/app/_components/locale-provider";
import { CurrencySwitcher } from "@/app/portfolio/_components/currency-switcher";
import type { BaseCurrency } from "@/app/portfolio/_components/currencies";
import { Card, CardContent } from "@/components/ui/card";
import type { Me, ReplayRange } from "@/lib/api";
import { ReplayRangeTabs } from "../_components/replay-range-tabs";
import { TrackingSection } from "../_components/tracking-section";

export function TrackingBody({ me, hadLoadError }: { me: Me | null; hadLoadError: boolean }) {
  const profile = useTranslations("profile");
  const t = useTranslations("jade.whatIf");
  const href = useLocalizedHref();
  const [currency, setCurrency] = useState<BaseCurrency>((me?.report_currency ?? "USD") as BaseCurrency);
  const [range, setRange] = useState<ReplayRange>("1Y");
  if (hadLoadError || !me) return <p className="text-sm text-destructive" role="alert">{profile("errorLoadFailed")}</p>;
  if (me.subscription.status !== "active" || me.subscription.type !== "jade") return <Card><CardContent className="space-y-3 pt-6"><p>{t("access")}</p><Link className="underline" href={href("/jade")}>{t("jadeLink")}</Link></CardContent></Card>;
  return <div className="flex min-w-0 flex-col gap-6">
    <div className="flex flex-wrap items-center gap-3"><CurrencySwitcher value={currency} onChange={setCurrency} /><ReplayRangeTabs value={range} onChange={setRange} /></div>
    <TrackingSection settings={{ currency, range }} />
  </div>;
}
