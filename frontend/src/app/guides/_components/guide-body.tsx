"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, XAxis, YAxis } from "recharts";
import { LanguageLinks } from "@/app/_components/language-links";
import { useLocale, useLocalizedHref } from "@/app/_components/locale-provider";
import type { Example } from "../_lib/example";
import type { Messages } from "@/locales";

export function GuideBody({kind,example:e}:{kind:"replay"|"var";example:Example}){
  const t=useTranslations("guides"),replay=useTranslations("jade.replay"),tail=useTranslations("jade.tailRisk");
  const {locale}=useLocale(),href=useLocalizedHref();
  const percent=new Intl.NumberFormat(locale,{style:"percent",minimumFractionDigits:2,maximumFractionDigits:2});
  const integer=new Intl.NumberFormat(locale,{maximumFractionDigits:0});
  const amount=new Intl.NumberFormat(locale,{style:"currency",currency:"USD",maximumFractionDigits:0});
  const date=new Intl.DateTimeFormat(locale,{dateStyle:"medium",timeZone:"UTC"});
  const month=new Intl.DateTimeFormat(locale,{year:"numeric",month:"long",timeZone:"UTC"});
  const p=(v:number)=>percent.format(v),n=(v:number)=>integer.format(v),d=(v:string)=>date.format(new Date(`${v}T00:00:00Z`));
  const pm=e.portfolio.metrics,sm=e.spy.metrics,pt=e.portfolio.tail;
  const values={
    exampleLabel:t("common.exampleLabel"),spyStart:p(e.constants.spyStart),aggStart:p(e.constants.aggStart),
    start:d(e.start),end:d(e.end),cumulative:p(pm.cumulative),spyCumulative:p(sm.cumulative),annualized:p(pm.annualized),spyAnnualized:p(sm.annualized),volatility:p(pm.volatility),spyVolatility:p(sm.volatility),drawdown:p(pm.drawdown),spyDrawdown:p(sm.drawdown),peak:d(pm.peakDate),trough:d(pm.troughDate),worstDay:p(pm.worstDay),spyWorstDay:p(sm.worstDay),worstDate:d(pm.worstDayDate),worstMonth:p(pm.worstMonth),spyWorstMonth:p(sm.worstMonth),worstMonthLabel:month.format(new Date(`${pm.worstMonthLabel}-01T00:00:00Z`)),endSpyWeight:p(e.endSpyWeight),monthStart:d(e.oneMonthStart),monthCumulative:p(e.oneMonth.cumulative),monthAnnualized:p(e.oneMonth.annualized),betaFallback:n(e.constants.betaFallback),betaSamples:n(e.constants.betaSamples),dailyCount:n(pt.dailyCount),dailyK95:n(pt.levels[0].daily.k),dailyK99:n(pt.levels[1].daily.k),monthCount:n(pt.monthCount),independentMonths:n(pt.independentMonths),dayVar:p(pt.levels[0].daily.var),dayCvar:p(pt.levels[0].daily.cvar),dayVar99:p(pt.levels[1].daily.var),normal95:p(pt.levels[0].normalDaily.var),normal99:p(pt.levels[1].normalDaily.var),fatCount:n(pt.fatCount),threeSd:n(e.constants.threeSd),normalExpected:new Intl.NumberFormat(locale,{minimumFractionDigits:1,maximumFractionDigits:1}).format(pt.normalExpected),worstLoss:p(-pm.worstDay),referenceDay:p(e.reference[0].daily),oneDay:n(e.constants.oneDay),oneMonth:n(e.constants.oneMonth),year1996:n(e.constants.referenceYears[0]).replace(/[,.\s]/g,""),year1999:n(e.constants.referenceYears[1]).replace(/[,.\s]/g,""),year2000:n(e.constants.referenceYears[2]).replace(/[,.\s]/g,""),year2002:n(e.constants.referenceYears[3]).replace(/[,.\s]/g,""),volume9:n(e.constants.volumes[0]),volume2:n(e.constants.volumes[1]),volume26:n(e.constants.volumes[2]),issue3:n(e.constants.issues[0]),issue7:n(e.constants.issues[2]),
  };
  const portfolio=t("common.portfolio",values),all={...values,portfolio};
  const tableClass="w-full table-fixed border-collapse text-xs sm:text-sm";
  const cellClass="break-words border-b border-foreground/10 px-1 py-3 text-left align-top";
  const caption=t("common.window",all);
  const label=(series:string,confidence:number,metric:string)=>t("common.tailRow",{series,confidence:p(confidence/100),metric});
  const metricRows=[['cumulative_return','cumulative'],['annualized_return','annualized'],['annualized_vol','volatility'],['max_drawdown','drawdown'],['worst_day','worstDay'],['worst_month','worstMonth']] as const;
  function replayTable(){return <table className={tableClass}>
    <caption className="mb-3 text-left leading-relaxed">{caption}</caption>
    <thead><tr><th className={cellClass}>{t("common.metric")}</th><th className={cellClass}>{portfolio}</th><th className={cellClass}>SPY</th></tr></thead>
    <tbody>{metricRows.map(([key,field])=><tr key={key}><th scope="row" className={cellClass}>{replay(`metrics.${key}`)}</th>{[pm,sm].map((m,i)=><td className={cellClass} key={i}>{p(m[field])}{field==='drawdown'&&<p>{t("common.dateRange",{start:d(m.peakDate),end:d(m.troughDate)})}</p>}{field==='worstDay'&&<p>{d(m.worstDayDate)}</p>}{field==='worstMonth'&&<p>{month.format(new Date(`${m.worstMonthLabel}-01T00:00:00Z`))}</p>}</td>)}</tr>)}</tbody>
  </table>;}
  function riskTable(){return <table className={tableClass}>
    <caption className="mb-3 text-left leading-relaxed">{caption}</caption>
    <thead><tr><th className={cellClass}>{t("common.metric")}</th><th className={cellClass}>{t("common.day",values)}</th><th className={cellClass}>{t("common.month",values)}</th></tr></thead>
    <tbody>{[e.portfolio,e.spy].flatMap((series,si)=>series.tail.levels.flatMap(level=>(['var','cvar']as const).map(metric=><tr key={`${si}-${level.level}-${metric}`}><th scope="row" className={cellClass}>{label(si===0?portfolio:'SPY',level.level,tail(`rows.${metric}`))}</th>{(['daily','monthly']as const).map(horizon=><td key={horizon} className={cellClass} data-cell={`${si===0?'portfolio':'spy'}-${level.level}-${horizon}-${metric}`}>{p(level[horizon][metric])}{si===0&&<p>{amount.format(level[horizon][metric]*e.constants.baseAmount)}</p>}</td>)}</tr>)))}
    <tr><th scope="row" className={cellClass}>{tail("rows.reference")}</th>{(['daily','monthly']as const).map(horizon=><td key={horizon} className={cellClass}>{e.reference.map(r=><p key={r.level}>{t("common.referenceValue",{confidence:p(r.level/100),value:p(r[horizon])})}</p>)}</td>)}</tr></tbody>
  </table>;}
  function normalTable(){return <table className={tableClass}>
    <caption className="mb-3 text-left leading-relaxed">{t("common.normalCaption",all)}</caption>
    <thead><tr><th className={cellClass}>{t("common.metric")}</th><th className={cellClass}>{t("common.historical")}</th><th className={cellClass}>{t("common.normal")}</th></tr></thead>
    <tbody>{pt.levels.flatMap(level=>(['var','cvar']as const).map(metric=><tr key={`${level.level}-${metric}`}><th scope="row" className={cellClass}>{label(portfolio,level.level,tail(`rows.${metric}`))}</th><td className={cellClass}>{p(level.daily[metric])}</td><td className={cellClass}>{p(level.normalDaily[metric])}</td></tr>))}</tbody>
  </table>;}
  function figure(){return <figure className="my-4 min-w-0">
    <div data-guide-chart aria-label={t(`${kind}.sections.s2.heading`)} className="h-60 w-full min-w-0">
      {kind==='replay'?<ResponsiveContainer width="100%" height="100%"><LineChart data={e.points} margin={{top:8,right:8,bottom:0,left:0}}>
        <CartesianGrid stroke="var(--border)" vertical={false}/><XAxis type="number" dataKey="time" domain={['dataMin','dataMax']} tickFormatter={v=>date.format(new Date(Number(v)))} minTickGap={36} tick={{fontSize:10,fill:'var(--muted-foreground)'}}/><YAxis width={48} tickFormatter={p} tick={{fontSize:10,fill:'var(--muted-foreground)'}}/>
        <Line name={portfolio} dataKey="portfolio" stroke="var(--foreground)" dot={false} isAnimationActive={false}/><Line name="SPY" dataKey="spy" stroke="var(--muted-foreground)" dot={false} isAnimationActive={false}/>
        <ReferenceLine x={Date.parse(pm.peakDate)} stroke="var(--muted-foreground)" strokeDasharray="3 3"/><ReferenceLine x={Date.parse(pm.troughDate)} stroke="var(--muted-foreground)" strokeDasharray="3 3"/><Legend wrapperStyle={{fontSize:10}}/>
      </LineChart></ResponsiveContainer>:<ResponsiveContainer width="100%" height="100%"><BarChart data={e.histogram}>
        <XAxis type="number" dataKey="lower" domain={['dataMin','dataMax']} tickFormatter={p} tick={{fontSize:10,fill:'var(--muted-foreground)'}}/><YAxis width={30} tickFormatter={n} tick={{fontSize:10,fill:'var(--muted-foreground)'}}/>
        <Bar dataKey="count" name={t("common.frequency")} fill="var(--muted-foreground)" isAnimationActive={false}/>
        <ReferenceLine x={-pt.levels[0].daily.var} stroke="#b58b3c"/><ReferenceLine x={-pt.levels[0].daily.cvar} stroke="#8070b0"/><ReferenceLine x={-e.reference[0].daily} stroke="var(--foreground)" strokeDasharray="3 3"/>
      </BarChart></ResponsiveContainer>}
    </div>
    {kind==='var'&&<div className="mt-2 flex flex-wrap gap-3 text-xs"><span><span aria-hidden="true" className="mr-1 inline-block w-3 border-t-2 border-[#b58b3c]"/>{tail("legend.var")}</span><span><span aria-hidden="true" className="mr-1 inline-block w-3 border-t-2 border-[#8070b0]"/>{tail("legend.cvar")}</span><span><span aria-hidden="true" className="mr-1 inline-block w-3 border-t-2 border-dashed border-foreground"/>{tail("legend.reference")}</span></div>}
    <figcaption className="mt-3 text-xs leading-relaxed text-foreground/70">{t(kind==='replay'?"common.replayCaption":"common.varCaption",all)}{kind==='var'&&<p className="mt-2">{t("common.histogramCounts",{bins:e.histogram.map(b=>t("common.histogramBin",{lower:p(b.lower),upper:p(b.lower+.005),count:n(b.count)})).join(({en:'; ',es:'; ',"zh-Hans":'；',"zh-Hant":'；'}as const)[locale])})}</p>}</figcaption>
  </figure>;}
  const sections:Messages['guides']['replay']['sections']|Messages['guides']['var']['sections']=kind==='replay'?t.raw('replay.sections'):t.raw('var.sections');
  return <main className="mx-auto w-full max-w-2xl px-6 py-12">
    <h1 className="font-serif text-3xl">{t(`${kind}.title`)}</h1>
    <p className="mt-6 text-sm leading-relaxed text-foreground/80">{t(`${kind}.intro`,all)}</p>
    <div className="mt-10 flex min-w-0 flex-col gap-8">{Object.entries(sections).map(([key,section])=><section key={key} className="min-w-0">
      <h2 className="font-serif text-lg">{t(`${kind}.sections.${key}.heading`)}</h2>
      <div className="mt-2 flex min-w-0 flex-col gap-3">{Object.keys(section).filter(k=>k!=='heading').map(k=><p className="break-words text-sm leading-relaxed text-foreground/70" key={k}>{t(`${kind}.sections.${key}.${k}`,all)}</p>)}</div>
      {key==='s2'&&figure()}{kind==='replay'&&key==='s2'&&replayTable()}{kind==='var'&&key==='s3'&&riskTable()}{kind==='var'&&key==='s8'&&normalTable()}
    </section>)}</div>
    <nav className="mt-12 flex flex-wrap gap-4 border-t border-foreground/10 pt-6 text-sm text-foreground/60">
      <Link href={href('/pricing')} className="underline">{t('common.pricingLink')}</Link><Link href={href(kind==='replay'?'/guides/jade-var':'/guides/jade-replay')} className="underline">{t(kind==='replay'?'common.varLink':'common.replayLink')}</Link>
    </nav><LanguageLinks/>
  </main>;
}
