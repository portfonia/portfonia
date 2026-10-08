import { getRouteLocale } from "@/lib/seo-server";
import { buildPageMetadata } from "@/lib/seo";
import type { Metadata } from "next";

import { LegalDocument } from "../_components/legal-document";

export default function PrivacyPage() {
  return <LegalDocument doc="privacy" />;
}

export async function generateMetadata(): Promise<Metadata> {
  return buildPageMetadata("privacy", (await getRouteLocale()) ?? "en");
}
