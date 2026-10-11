import { getRouteLocale } from "@/lib/seo-server";
import { buildPageMetadata } from "@/lib/seo";
import type { Metadata } from "next";

import { LegalDocument } from "../_components/legal-document";
import { BuyCreditsLink } from "./buy-credits-link";
import { PlanComparison } from "./plan-comparison";

export default function PricingPage() {
  return <><LegalDocument doc="pricing" afterIntro={<PlanComparison />} /><BuyCreditsLink /></>;
}

export async function generateMetadata(): Promise<Metadata> {
  return buildPageMetadata("pricing", (await getRouteLocale()) ?? "en");
}
