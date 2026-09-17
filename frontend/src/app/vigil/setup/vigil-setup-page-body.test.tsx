import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LocaleProvider } from "@/app/_components/locale-provider";
import { VigilSetupPageBody } from "./vigil-setup-page-body";

describe("VigilSetupPageBody", () => {
  it("renders a not-yet-available placeholder, no form fields", () => {
    render(
      <LocaleProvider>
        <VigilSetupPageBody />
      </LocaleProvider>,
    );

    expect(screen.getByText(/isn't available yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});
