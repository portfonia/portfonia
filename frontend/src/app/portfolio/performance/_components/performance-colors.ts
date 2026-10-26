import type { BenchmarkCode } from "@/lib/api";

export const PORTFOLIO_COLOR = "var(--chart-1)";
// CSI 300 keeps its own Performance-page token for historical reasons (issue #433).
export const BENCHMARK_COLORS: Record<BenchmarkCode, string> = {
  sp500: "var(--chart-2)",
  dow30: "var(--chart-3)",
  nasdaq: "var(--chart-4)",
  csi300: "var(--chart-csi300)",
};
