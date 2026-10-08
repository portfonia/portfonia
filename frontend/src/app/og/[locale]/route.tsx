import { ImageResponse } from "next/og";
import { catalogs, isLocale } from "@/locales";
import { loadOgFont } from "@/lib/og-font";

export const dynamic = "force-static";
export const dynamicParams = false;
export function generateStaticParams() {
  return [{ locale: "en" }, { locale: "zh-Hans" }, { locale: "zh-Hant" }];
}

export async function GET(_request: Request, { params }: { params: Promise<{ locale: string }> }) {
  const { locale } = await params;
  if (!isLocale(locale)) return new Response(null, { status: 404 });
  const copy = catalogs[locale].seo.og;
  const brand = catalogs[locale].common.brandName;
  const text = [brand, copy.headline, copy.subline, copy.sampleTitle, ...copy.sampleRows].join(" ");
  const data = await loadOgFont(locale, text);
  return new ImageResponse(
    <div style={{ display: "flex", flexDirection: "column", width: "100%", height: "100%", padding: "48px 56px", backgroundColor: "#0b0b0c", color: "#eeeae0", fontFamily: "Noto Sans" }}>
      <div style={{ display: "flex", color: "#dcaa4a", fontSize: 30 }}>{brand}</div>
      <div style={{ display: "flex", fontSize: locale === "en" ? 54 : 50, marginTop: 28, maxWidth: 1080, lineHeight: 1.2 }}>{copy.headline}</div>
      <div style={{ display: "flex", fontSize: 24, marginTop: 20, color: "#b7b2a8", maxWidth: 1000, lineHeight: 1.4 }}>{copy.subline}</div>
      <div style={{ display: "flex", flexDirection: "column", position: "absolute", right: 56, bottom: 44, width: 740, padding: "24px 28px", borderRadius: 20, border: "1px solid #3a362e", backgroundColor: "#191817" }}>
        <div style={{ display: "flex", color: "#dcaa4a", fontSize: 22, marginBottom: 14 }}>{copy.sampleTitle}</div>
        {copy.sampleRows.map((row) => <div key={row} style={{ display: "flex", fontSize: 21, lineHeight: 1.5, marginTop: 5 }}>{row}</div>)}
      </div>
    </div>,
    { width: 1200, height: 630, fonts: [{ name: "Noto Sans", data, weight: 400, style: "normal" }] },
  );
}
