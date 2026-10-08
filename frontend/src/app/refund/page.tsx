import { getRouteLocale } from "@/lib/seo-server";
import { buildPageMetadata } from "@/lib/seo";
import type { Metadata } from "next";

import { LegalDocument } from "../_components/legal-document";

export default function RefundPage() {
  return <LegalDocument doc="refund" />;
}

export async function generateMetadata(): Promise<Metadata> {
  return buildPageMetadata("refund", (await getRouteLocale()) ?? "en");
}
