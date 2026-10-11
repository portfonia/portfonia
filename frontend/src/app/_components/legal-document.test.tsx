import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { catalogs } from "@/locales";
import { LegalDocument, MERCHANT_OF_RECORD } from "./legal-document";
import { LocaleProvider } from "./locale-provider";

describe("LegalDocument", () => {
  it.each(["pricing", "refund"] as const)("renders %s and links to the other documents", (doc) => {
    render(
      <LocaleProvider routeLocale={null}>
        <LegalDocument doc={doc} />
      </LocaleProvider>,
    );

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(catalogs.en.legal[doc].title);
    expect(screen.getByRole("heading", { level: 2, name: catalogs.en.legal[doc].sections[0].heading })).toBeInTheDocument();
    const links = within(screen.getByRole("navigation")).getAllByRole("link");
    expect(links.map((link) => link.getAttribute("href"))).toEqual(
      ["pricing", "terms", "privacy", "refund"].filter((key) => key !== doc).map((key) => `/${key}`),
    );
  });

  it("renders the reseller notice and merchant name without leftover placeholders", () => {
    render(
      <LocaleProvider routeLocale={null}>
        <LegalDocument doc="refund" />
      </LocaleProvider>,
    );

    const main = screen.getByRole("main");
    expect(main).toHaveTextContent(catalogs.en.legal.resellerNotice);
    expect(main).toHaveTextContent(`${MERCHANT_OF_RECORD}'s Buyer Terms`);
    expect(main.textContent).not.toMatch(/\{(merchantOfRecord|resellerNotice)\}/);
  });
});


it.each(["pricing", "privacy", "terms", "refund"] as const)("renders language choices at the bottom of %s", (doc) => {
  window.history.replaceState(null, "", `/zh-Hans/${doc}`);
  const { container } = render(<LocaleProvider routeLocale="zh-Hans"><LegalDocument doc={doc} /></LocaleProvider>);
  expect(container.querySelector(`a[href="/zh-Hant/${doc}"]`)).not.toBeNull();
  window.history.replaceState(null, "", "/");
});

it.each(["pricing", "terms", "privacy", "refund"] as const)("acceptance_09 %s Spanish notice links to English and is absent in other locales", (doc) => {
  const notice = "Esta traducción se ofrece solo como referencia. En caso de discrepancia, prevalece la versión en inglés.";
  for (const locale of ["es", "en", "zh-Hans", "zh-Hant"] as const) {
    window.history.replaceState(null, "", locale === "en" ? `/${doc}` : `/${locale}/${doc}`);
    const { container, unmount } = render(<LocaleProvider routeLocale={locale}><LegalDocument doc={doc} /></LocaleProvider>);
    if (locale === "es") {
      const paragraph = screen.getByText(notice, { exact: false });
      expect(paragraph.tagName).toBe("P");
      expect(paragraph.previousElementSibling?.textContent).toBe(catalogs.es.legal[doc].lastUpdated);
      expect(within(paragraph).getByRole("link", { name: "Ver la versión en inglés" })).toHaveAttribute("href", `/${doc}`);
    } else {
      expect(container.textContent).not.toContain(notice);
      expect(container.querySelector('[data-english-prevails]')).toBeNull();
    }
    unmount();
  }
  window.history.replaceState(null, "", "/");
});
