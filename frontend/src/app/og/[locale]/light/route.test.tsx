import type { ReactElement } from "react";
import { expect, it, vi } from "vitest";

const { imageResponse } = vi.hoisted(() => ({ imageResponse: vi.fn() }));
vi.mock("@/lib/og-font", () => ({ loadOgFonts: vi.fn(async () => []) }));
vi.mock("next/og", () => ({ ImageResponse: class extends Response {
  constructor(element: ReactElement) {
    super("image", { headers: { "content-type": "image/png" } });
    imageResponse(element);
  }
} }));
import { GET, generateStaticParams, dynamic } from "./route";

it("856 A7 returns all four localized images with the light palette and rejects invalid locales", async () => {
  expect(dynamic).toBe("force-static");
  expect(generateStaticParams().map(({ locale }) => locale)).toEqual(["en", "es", "zh-Hans", "zh-Hant"]);
  for (const locale of ["en", "es", "zh-Hans", "zh-Hant"]) {
    const response = await GET(new Request("https://example.com"), { params: Promise.resolve({ locale }) });
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("image/png");
    const tree = JSON.stringify(imageResponse.mock.lastCall?.[0]);
    expect(tree).toContain('"backgroundColor":"#f6f8f7"');
    for (const color of ["#ffffff", "#e8eeec", "#1a7350", "#5a6a80", "#50625e", "#80611a", "#0b1a20", "rgba(5,15,19,0.12)"]) expect(tree).toContain(color);
    expect(tree).toContain(encodeURIComponent('stroke="#80611a"'));
  }
  imageResponse.mockClear();
  expect((await GET(new Request("https://example.com"), { params: Promise.resolve({ locale: "invalid" }) })).status).toBe(404);
  expect(imageResponse).not.toHaveBeenCalled();
});
