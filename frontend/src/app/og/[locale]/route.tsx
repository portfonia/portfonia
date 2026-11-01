import { isLocale } from "@/locales";
import { renderOgImage, OG_PALETTE_DARK } from "./og-image";

export const dynamic = "force-static";
export const dynamicParams = false;
export function generateStaticParams() {
  return [{ locale: "en" }, { locale: "es" }, { locale: "zh-Hans" }, { locale: "zh-Hant" }];
}

export async function GET(_request: Request, { params }: { params: Promise<{ locale: string }> }) {
  const { locale } = await params;
  if (!isLocale(locale)) return new Response(null, { status: 404 });
  return renderOgImage(locale, OG_PALETTE_DARK);
}
