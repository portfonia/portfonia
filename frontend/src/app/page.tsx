import { getRouteLocale } from "@/lib/seo-server";
import { catalogs } from "@/locales";
import { buildPageMetadata, SITE_URL, localizedPath } from "@/lib/seo";
import type { Metadata } from "next";

import { HomeSections } from "./_components/home-sections";

export default async function HomePage() {
  const locale = (await getRouteLocale()) ?? "en";
  const data = [
    { "@context": "https://schema.org", "@type": "Organization", name: "Portfonia", url: SITE_URL },
    { "@context": "https://schema.org", "@type": "SoftwareApplication", name: "Portfonia", applicationCategory: "FinanceApplication", operatingSystem: "Web", url: SITE_URL + localizedPath("/", locale), inLanguage: locale, description: catalogs[locale].seo.pages.home.description },
  ];
  return <><script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(data).replace(/</g, "\\u003c") }} /><HomeSections /></>;
}

export async function generateMetadata(): Promise<Metadata> {
  return buildPageMetadata("home", (await getRouteLocale()) ?? "en");
}
