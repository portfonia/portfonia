import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LocaleProvider } from "@/app/_components/locale-provider";
import VigilRetrievePage from "./page";

// #529 (#516 finding 17): this route stays public but no longer pretends
// the retrieve product exists — it must render a single static not-enabled
// note and must not mount PublicActionShell (no confirm form/button, no
// altcha).
describe("VigilRetrievePage", () => {
  it("renders the retrieve title and a static not-enabled note", () => {
    render(
      <LocaleProvider>
        <VigilRetrievePage />
      </LocaleProvider>,
    );

    expect(screen.getByText(/retrieve file/i)).toBeInTheDocument();
    expect(screen.getByText(/not enabled yet/i)).toBeInTheDocument();
  });

  it("does not mount PublicActionShell (no confirm form/button)", () => {
    render(
      <LocaleProvider>
        <VigilRetrievePage />
      </LocaleProvider>,
    );

    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("form")).not.toBeInTheDocument();
  });
});
