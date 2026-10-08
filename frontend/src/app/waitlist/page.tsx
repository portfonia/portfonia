import { LanguageLinks } from "../_components/language-links";
import { getRouteLocale } from "@/lib/seo-server";
import { buildPageMetadata } from "@/lib/seo";
import type { Metadata } from "next";
import { WaitlistForm } from "./waitlist-form";
import { WaitlistHeading } from "./waitlist-heading";

export default function WaitlistPage() {
  return (
    <main className="mx-auto flex max-w-lg flex-col gap-8 px-4 py-24">
      <WaitlistHeading />
      <WaitlistForm />
      <LanguageLinks />
    </main>
  );
}

export async function generateMetadata(): Promise<Metadata> {
  return buildPageMetadata("waitlist", (await getRouteLocale()) ?? "en");
}
