import { act, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { LocaleProvider } from "@/app/_components/locale-provider";
import { catalogs } from "@/locales";
const { getJadeStyle } = vi.hoisted(() => ({ getJadeStyle: vi.fn() }));
vi.mock("@/lib/api", async () => ({ ...await vi.importActual<typeof import("@/lib/api")>("@/lib/api"), getJadeStyle }));
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));
vi.mock("recharts", () => ({ ResponsiveContainer: ({children}: {children: React.ReactNode}) => <div>{children}</div>, LineChart: ({children}: {children: React.ReactNode}) => <div data-testid="line-chart">{children}</div>, Line: ({dataKey}: {dataKey: string}) => <span data-testid={`line-${dataKey}`}/>, XAxis: () => null, YAxis: () => null, Tooltip: () => null, CartesianGrid: () => null }));
const symbols=["IWF","IWD","IWM","EFA","EEM","2800.HK","510300.SS","AGG","TLT","GLD","DBC","VNQ","BIL"];
const weights=symbols.map((symbol,i)=>({symbol,weight:i<2?"0.400000":i===2?"0.200000":"0.000000"}));
const fit={weights,r_squared:"0.590000",residual_vol:"0.012300",low_fit:true};
const data={status:"ok",base_currency:"USD",benchmark:"sp500",benchmark_symbol:"SPY",benchmark_name:"SPDR",benchmark_status:"ok",window_start:"2026-07-08",window_end:"2026-10-08",first_valid_date:"2026-07-08",sample_count:60,horizon_days:3,min_samples:42,portfolio:fit,benchmark_fit:{...fit,weights:weights.map((w,i)=>({...w,weight:i===3?"1.000000":"0.000000"}))},points:[{date:"2026-07-08",portfolio:"0.000000",style_mix:"0.000000"}],coverage:{own_share:"1.000000",proxy_share:"0.000000",head_proxy_share:"0.000000",cash_share:"0.000000",cash_assumed_share:"0.000000",approx_share_at_start:"0.000000",data_quality:false,pending_share:null},proxy_inflates_fit:false};
type Settings={currency:"USD"|"EUR"|"CNY";benchmark:"sp500"|"csi300"}|null;
async function view(settings: Settings=null) {
  const { StyleSection }=await import("./style-section");
  const wrap=(s:Settings)=><LocaleProvider routeLocale={null}><div className="dark" style={{width:375}}><StyleSection settings={s}/></div></LocaleProvider>;
  return {...render(wrap(settings)),wrap};
}
beforeEach(()=>{getJadeStyle.mockReset();});
it("B13 initial request has no settings and overlay covers table chart model",async()=>{
  let resolve:(value:typeof data)=>void=()=>{};getJadeStyle.mockImplementation(()=>new Promise(r=>{resolve=r;}));
  const {container}=await view();expect(getJadeStyle).toHaveBeenCalledExactlyOnceWith();
  const inert=container.querySelector("[inert]");
  for(const target of [screen.getByTestId("style-table"),screen.getByTestId("style-chart"),container.querySelector("details")])expect(inert).toContainElement(target);
  expect(container.querySelector("details")).not.toHaveAttribute("open");
  await act(async()=>resolve(data));expect(screen.queryByTestId("calculating-overlay")).not.toBeInTheDocument();
});
it("B13 settings queue only latest and equivalent settings do not fetch",async()=>{
  let resolve:(value:typeof data)=>void=()=>{};
  getJadeStyle.mockResolvedValueOnce(data).mockImplementationOnce(()=>new Promise(r=>{resolve=r;})).mockResolvedValueOnce({...data,base_currency:"CNY",benchmark:"csi300"});
  const {rerender,wrap}=await view();await screen.findByTestId("line-chart");
  rerender(wrap({currency:"USD",benchmark:"sp500"}));expect(getJadeStyle).toHaveBeenCalledOnce();
  rerender(wrap({currency:"EUR",benchmark:"sp500"}));expect(getJadeStyle).toHaveBeenLastCalledWith("EUR","sp500");
  rerender(wrap({currency:"EUR",benchmark:"csi300"}));rerender(wrap({currency:"CNY",benchmark:"csi300"}));expect(getJadeStyle).toHaveBeenCalledTimes(2);
  await act(async()=>resolve({...data,base_currency:"EUR"}));await waitFor(()=>expect(getJadeStyle).toHaveBeenCalledTimes(3));expect(getJadeStyle).toHaveBeenLastCalledWith("CNY","csi300");
});
it("B13 failed request keeps previous data and does not retry",async()=>{
  getJadeStyle.mockResolvedValueOnce(data).mockRejectedValueOnce(new Error("offline"));
  const {rerender,wrap}=await view();await screen.findByTestId("line-chart");rerender(wrap({currency:"EUR",benchmark:"sp500"}));
  expect(await screen.findByRole("alert")).toHaveTextContent("Style exposure could not be loaded.");expect(screen.getByTestId("style-table")).toHaveTextContent("40.00%");await act(async()=>{});expect(getJadeStyle).toHaveBeenCalledTimes(2);
});
it("B14 table union sort ties bars statistics and both chart lines",async()=>{
  getJadeStyle.mockResolvedValue(data);const {container}=await view();await screen.findByTestId("line-chart");
  const table=screen.getByTestId("style-table");expect([...table.querySelectorAll("[data-style-row]")].map(e=>e.getAttribute("data-style-row"))).toEqual(["IWF","IWD","IWM","EFA"]);
  expect(table.querySelectorAll("[data-style-bar]")).toHaveLength(4);expect(table.querySelector("[data-style-bar]")).toHaveStyle({width:"40%"});
  expect(table).toHaveTextContent("Share of movement explained (R²)");expect(table).toHaveTextContent("Unexplained volatility (annualized)");expect(table).toHaveTextContent("1.23%");
  expect(screen.getByTestId("line-portfolio")).toBeInTheDocument();expect(screen.getByTestId("line-style_mix")).toBeInTheDocument();expect(container.querySelector("details")).not.toHaveAttribute("open");
});
it.each(["pending","unavailable"])("B14 benchmark %s has dashes",async status=>{
  getJadeStyle.mockResolvedValue({...data,benchmark_status:status,benchmark_fit:null});await view();await screen.findByTestId("line-chart");
  const table=screen.getByTestId("style-table");expect(table.querySelector('[data-style-row="EFA"]')).not.toBeInTheDocument();
  for(const cell of table.querySelectorAll("[data-benchmark-cell]"))expect(cell).toHaveTextContent("—");
});
it.each([true,false])("B15 low fit %s",async low=>{
  getJadeStyle.mockResolvedValue({...data,portfolio:{...fit,low_fit:low}});await view();await screen.findByTestId("line-chart");expect(screen.queryByTestId("style-low-fit")!==null).toBe(low);
  if(low)expect(screen.getByTestId("style-low-fit")).toHaveTextContent("R² is 0.59, below 0.60");
});
it.each(["ok","insufficient","pending","no_holdings"])("B15 status %s gates notes",async status=>{
  getJadeStyle.mockResolvedValue({...data,status,portfolio:status==="ok"?fit:null,sample_count:41,proxy_inflates_fit:true,coverage:{...data.coverage,data_quality:true}});await view();await waitFor(()=>expect(screen.queryByTestId("calculating-overlay")).not.toBeInTheDocument());
  expect(screen.queryByText(/Based on about three months/)!==null).toBe(status==="ok");
  expect(screen.queryByText(/At least 66%/)!==null).toBe(status==="ok"||status==="insufficient");
  expect(screen.queryByText(/raise the fit mechanically/)!==null).toBe(status==="ok"||status==="insufficient");
  if(status==="insufficient")expect(screen.getByText("Fewer than 42 three-day returns are available (41), so no style mix is shown.")).toBeInTheDocument();
});
it.each(["quality","proxy","neither"])("B15 separate flags %s",async flag=>{
  getJadeStyle.mockResolvedValue({...data,coverage:{...data.coverage,data_quality:flag==="quality"},proxy_inflates_fit:flag==="proxy"});await view();await screen.findByTestId("line-chart");
  expect(screen.queryByText(/At least 66%/)!==null).toBe(flag==="quality");expect(screen.queryByText(/raise the fit mechanically/)!==null).toBe(flag==="proxy");
});
it("375px dark table bars notice and chart stay within flexible cells",async()=>{
  getJadeStyle.mockResolvedValue(data);await view();await screen.findByTestId("line-chart");const table=screen.getByTestId("style-table");expect(table).toHaveClass("grid-cols-3","min-w-0");
  for(const cell of table.querySelectorAll(".contents > *"))expect(cell).toHaveClass("min-w-0","break-words");
  for(const bar of table.querySelectorAll("[data-style-bar]"))expect(bar.parentElement).toHaveClass("w-full","min-w-0");
  expect(screen.getByTestId("style-low-fit")).toHaveClass("break-words","dark:bg-amber-950/30","dark:text-amber-200");expect(screen.getByTestId("style-chart")).toHaveClass("h-60","w-full","min-w-0");
});
it.each(["en","zh-Hans","zh-Hant"] as const)("B16 %s D9 labels and parity",locale=>{
  const c=catalogs[locale].jade as unknown as Record<string,{labels:Record<string,string>}>;
  expect(Object.keys(c.style.labels)).toEqual(symbols.map(s=>s.replaceAll(".","_")));
  const keys=(value:object,prefix=""):string[]=>Object.entries(value).flatMap(([key,v])=>v!==null&&typeof v==="object"?keys(v,`${prefix}${key}.`):[`${prefix}${key}`]).sort();
  expect(keys(c.style)).toEqual(keys((catalogs.en.jade as unknown as typeof c).style));
  const expected=locale==="en"?"US large-cap growth":locale==="zh-Hans"?"\u7f8e\u56fd\u5927\u76d8\u6210\u957f":"\u7f8e\u570b\u5927\u578b\u6210\u9577\u80a1";expect(c.style.labels.IWF).toBe(expected);
});
