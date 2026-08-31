import type { Metadata } from "next";

import { LegalDocument } from "../_components/legal-document";

// See terms/page.tsx: static English title is an accepted tradeoff, not an
// oversight — locale is client-only, unreachable from this Server Component.
export const metadata: Metadata = {
  title: "Portfonia — Privacy Policy",
};

export default function PrivacyPage() {
  return <LegalDocument doc="privacy" />;
}
