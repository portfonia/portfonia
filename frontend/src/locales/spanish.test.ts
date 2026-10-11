import { expect, it } from "vitest";
import { isLocale, LOCALES, REPORT_LANGUAGES } from "./index";
import { INTL_LOCALES } from "@/app/portfolio/performance/_components/performance-format";

it("acceptance_05 Spanish is selectable immediately in the agreed order", () => {
  expect(isLocale("es")).toBe(true);
  expect(LOCALES.map(locale => locale.value)).toEqual(["en", "es", "zh-Hans", "zh-Hant"]);
  expect(REPORT_LANGUAGES.map(locale => locale.value)).toEqual(["en", "zh", "zh-Hant"]);
  expect(INTL_LOCALES.es).toBe("es-419");
});
