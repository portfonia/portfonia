"use client";

import Link from "next/link";

import { useLegalMessages } from "./locale-provider";

type LegalDocKey = "pricing" | "terms" | "privacy" | "refund";

const documentOrder: LegalDocKey[] = ["pricing", "terms", "privacy", "refund"];

// The payment provider acting as Merchant of Record. Legal copy names it only
// through the {merchantOfRecord} placeholder, so switching provider is a
// one-line change here rather than an edit to every locale string.
export const MERCHANT_OF_RECORD = "Paddle.com";

function fillMerchant(text: string): string {
  return text.replaceAll("{merchantOfRecord}", MERCHANT_OF_RECORD);
}

export function LegalDocument({ doc }: { doc: LegalDocKey }) {
  const t = useLegalMessages();
  const content = t[doc];

  return (
    <main className="mx-auto w-full max-w-2xl px-6 py-12">
      <h1 className="font-serif text-3xl">{content.title}</h1>
      <p className="mt-2 text-sm text-foreground/45">{content.lastUpdated}</p>
      <p className="mt-6 text-sm leading-relaxed text-foreground/80">{fillMerchant(content.intro)}</p>

      <div className="mt-10 flex flex-col gap-8">
        {content.sections.map((section) => (
          <section key={section.heading}>
            <h2 className="font-serif text-lg">{section.heading}</h2>
            <div className="mt-2 flex flex-col gap-3">
              {section.body.map((paragraph) => (
                <p key={paragraph} className="text-sm leading-relaxed text-foreground/70">
                  {fillMerchant(paragraph)}
                </p>
              ))}
            </div>
          </section>
        ))}
      </div>

      <nav className="mt-12 flex flex-wrap gap-4 border-t border-white/10 pt-6 text-sm text-foreground/60">
        {documentOrder.filter((key) => key !== doc).map((key) => (
          <Link key={key} href={`/${key}`} className="underline">
            {t.nav[key]}
          </Link>
        ))}
      </nav>
    </main>
  );
}
