import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { LocaleProvider } from "@/app/_components/locale-provider";
import en from "@/locales/en.json";
import hans from "@/locales/zh-Hans.json";
import hant from "@/locales/zh-Hant.json";
const { getJadeStress } = vi.hoisted(() => ({ getJadeStress: vi.fn() }));
vi.mock("@/lib/api", async () => ({ ...await vi.importActual<typeof import("@/lib/api")>("@/lib/api"), getJadeStress }));
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));
vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  LineChart: ({ children }: { children: React.ReactNode }) => <div data-testid="line-chart">{children}</div>,
  Line: ({ dataKey }: { dataKey: string }) => <span data-testid={`line-${dataKey}`} />,
  CartesianGrid: () => null, Tooltip: () => null, XAxis: () => null, YAxis: () => null,
  ReferenceLine: ({ x, strokeDasharray }: { x: number; strokeDasharray: string }) => <span data-testid="reference" data-x={x} data-dash={strokeDasharray} />,
}));
const coverage={ own_share:"1.000000",head_proxy_share:"0.000000",proxy_share:"0.000000",cash_share:"0.000000",cash_assumed_share:"0.000000",approx_share_at_start:"0.000000",data_quality:false,pending_share:null };
const scenario={ id:"covid_2020",peak_date:"2020-02-19",trough_date:"2020-03-23",window_start:"2019-08-19",window_end:"2020-09-23",status:"ok",first_valid_date:"2019-08-19",sample_count:100,portfolio_value:"100000.00",shock_return:"-0.178571",shock_amount:"-17857.10",max_drawdown:"-0.250000",max_drawdown_peak:"2020-02-20",max_drawdown_trough:"2020-03-24",benchmark_status:"ok",benchmark_shock_return:"-0.339000",benchmark_max_drawdown:"-0.340000",contributions:[{asset_class:"STOCK",contribution:"-0.186090"},{asset_class:"BOND_FUND",contribution:"0.007519"}],points:[{date:"2020-02-19",portfolio:"0.000000",benchmark:"0.000000"}],coverage,proxy_understates:false,holdings:[] };
const data={base_currency:"USD",benchmark:"sp500",benchmark_symbol:"SPY",benchmark_name:"SPDR S&P 500 ETF Trust",scenarios:[scenario,{...scenario,id:"rates_2022",peak_date:"2022-01-03",trough_date:"2022-10-12",shock_return:"-0.100000",shock_amount:"-10000.00",contributions:[{asset_class:"BOND_FUND",contribution:"-0.100000"}]},{...scenario,id:"tariffs_2025"}]};
type Settings={currency:"USD"|"EUR"|"CNY";benchmark:"sp500"|"csi300"}|null;
async function view(settings: Settings=null) {
  const {StressSection}=await import("./stress-section");
  expect(StressSection).toBeDefined();
  const wrap=(s:Settings)=><LocaleProvider routeLocale={null}><div className="dark" style={{width:375}}><StressSection settings={s}/></div></LocaleProvider>;
  return {...render(wrap(settings)),wrap};
}
beforeEach(()=>{getJadeStress.mockReset();});
it("A16 initial layout under overlay settles",async()=>{
  let resolve:(value:typeof data)=>void=()=>{};getJadeStress.mockImplementation(()=>new Promise(r=>{resolve=r;}));
  const {container}=await view();expect(getJadeStress).toHaveBeenCalledExactlyOnceWith();
  for(const button of screen.getByRole("radiogroup").querySelectorAll("button"))expect(button).toBeDisabled();
  const region=container.querySelector("[inert]");expect(region).toContainElement(screen.getByTestId("stress-table"));expect(region).toContainElement(screen.getByRole("radiogroup"));expect(region).toContainElement(container.querySelector("details"));expect(region).toContainElement(screen.getByTestId("stress-chart"));
  expect(screen.getByTestId("stress-table").textContent?.match(/—/g)).toHaveLength(4);expect(screen.getByTestId("stress-chart")).toBeEmptyDOMElement();expect(container.querySelector("details")).not.toHaveAttribute("open");
  await act(async()=>resolve(data));expect(screen.queryByTestId("calculating-overlay")).not.toBeInTheDocument();
});
it("A17 local scenario switch swaps figures dates and contributions",async()=>{
  getJadeStress.mockResolvedValue(data);await view();await screen.findByTestId("line-chart");expect(screen.getByRole("button",{name:"2020 COVID shock"})).toHaveAttribute("aria-pressed","true");
  fireEvent.click(screen.getByRole("button",{name:"2022 rate hikes"}));expect(getJadeStress).toHaveBeenCalledOnce();expect(screen.getByTestId("stress-table")).toHaveTextContent("-10.00%");expect(screen.getByText(/S&P 500 closing peak 2022-01-03/)).toBeInTheDocument();expect(screen.getByTestId("stress-contributions").querySelectorAll("[data-contribution]")).toHaveLength(1);
});
it("A18 percentage amount dates and null table cells",async()=>{
  getJadeStress.mockResolvedValue({...data,scenarios:[{...scenario,benchmark_max_drawdown:null}]});await view();await screen.findByTestId("line-chart");const table=screen.getByTestId("stress-table");expect(table).toHaveTextContent("-17.86%");expect(table).toHaveTextContent("-17,857.10 USD");expect(table).toHaveTextContent("2020-02-20 to 2020-03-24");expect(table).toHaveTextContent("-33.90%");expect(table.textContent?.match(/USD/g)).toHaveLength(1);expect(within(table).getByText("—")).toBeInTheDocument();
});
it("A19 benchmark line and legend follow availability; other status has text",async()=>{
  getJadeStress.mockResolvedValue(data);const {unmount}=await view();await screen.findByTestId("line-chart");expect(screen.getByTestId("line-benchmark")).toBeInTheDocument();expect(screen.getByText("S&P 500 peak (2020-02-19)")).toBeInTheDocument();expect(screen.getByText("S&P 500 trough (2020-03-23)")).toBeInTheDocument();expect(screen.getAllByTestId("reference").map(e=>e.dataset.dash)).toEqual(["6 3","2 3"]);unmount();
  getJadeStress.mockResolvedValue({...data,scenarios:[{...scenario,benchmark_status:"unavailable"}]});const next=await view();await screen.findByTestId("line-chart");expect(screen.queryByTestId("line-benchmark")).not.toBeInTheDocument();expect(screen.getByTestId("stress-legend")).not.toHaveTextContent("(SPY)");next.unmount();
  getJadeStress.mockResolvedValue({...data,scenarios:[{...scenario,status:"pending"}]});await view();expect(await screen.findByText(/Price history for your holdings is being prepared/)).toBeInTheDocument();
});
it("A20 ordered class labels and contribution bar directions",async()=>{
  getJadeStress.mockResolvedValue(data);await view();await screen.findByTestId("line-chart");const rows=screen.getByTestId("stress-contributions").querySelectorAll("[data-contribution]");expect(rows[0]).toHaveTextContent(en.portfolio.assetClasses.STOCK);expect(rows[1]).toHaveTextContent(en.portfolio.assetClasses.BOND_FUND);
  expect(rows[0].querySelector("[data-bar]")).toHaveStyle({right:"50%",width:"50%"});expect(rows[1].querySelector("[data-bar]")).toHaveStyle({left:"50%"});expect(parseFloat((rows[1].querySelector("[data-bar]") as HTMLElement).style.width)).toBeCloseTo(.007519/.186090*50);
});
it("A21 settings queue latest once, equivalent span events do nothing, failure retains data",async()=>{
  let resolve:(value:typeof data)=>void=()=>{};getJadeStress.mockResolvedValueOnce(data).mockImplementationOnce(()=>new Promise(r=>{resolve=r;})).mockResolvedValueOnce({...data,base_currency:"CNY",benchmark:"csi300"}).mockRejectedValueOnce(new Error("offline"));
  const {rerender,wrap}=await view();await screen.findByTestId("line-chart");rerender(wrap({currency:"USD",benchmark:"sp500"}));expect(getJadeStress).toHaveBeenCalledOnce();rerender(wrap({currency:"EUR",benchmark:"csi300"}));expect(getJadeStress).toHaveBeenLastCalledWith("EUR","csi300");rerender(wrap({currency:"CNY",benchmark:"csi300"}));rerender(wrap({currency:"CNY",benchmark:"csi300"}));expect(getJadeStress).toHaveBeenCalledTimes(2);
  await act(async()=>resolve({...data,base_currency:"EUR",benchmark:"csi300"}));await waitFor(()=>expect(getJadeStress).toHaveBeenCalledTimes(3));expect(getJadeStress).toHaveBeenLastCalledWith("CNY","csi300");rerender(wrap({currency:"EUR",benchmark:"sp500"}));expect(await screen.findByRole("alert")).toHaveTextContent("Stress scenarios could not be loaded");expect(screen.getByTestId("stress-table")).toHaveTextContent("CNY");await act(async()=>{});expect(getJadeStress).toHaveBeenCalledTimes(4);
});
it("A22 notices and later listing, desktop details closed",async()=>{
  vi.stubGlobal("innerWidth",1024);getJadeStress.mockResolvedValue({...data,scenarios:[{...scenario,coverage:{...coverage,data_quality:true},proxy_understates:true,holdings:[{holding_id:"h",name:"Late",asset_class:"STOCK",weight:"1.000000",method:"head_proxy",excluded_reason:null,proxy_symbol:"SPY",proxy_name:"SPDR",beta:"1.0000",beta_samples:0,own_first_date:null}]}]});const {container,unmount}=await view();await screen.findByTestId("line-chart");expect(container.querySelector("details")).not.toHaveAttribute("open");expect(screen.getByText(/At least 66%/)).toBeInTheDocument();expect(screen.getByText(/may understate their losses/)).toBeInTheDocument();expect(screen.getByText(/SPY scaled by beta 1.0000 \(0 samples\) for the whole window/)).toBeInTheDocument();unmount();getJadeStress.mockResolvedValue(data);await view();await screen.findByTestId("line-chart");expect(screen.queryByText(/At least 66%/)).not.toBeInTheDocument();expect(screen.queryByText(/may understate their losses/)).not.toBeInTheDocument();vi.unstubAllGlobals();
});
it("A24 locale parity and authored titles",()=>{
  function keys(value:object,prefix=""):string[]{return Object.entries(value).flatMap(([key,v])=>typeof v==="object"&&v!==null?keys(v,`${prefix}${key}.`):[`${prefix}${key}`]).sort();}
  const catalogs=[en,hans,hant].map(c=>(c.jade as unknown as {stress?:object}).stress);expect(catalogs[0]).toBeDefined();expect(keys(catalogs[0]??{})).toEqual(keys(catalogs[1]??{}));expect(keys(catalogs[0]??{})).toEqual(keys(catalogs[2]??{}));expect((catalogs[1] as {title:string}).title).toBe("历史情景压力测试");expect((catalogs[2] as {title:string}).title).toBe("歷史情境壓力測試");
});
it("375px selector and legend wrap, grid cells wrap and chart/tracks fill card",async()=>{
  getJadeStress.mockResolvedValue(data);await view();await screen.findByTestId("line-chart");expect(screen.getByRole("radiogroup")).toHaveClass("flex-wrap");expect(screen.getByTestId("stress-legend")).toHaveClass("flex-wrap");expect(screen.getByTestId("stress-table")).toHaveClass("grid-cols-3","text-xs","sm:text-sm");for(const c of screen.getByTestId("stress-table").querySelectorAll(".contents > *"))expect(c).toHaveClass("min-w-0","break-words");expect(screen.getByTestId("stress-chart")).toHaveClass("h-60","w-full","min-w-0");for(const track of screen.getByTestId("stress-contributions").querySelectorAll("[data-track]"))expect(track).toHaveClass("w-full","min-w-0");
});

it.each([
  ["en", "Watch only (quantity 0); not included"],
  ["zh-Hans", "\u4ec5\u5173\u6ce8\uff08\u6570\u91cf\u4e3a 0\uff09\uff0c\u4e0d\u8ba1\u5165"],
  ["zh-Hant", "\u50c5\u95dc\u6ce8\uff08\u6578\u91cf\u70ba 0\uff09\uff0c\u4e0d\u8a08\u5165"],
] as const)("watch-only stress row uses the exact %s copy", async (locale, text) => {
  const { StressSection } = await import("./stress-section");
  getJadeStress.mockResolvedValue({ ...data, scenarios: [{ ...scenario, holdings: [{ holding_id: "watched", name: "Watched", asset_class: "STOCK", method: "excluded", weight: null, excluded_reason: "watch_only", proxy_symbol: null, proxy_name: null, beta: null, beta_samples: null, own_first_date: null }] }] });
  render(<LocaleProvider routeLocale={locale}><StressSection settings={null} /></LocaleProvider>);
  expect(await screen.findByText(text)).toBeInTheDocument();
});
