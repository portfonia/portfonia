"use client";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { LanguageLinks } from "@/app/_components/language-links";
import { useLocale, useLocalizedHref } from "@/app/_components/locale-provider";
import { formatShortDate, formatTickPct } from "@/app/portfolio/performance/_components/performance-format";
import type { WhatIfExample } from "../_lib/example-what-if";
export function WhatIfGuide({ example }: { example: WhatIfExample }) {
 const t=useTranslations("guides.whatIf"), common=useTranslations("guides.common"), replay=useTranslations("jade.replay"), rules=useTranslations("jade.whatIf.rules");
 const {locale}=useLocale(); const href=useLocalizedHref();
 const cell="border-b border-foreground/10 p-3 text-left align-top font-normal";
 return <main className="mx-auto w-full max-w-2xl px-6 py-12">
  <h1 className="font-serif text-3xl">{t("title")}</h1><p className="mt-6 text-sm leading-relaxed text-foreground/80">{t("intro")}</p>
  <div className="mt-10 flex min-w-0 flex-col gap-8">{(["compare","rules","anchoring","example","limits","watermark"] as const).map(key=><section key={key} className="min-w-0"><h2 className="font-serif text-lg">{t(`sections.${key}.heading`)}</h2><p className="mt-2 break-words text-sm leading-relaxed text-foreground/70">{t(`sections.${key}.text`)}</p>{key==="example" && <>
   <p className="mt-3 text-sm">{t("window",{start:example.start,end:example.end})} {common("exampleLabel")}</p>
   <figure className="my-4 min-w-0"><div data-guide-chart className="h-60 w-full min-w-0"><ResponsiveContainer width="100%" height="100%"><LineChart data={example.points} margin={{top:8,right:8,left:0,bottom:0}}><CartesianGrid stroke="var(--border)" vertical={false} strokeDasharray="3 3" /><XAxis dataKey="time" type="number" domain={["dataMin","dataMax"]} tickFormatter={value=>formatShortDate(new Date(Number(value)).toISOString().slice(0,10),locale)} tick={{fill:"var(--muted-foreground)",fontSize:12}} /><YAxis width={52} tickFormatter={formatTickPct} tick={{fill:"var(--muted-foreground)",fontSize:12}} /><Tooltip isAnimationActive={false} labelFormatter={value=>formatShortDate(new Date(Number(value)).toISOString().slice(0,10),locale)} formatter={value=>formatTickPct(Number(value))} wrapperStyle={{maxWidth:"100%"}} /><Legend /><Line dataKey="none" name={rules("none")} stroke="#64748b" strokeWidth={2} dot={false} isAnimationActive={false}/><Line dataKey="quarterly" name={rules("quarterly")} stroke="#8b7bb8" strokeWidth={2} dot={false} isAnimationActive={false}/></LineChart></ResponsiveContainer></div><figcaption className="mt-3 text-xs text-foreground/70">{t("chartCaption")}</figcaption></figure>
   <div className="max-w-full overflow-x-auto"><table className="w-full text-sm"><caption className="mb-3 text-left">{common("exampleLabel")}</caption><thead><tr><th className={cell}>{common("metric")}</th><th className={cell}>{rules("none")}</th><th className={cell}>{rules("quarterly")}</th></tr></thead><tbody>{([["cumulative_return","cumulative"],["annualized_return","annualized"],["annualized_vol","volatility"],["max_drawdown","drawdown"]] as const).map(([label,metric])=><tr key={metric}><th scope="row" className={cell}>{replay(`metrics.${label}`)}</th>{(["none","quarterly"] as const).map(rule=><td key={rule} className={cell}>{formatTickPct(example[rule][metric])}</td>)}</tr>)}</tbody></table></div>
  </>}</section>)}</div>
  <nav className="mt-12 flex flex-wrap gap-4 border-t border-foreground/10 pt-6 text-sm text-foreground/60"><Link className="underline" href={href("/pricing")}>{common("pricingLink")}</Link><Link className="underline" href={href("/guides")}>{common("guidesLink")}</Link></nav><LanguageLinks />
 </main>;
}
