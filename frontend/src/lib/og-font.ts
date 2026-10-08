import type { Locale } from "@/locales";

// Same legacy User-Agent as Next's bundled OG loader: request TTF/OTF, not WOFF2.
const FONT_USER_AGENT = "Mozilla/5.0 (Macintosh; U; Intel Mac OS X 10_6_8; de-at) AppleWebKit/533.21.1 (KHTML, like Gecko) Version/5.0.5 Safari/533.21.1";
const FONT_FAMILIES: Record<Locale, readonly [string, string]> = {
  en: ["Geist", "Newsreader"],
  "zh-Hans": ["Noto Sans SC", "Noto Serif SC"],
  "zh-Hant": ["Noto Sans TC", "Noto Serif TC"],
};

export async function loadOgFonts(locale: Locale, text: string) {
  const [sans, serif] = FONT_FAMILIES[locale];
  return Promise.all([
    { name: "Sans", family: sans, weight: 400 as const },
    { name: "Sans", family: sans, weight: 600 as const },
    { name: "Serif", family: serif, weight: 400 as const },
  ].map(async ({ name, family, weight }) => ({
    name, data: await loadFont(family, weight, text), weight, style: "normal" as const,
  })));
}
async function loadFont(family: string, weight: number, text: string): Promise<ArrayBuffer> {
  const query = new URLSearchParams({ family: `${family}:wght@${weight}`, text });
  const response = await fetch(`https://fonts.googleapis.com/css2?${query}`, { headers: { "User-Agent": FONT_USER_AGENT } });
  if (!response.ok) throw new Error(`Font stylesheet request failed: ${response.status}`);
  const css = await response.text();
  const source = css.match(/src:\s*url\(([^)]+)\)\s*format\(['"](?:opentype|truetype)['"]\)/);
  if (!source) throw new Error("No supported OpenType or TrueType font source");
  const font = await fetch(source[1]);
  if (!font.ok) throw new Error(`Font binary request failed: ${font.status}`);
  return font.arrayBuffer();
}
