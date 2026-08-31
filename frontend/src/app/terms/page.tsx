import type { Metadata } from "next";

import { LegalDocument } from "../_components/legal-document";

// Static English title: locale is client-only (no URL-based locale routing,
// per locales/README.md), so a Server Component's metadata can't read it —
// the browser tab stays English even for zh-Hans visitors, same accepted
// tradeoff as every other route here shipping no metadata at all.
export const metadata: Metadata = {
  title: "Portfonia — Terms of Service",
};

export default function TermsPage() {
  return <LegalDocument doc="terms" />;
}
