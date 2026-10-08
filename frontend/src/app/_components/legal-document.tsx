"use client";

import { LanguageLinks } from "./language-links";
import Link from "next/link";

import { useLegalMessages, useLocalizedHref } from "./locale-provider";

type LegalDocKey = "pricing" | "terms" | "privacy" | "refund";

const documentOrder: LegalDocKey[] = ["pricing", "terms", "privacy", "refund"];

// Payment provider acting as Merchant of Record. Legal copy never names it
// literally: general mentions use {merchantOfRecord}, and the provider's
// mandated reseller notice (verbatim wording, per locale) lives in
// legal.resellerNotice and is inserted at {resellerNotice}. Switching provider
// means changing this constant and rewriting legal.resellerNotice.
export const MERCHANT_OF_RECORD = "Paddle";

function fillMerchant(text: string, resellerNotice: string): string {
  return text
    .replaceAll("{resellerNotice}", resellerNotice)
    .replaceAll("{merchantOfRecord}", MERCHANT_OF_RECORD);
}

export function LegalDocument({ doc }: { doc: LegalDocKey }) {
  const t = useLegalMessages();
  const href = useLocalizedHref();
  const content = t[doc];

  return (
    <main className="mx-auto w-full max-w-2xl px-6 py-12">
      <h1 className="font-serif text-3xl">{content.title}</h1>
      <p className="mt-2 text-sm text-foreground/45">{content.lastUpdated}</p>
      <p className="mt-6 text-sm leading-relaxed text-foreground/80">{fillMerchant(content.intro, t.resellerNotice)}</p>

      <div className="mt-10 flex flex-col gap-8">
        {content.sections.map((section) => (
          <section key={section.heading}>
            <h2 className="font-serif text-lg">{section.heading}</h2>
            <div className="mt-2 flex flex-col gap-3">
              {section.body.map((paragraph) => (
                <p key={paragraph} className="text-sm leading-relaxed text-foreground/70">
                  {fillMerchant(paragraph, t.resellerNotice)}
                </p>
              ))}
            </div>
          </section>
        ))}
      </div>

      <nav className="mt-12 flex flex-wrap gap-4 border-t border-white/10 pt-6 text-sm text-foreground/60">
        {documentOrder.filter((key) => key !== doc).map((key) => (
          <Link key={key} href={href(`/${key}`)} className="underline">
            {t.nav[key]}
          </Link>
        ))}
      </nav>
      <LanguageLinks />
    </main>
  );
}
