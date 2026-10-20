import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { catalogs } from "@/locales";
import { LocaleProvider } from "@/app/_components/locale-provider";
import { ExpiredSessionBanner } from "./expired-session-banner";

function renderBanner(reason?: string) {
  return render(
    <LocaleProvider routeLocale={null}>
      <ExpiredSessionBanner reason={reason} />
    </LocaleProvider>,
  );
}

describe("ExpiredSessionBanner", () => {
  it("renders the expired-session notice when reason=expired", () => {
    renderBanner("expired");

    expect(
      screen.getByText(catalogs.en.menu.sessionExpired),
    ).toBeInTheDocument();
  });

  it("renders nothing for any other or missing reason", () => {
    const { container, rerender } = render(
      <LocaleProvider routeLocale={null}>
        <ExpiredSessionBanner />
      </LocaleProvider>,
    );
    expect(container).toBeEmptyDOMElement();

    rerender(
      <LocaleProvider routeLocale={null}>
        <ExpiredSessionBanner reason="something_else" />
      </LocaleProvider>,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
