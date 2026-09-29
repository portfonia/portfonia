import type { Metadata } from "next";

import { LegalDocument } from "../_components/legal-document";

// Static English title: locale is client-only (no URL-based locale routing,
// per locales/README.md), so a Server Component's metadata can't read it.
export const metadata: Metadata = {
  title: "Portfonia — Pricing",
};

export default function PricingPage() {
  return <LegalDocument doc="pricing" />;
}
