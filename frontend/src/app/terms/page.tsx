import type { Metadata } from "next";

import { LegalDocument } from "../_components/legal-document";

export const metadata: Metadata = {
  title: "Portfonia — Terms of Service",
};

export default function TermsPage() {
  return <LegalDocument doc="terms" />;
}
