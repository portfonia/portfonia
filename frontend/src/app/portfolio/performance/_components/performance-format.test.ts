import { expect, it } from "vitest";
import { formatAxisDate, formatMonthYear, formatShortDate } from "./performance-format";

it("806 A1 formats axis dates according to the chart span", () => {
  expect(formatAxisDate("2023-01-24", "en", 1826)).toContain("2023");
  expect(formatAxisDate("2023-01-24", "en", 90)).toBe(formatShortDate("2023-01-24", "en"));
  expect(formatMonthYear("2023-01-24", "en")).toContain("2023");
});
