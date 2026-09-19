import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LocaleProvider } from "@/app/_components/locale-provider";
import VigilRevokePage from "./page";

// #529 (#516 finding 17): this route stays public but no longer pretends
// the revoke product exists — it must render a single static not-enabled
// note and must not mount PublicActionShell (no confirm form/button, no
// altcha).
describe("VigilRevokePage", () => {
  it("renders the revoke title and a static not-enabled note", () => {
    render(
      <LocaleProvider>
        <VigilRevokePage />
      </LocaleProvider>,
    );

    expect(screen.getByText(/revoke release/i)).toBeInTheDocument();
    expect(screen.getByText(/not enabled yet/i)).toBeInTheDocument();
  });

  it("does not mount PublicActionShell (no confirm form/button)", () => {
    render(
      <LocaleProvider>
        <VigilRevokePage />
      </LocaleProvider>,
    );

    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("form")).not.toBeInTheDocument();
  });
});
