import type { MetadataRoute } from "next";
import { PROTECTED_PATH_PREFIXES, SITE_URL } from "@/lib/seo";
export default function robots(): MetadataRoute.Robots {
  return { rules: [{ userAgent: "*", allow: "/", disallow: [...PROTECTED_PATH_PREFIXES, "/login", "/signup", "/forgot-password", "/reset-password", "/verify-email", "/unsubscribe", "/api/", "/agent/revoke"] }], sitemap: `${SITE_URL}/sitemap.xml` };
}
