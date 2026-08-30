"use client";

import Link from "next/link";

import { useLegalMessages } from "./locale-provider";

type LegalDocKey = "terms" | "privacy";

export function LegalDocument({ doc }: { doc: LegalDocKey }) {
  const t = useLegalMessages();
  const content = t[doc];
  const otherDoc = doc === "terms" ? "privacy" : "terms";
  const otherHref = doc === "terms" ? "/privacy" : "/terms";

  return (
    <main className="mx-auto w-full max-w-2xl px-6 py-12">
      <h1 className="font-serif text-3xl">{content.title}</h1>
      <p className="mt-2 text-sm text-foreground/45">{content.lastUpdated}</p>
      <p className="mt-6 text-sm leading-relaxed text-foreground/80">{content.intro}</p>
      {content.translationPending ? (
        <p className="mt-4 rounded-md border border-white/10 bg-white/5 px-4 py-3 text-sm text-foreground/60">
          {content.translationPending}
        </p>
      ) : null}

      <div className="mt-10 flex flex-col gap-8">
        {content.sections.map((section) => (
          <section key={section.heading}>
            <h2 className="font-serif text-lg">{section.heading}</h2>
            <div className="mt-2 flex flex-col gap-3">
              {section.body.map((paragraph) => (
                <p key={paragraph} className="text-sm leading-relaxed text-foreground/70">
                  {paragraph}
                </p>
              ))}
            </div>
          </section>
        ))}
      </div>

      <p className="mt-12 border-t border-white/10 pt-6 text-sm text-foreground/60">
        <Link href={otherHref} className="underline">
          {t.nav[otherDoc]}
        </Link>
      </p>
    </main>
  );
}
