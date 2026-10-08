import { describe, expect, it, vi } from "vitest";
import { readdirSync } from "node:fs";
import { catalogs } from "@/locales";
const requestHeaders = vi.hoisted(() => new Headers());
vi.mock("next/headers", () => ({ headers: async () => requestHeaders }));
import { getRouteLocale } from "./seo-server";
import { localizedPath, splitLocalePrefix, isSeoPath, buildPageMetadata, PROTECTED_PATH_PREFIXES } from "./seo";

describe("issue #702 SEO helpers", () => {
  it("localizes only Chinese paths and respects prefix boundaries", () => {
    expect(localizedPath("/pricing", "zh-Hans")).toBe("/zh-Hans/pricing");
    expect(localizedPath("/", "zh-Hant")).toBe("/zh-Hant");
    expect(localizedPath("/pricing", "en")).toBe("/pricing");
    expect(splitLocalePrefix("/zh-Hans")).toEqual({ locale: "zh-Hans", path: "/" });
    expect(splitLocalePrefix("/zh-Hant")).toEqual({ locale: "zh-Hant", path: "/" });
    expect(splitLocalePrefix("/zh-Hant/terms")).toEqual({ locale: "zh-Hant", path: "/terms" });
    expect(splitLocalePrefix("/pricing")).toEqual({ locale: null, path: "/pricing" });
    expect(splitLocalePrefix("/zh-Hansx")).toEqual({ locale: null, path: "/zh-Hansx" });
    expect(splitLocalePrefix("/en/pricing")).toEqual({ locale: null, path: "/en/pricing" });
    expect(isSeoPath("/about")).toBe(true);
    expect(isSeoPath("/pricing/x")).toBe(false);
  });
  it("matches the complete Traditional pricing metadata example", () => {
    const { title, description } = catalogs["zh-Hant"].seo.pages.pricing;
    const image = "https://portfonia.com/og/zh-Hant";
    expect(buildPageMetadata("pricing", "zh-Hant")).toEqual({ title, description,
      alternates: { canonical: "https://portfonia.com/zh-Hant/pricing", languages: {
        en: "https://portfonia.com/pricing", "zh-CN": "https://portfonia.com/zh-Hans/pricing",
        "zh-SG": "https://portfonia.com/zh-Hans/pricing", zh: "https://portfonia.com/zh-Hans/pricing",
        "zh-TW": "https://portfonia.com/zh-Hant/pricing", "zh-HK": "https://portfonia.com/zh-Hant/pricing",
        "zh-MO": "https://portfonia.com/zh-Hant/pricing", "x-default": "https://portfonia.com/pricing",
      } },
      openGraph: { type: "website", siteName: "Portfonia", url: "https://portfonia.com/zh-Hant/pricing",
        title, description, locale: "zh_TW", alternateLocale: ["en_US", "zh_CN"],
        images: [{ url: image, width: 1200, height: 630, alt: catalogs["zh-Hant"].seo.ogImageAlt }] },
      twitter: { card: "summary_large_image", title, description, images: [image] },
    });
  });
  it("accepts only a supported locale header", async () => {
    requestHeaders.set("x-portfonia-locale", "zh-Hans");
    expect(await getRouteLocale()).toBe("zh-Hans");
    requestHeaders.set("x-portfonia-locale", "invalid");
    expect(await getRouteLocale()).toBeNull();
    requestHeaders.delete("x-portfonia-locale");
    expect(await getRouteLocale()).toBeNull();
  });
  it("classifies every app route directory explicitly", () => {
    const publicPaths = ["/about", "/agent", "/forgot-password", "/login", "/pricing", "/privacy", "/refund", "/reset-password", "/signup", "/terms", "/unsubscribe", "/verify-email", "/waitlist"];
    for (const dir of readdirSync("src/app", { withFileTypes: true })) {
      if (!dir.isDirectory() || ["_components", "api", "og"].includes(dir.name)) continue;
      expect([...PROTECTED_PATH_PREFIXES, ...publicPaths], dir.name).toContain(`/${dir.name}`);
    }
  });
});
