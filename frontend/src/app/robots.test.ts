import { expect, it } from "vitest";
import robots from "./robots";
import { PROTECTED_PATH_PREFIXES } from "@/lib/seo";
it("disallows protected and auth flows and publishes the sitemap", () => {
  expect(robots()).toEqual({ rules: [{ userAgent: "*", allow: "/", disallow: [...PROTECTED_PATH_PREFIXES, "/api/", "/agent/revoke"] }], sitemap: "https://portfonia.com/sitemap.xml" });
});
