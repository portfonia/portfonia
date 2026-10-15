import type { Metadata } from "next";
import { catalogs } from "@/locales";
import { buildPageMetadata } from "@/lib/seo";
import { getRouteLocale } from "@/lib/seo-server";

export async function generateMetadata(): Promise<Metadata> {
  return buildPageMetadata("agentGuide", (await getRouteLocale()) ?? "en");
}

export default async function AgentGuidePage() {
  const locale = (await getRouteLocale()) ?? "en";
  const copy = catalogs[locale].agentGuide;
  return <main className="mx-auto w-full max-w-2xl px-6 py-12">
    <h1 className="font-serif text-3xl">{copy.title}</h1>
    <div className="mt-10 flex flex-col gap-8">
      <section>
        <h2 className="font-serif text-xl">{copy.whatHeading}</h2>
        <p className="mt-3 text-sm leading-relaxed text-foreground/80">{copy.what}</p>
      </section>
      <section>
        <h2 className="font-serif text-xl">{copy.howHeading}</h2>
        <p className="mt-3 text-sm leading-relaxed text-foreground/80">{copy.how}</p>
      </section>
      <section>
        <h2 className="font-serif text-xl">{copy.effectHeading}</h2>
        <p className="mt-3 text-sm leading-relaxed text-foreground/80">{copy.effect}</p>
      </section>
    </div>
  </main>;
}
