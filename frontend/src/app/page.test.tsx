import { render } from "@testing-library/react";
import { expect, it, vi } from "vitest";
const requestHeaders = vi.hoisted(() => new Headers());
vi.mock("next/headers", () => ({ headers: async () => requestHeaders }));
vi.mock("./_components/home-sections", () => ({ HomeSections: () => <div data-testid="home" /> }));
import HomePage, { generateMetadata } from "./page";
import { catalogs } from "@/locales";
it("publishes localized metadata and escaped JSON-LD before the home content", async () => {
  requestHeaders.set("x-portfonia-locale", "zh-Hant");
  expect((await generateMetadata()).alternates?.canonical).toBe("https://portfonia.com/zh-Hant");
  const { container } = render(await HomePage());
  const script = container.querySelector('script[type="application/ld+json"]');
  expect(script).not.toBeNull();
  expect(JSON.parse(script?.textContent ?? "")).toEqual([
    { "@context": "https://schema.org", "@type": "Organization", name: "Portfonia", url: "https://portfonia.com" },
    { "@context": "https://schema.org", "@type": "SoftwareApplication", name: "Portfonia", applicationCategory: "FinanceApplication", operatingSystem: "Web", url: "https://portfonia.com/zh-Hant", inLanguage: "zh-Hant", description: catalogs["zh-Hant"].seo.pages.home.description },
  ]);
  expect(container.firstElementChild).toBe(script);
});
it("escapes literal less-than characters in catalog text", async () => {
  requestHeaders.delete("x-portfonia-locale");
  const original = catalogs.en.seo.pages.home.description;
  try {
    catalogs.en.seo.pages.home.description = "sample </script>";
    const { container } = render(await HomePage());
    const text = container.querySelector("script")?.textContent ?? "";
    expect(text).toContain("\\u003c/script>");
    expect(text).not.toContain("</script>");
  } finally { catalogs.en.seo.pages.home.description = original; }
});
