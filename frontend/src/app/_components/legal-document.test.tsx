import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { catalogs } from "@/locales";
import { LegalDocument } from "./legal-document";
import { LocaleProvider } from "./locale-provider";

describe("LegalDocument", () => {
  it.each(["pricing", "refund"] as const)("renders %s and links to the other documents", (doc) => {
    render(
      <LocaleProvider>
        <LegalDocument doc={doc} />
      </LocaleProvider>,
    );

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(catalogs.en.legal[doc].title);
    expect(screen.getByRole("heading", { level: 2, name: catalogs.en.legal[doc].sections[0].heading })).toBeInTheDocument();
    const links = within(screen.getByRole("main")).getAllByRole("link");
    expect(links.map((link) => link.getAttribute("href"))).toEqual(
      ["pricing", "terms", "privacy", "refund"].filter((key) => key !== doc).map((key) => `/${key}`),
    );
  });
});
