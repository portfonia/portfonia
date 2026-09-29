import type { Metadata } from "next";

import { LegalDocument } from "../_components/legal-document";

// See terms/page.tsx: static English title is an accepted tradeoff because
// locale is client-only and unavailable to this Server Component.
export const metadata: Metadata = {
  title: "Portfonia — Refund Policy",
};

export default function RefundPage() {
  return <LegalDocument doc="refund" />;
}
