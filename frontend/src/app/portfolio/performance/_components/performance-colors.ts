import type { BenchmarkCode } from "@/lib/api";

export const PORTFOLIO_COLOR = "var(--chart-1)";
// csi300 uses a dedicated Performance-page token (issue #433), not the
// shared `--chart-5` the /portfolio breakdown charts still use — `--chart-5`
// is a very dark near-black neutral in the dark theme, effectively
// invisible against the card background.
export const BENCHMARK_COLORS: Record<BenchmarkCode, string> = {
  sp500: "var(--chart-2)",
  dow30: "var(--chart-3)",
  nasdaq: "var(--chart-4)",
  csi300: "var(--chart-csi300)",
};
