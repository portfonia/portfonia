import { SPY_AGG_CLOSES } from "../_data/spy-agg-closes";
const mean = (xs: readonly number[]) => xs.reduce((a,b)=>a+b,0)/xs.length;
const sd = (xs: readonly number[]) => { const m=mean(xs);return Math.sqrt(xs.reduce((sum,x)=>sum+(x-m)**2,0)/(xs.length-1)); };
const dailyReturns = (values: readonly number[]) => values.slice(1).map((v,i)=>v/values[i]-1);
export function replayMetrics(dates: readonly string[], values: readonly number[]) {
  const daily=dailyReturns(values);
  let peak=values[0],peakDate=dates[0],drawdown=0,ddPeak=dates[0],ddTrough=dates[0];
  const months=new Map<string,number>();
  values.forEach((v,i)=>{if(v>peak){peak=v;peakDate=dates[i];}const dd=v/peak-1;if(dd<drawdown){drawdown=dd;ddPeak=peakDate;ddTrough=dates[i];}months.set(dates[i].slice(0,7),v);});
  let previous=values[0],worstMonth=Infinity,worstMonthLabel="";
  months.forEach((v,m)=>{const r=v/previous-1;if(r<worstMonth){worstMonth=r;worstMonthLabel=m;}previous=v;});
  const worstDay=Math.min(...daily), ratio=values.at(-1)!/values[0];
  const calendarDays=(Date.parse(dates.at(-1)!)-Date.parse(dates[0]))/86400000;
  return {cumulative:ratio-1,annualized:ratio**(365.25/calendarDays)-1,volatility:sd(daily)*Math.sqrt(252),drawdown,peakDate:ddPeak,troughDate:ddTrough,worstDay,worstDayDate:dates[daily.indexOf(worstDay)+1],worstMonth,worstMonthLabel};
}
export function tailRisk(values: readonly number[]) {
  const daily=dailyReturns(values),monthly=values.slice(21).map((v,i)=>v/values[i]-1);
  function cell(xs:number[],level:95|99){const sorted=[...xs].sort((a,b)=>a-b);const k=Math.floor((xs.length*(100-level)+99)/100);return {var:-sorted[k-1],cvar:-mean(sorted.slice(0,k)),k};}
  function normal(xs:number[],level:95|99){const z=level===95?1.6448536269514722:2.3263478740408408,sigma=sd(xs),mu=mean(xs);return {var:z*sigma-mu,cvar:sigma*Math.exp(-z*z/2)/Math.sqrt(2*Math.PI)/(1-level/100)-mu};}
  return {dailyCount:daily.length,monthCount:monthly.length,independentMonths:Math.floor(daily.length/21),fatCount:daily.filter(r=>r<mean(daily)-3*sd(daily)).length,normalExpected:daily.length*.0013498980316301,levels:([95,99] as const).map(level=>({level,daily:cell(daily,level),monthly:cell(monthly,level),normalDaily:normal(daily,level),normalMonthly:normal(monthly,level)}))};
}
export function calculateExample(){
  const dates=SPY_AGG_CLOSES.map(r=>r[0]);
  const spy=SPY_AGG_CLOSES.map(r=>r[1]/SPY_AGG_CLOSES[0][1]);
  const values=SPY_AGG_CLOSES.map((r,i)=>.6*spy[i]+.4*r[2]/SPY_AGG_CLOSES[0][2]);
  const counts=new Map<number,number>();dailyReturns(values).forEach(r=>{const index=Math.floor(r/.005);counts.set(index,(counts.get(index)??0)+1);});
  const minimum=Math.min(...counts.keys()),maximum=Math.max(...counts.keys());
  const monthRows=dates.map((date,i)=>({date,value:values[i]})).filter(r=>r.date >= "2026-09-09");
  return {start:dates[0],end:dates.at(-1)!,portfolio:{metrics:replayMetrics(dates,values),tail:tailRisk(values)},spy:{metrics:replayMetrics(dates,spy),tail:tailRisk(spy)},oneMonth:replayMetrics(monthRows.map(r=>r.date),monthRows.map(r=>r.value)),oneMonthStart:monthRows[0].date,endSpyWeight:.6*spy.at(-1)!/values.at(-1)!,points:dates.map((date,i)=>({time:Date.parse(date),portfolio:values[i]-1,spy:spy[i]-1})),histogram:Array.from({length:maximum-minimum+1},(_,i)=>({index:minimum+i,lower:(minimum+i)*.005,count:counts.get(minimum+i)??0})),reference:([95,99]as const).map(level=>{const z=level===95?1.6448536269514722:2.3263478740408408;return {level,daily:z*.2/Math.sqrt(252),monthly:z*.2*Math.sqrt(21/252)};}),constants:{spyStart:.6,aggStart:.4,betaFallback:1,betaSamples:60,baseAmount:100000,threeSd:3,referenceYears:[1996,1999,2000,2002],volumes:[9,2,26],issues:[3,3,7],oneDay:1,oneMonth:1}};
}
export type Example = ReturnType<typeof calculateExample>;
