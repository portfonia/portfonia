import { act, render, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { LocaleProvider } from "@/app/_components/locale-provider";
const api = vi.hoisted(() => ({ getJadeTailRisk: vi.fn(), getJadeStress: vi.fn(), getJadeStyle: vi.fn() }));
vi.mock("@/lib/api", async () => ({ ...await vi.importActual<typeof import("@/lib/api")>("@/lib/api"), ...api }));
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));
import { TailRiskSection } from "./tail-risk-section";
import { StressSection } from "./stress-section";
import { StyleSection } from "./style-section";
it.each(["tail", "stress", "style"] as const)("B14 %s ignores range-only settings changes", async name => {
  const fn = name === "tail" ? api.getJadeTailRisk : name === "stress" ? api.getJadeStress : api.getJadeStyle;
  fn.mockResolvedValue({ status: "pending", base_currency: "USD", benchmark: "sp500", benchmark_symbol: "SPY", points: [], coverage: {}, scenarios: [], portfolio: null });
  const Component = name === "tail" ? TailRiskSection : name === "stress" ? StressSection : StyleSection;
  const wrap = (range: "1Y" | "3Y") => <LocaleProvider routeLocale={null}><Component settings={{ currency: "USD", benchmark: "sp500", ...{ range } }} /></LocaleProvider>;
  const { rerender } = render(wrap("1Y"));
  await waitFor(() => expect(fn).toHaveBeenCalledOnce()); await act(async () => {});
  rerender(wrap("3Y")); await act(async () => {});
  expect(fn).toHaveBeenCalledOnce();
});
