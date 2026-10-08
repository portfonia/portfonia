import { ImageResponse } from "next/og";
import { catalogs, isLocale } from "@/locales";
import { loadOgFonts } from "@/lib/og-font";

export const dynamic = "force-static";
export const dynamicParams = false;
export function generateStaticParams() {
  return [{ locale: "en" }, { locale: "zh-Hans" }, { locale: "zh-Hant" }];
}

const TIER_COLORS = { established: "#5fbe8b", probable: "#dcaa4a", speculative: "#8b93a8" };
const BRACE = `data:image/svg+xml,${encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="400" viewBox="0 0 64 400"><path d="M6 4 C30 4 28 24 28 60 L28 168 C28 188 38 198 58 200 C38 202 28 212 28 232 L28 340 C28 376 30 396 6 396" fill="none" stroke="#dcaa4a" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/></svg>')}`;

export async function GET(_request: Request, { params }: { params: Promise<{ locale: string }> }) {
  const { locale } = await params;
  if (!isLocale(locale)) return new Response(null, { status: 404 });
  const catalog = catalogs[locale];
  const copy = catalog.seo.og;
  const brand = catalog.common.brandName;
  const { how, preview } = catalog.home;
  const inputs = [
    { title: how.cards[0].title, chips: copy.markets },
    { title: how.cards[1].title, chips: how.cards[1].tags },
  ];
  const sections = [
    { number: "1", title: preview.snapshotTitle },
    { number: "2", title: preview.macroTitle },
    { number: "", title: preview.calendarTitle },
    { number: "3", title: preview.analysisTitle },
    { number: "4", title: preview.radarTitle },
  ];
  const tiers = (Object.keys(TIER_COLORS) as (keyof typeof TIER_COLORS)[]).map(key => ({ text: how.tiers[key], color: TIER_COLORS[key] }));
  const text = [brand, copy.headline, preview.tag, ...inputs.flatMap(input => [input.title, ...input.chips]), copy.sampleTitle, ...sections.flatMap(section => [section.number, section.title]), how.confidenceLead, ...tiers.map(tier => tier.text)].join(" ");
  const fonts = await loadOgFonts(locale, text);
  return new ImageResponse(
    <div style={{ display: "flex", flexDirection: "column", width: "100%", height: "100%", padding: "34px 52px 36px", backgroundColor: "#050f13", color: "#eef3f1", fontFamily: "Sans" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", flexShrink: 0 }}>
        <div style={{ display: "flex", color: "#dcaa4a", fontSize: 30, fontWeight: 600 }}>{brand}</div>
        <div style={{ display: "flex", color: "#8fa19d", fontSize: 22 }}>{preview.tag}</div>
      </div>
      <div style={{ display: "flex", fontFamily: "Serif", fontSize: locale === "en" ? 56 : 52, lineHeight: 1.15, marginTop: 8, flexShrink: 0 }}>{copy.headline}</div>
      <div style={{ display: "flex", marginTop: 24, flex: 1, alignItems: "stretch" }}>
        <div style={{ display: "flex", flexDirection: "column", justifyContent: "center", width: 356, gap: 16, flexShrink: 0 }}>
          {inputs.map(input => (
            <div key={input.title} style={{ display: "flex", flexDirection: "column", padding: "20px 22px", borderRadius: 18, backgroundColor: "#0d1c22", border: "1.5px solid rgba(238,243,241,0.12)", flexShrink: 0 }}>
              <div style={{ display: "flex", fontSize: locale === "en" ? 30 : 32, fontWeight: 600 }}>{input.title}</div>
              <div style={{ display: "flex", marginTop: 14, gap: 8, alignItems: "flex-start", flexShrink: 0 }}>
                {input.chips.map(chip => <div key={chip} style={{ display: "flex", padding: "4px 12px", borderRadius: 8, backgroundColor: "#142a32", fontSize: 23, flexShrink: 0 }}>{chip}</div>)}
              </div>
            </div>
          ))}
        </div>
        <div style={{ display: "flex", width: 104, alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
          {/* ImageResponse renders a nested SVG image directly. */}
          {/* eslint-disable-next-line @next/next/no-img-element, jsx-a11y/alt-text */}
          <img src={BRACE} width={64} height={locale === "en" ? 328 : 310} />
        </div>
        <div style={{ display: "flex", flexDirection: "column", justifyContent: "space-between", flex: 1, padding: "12px 28px 16px", borderRadius: 20, backgroundColor: "#0d1c22", border: "1.5px solid #dcaa4a" }}>
          <div style={{ display: "flex", fontFamily: "Serif", fontSize: 28, color: "#dcaa4a", lineHeight: 1.4, flexShrink: 0 }}>{copy.sampleTitle}</div>
          {sections.map(section => (
            <div key={section.title} style={{ display: "flex", borderTop: "1px solid rgba(238,243,241,0.12)", padding: "4px 0", alignItems: "center", flexShrink: 0 }}>
              <div style={{ display: "flex", width: 40, color: "#dcaa4a", fontSize: 24, fontWeight: 600, flexShrink: 0 }}>{section.number}</div>
              <div style={{ display: "flex", fontSize: locale === "en" ? 29 : 30, fontWeight: 600, flexShrink: 0 }}>{section.title}</div>
            </div>
          ))}
          <div style={{ display: "flex", flexDirection: "column", borderTop: "1px solid rgba(238,243,241,0.12)", paddingTop: 10, flexShrink: 0 }}>
            <div style={{ display: "flex", fontSize: 20, color: "#8fa19d" }}>{how.confidenceLead}</div>
            <div style={{ display: "flex", marginTop: 10, gap: 10, flexShrink: 0 }}>
              {tiers.map(tier => (
                <div key={tier.text} style={{ display: "flex", alignItems: "center", gap: 8, border: `1.5px solid ${tier.color}`, color: tier.color, fontSize: 20, padding: "5px 14px", borderRadius: 999, flexShrink: 0 }}>
                  <div style={{ display: "flex", width: 10, height: 10, backgroundColor: tier.color, borderRadius: 999, flexShrink: 0 }} />
                  {tier.text}
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>,
    { width: 1200, height: 630, fonts },
  );
}
