import type { Metadata } from "next";

import { LegalDocument } from "../_components/legal-document";

export const metadata: Metadata = {
  title: "Portfonia — Privacy Policy",
};

export default function PrivacyPage() {
  return <LegalDocument doc="privacy" />;
}
