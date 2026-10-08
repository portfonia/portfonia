import { getRouteLocale } from "@/lib/seo-server";
import { buildPageMetadata } from "@/lib/seo";
import type { Metadata } from "next";

import { LegalDocument } from "../_components/legal-document";

export default function TermsPage() {
  return <LegalDocument doc="terms" />;
}

export async function generateMetadata(): Promise<Metadata> {
  return buildPageMetadata("terms", (await getRouteLocale()) ?? "en");
}
