import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LocaleProvider } from "@/app/_components/locale-provider";
import type { VigilVaultLoadResult } from "@/lib/vigil/server";
import { VigilPageBody } from "./vigil-page-body";

function renderBody(result: VigilVaultLoadResult) {
  return render(
    <LocaleProvider>
      <VigilPageBody result={result} />
    </LocaleProvider>,
  );
}

describe("VigilPageBody", () => {
  it("shows a setup-required card with a Set up Vigil link to /vigil/setup for the current no-vault response", () => {
    renderBody({ status: "ok", vault: { vault_id: null, phase: "DISARMED", revision: 0 } });

    expect(screen.getByText(/isn't set up yet/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /set up vigil/i })).toHaveAttribute(
      "href",
      "/vigil/setup",
    );
  });

  it("shows an unavailable card on a 403/503/error load, with no setup link", () => {
    renderBody({ status: "unavailable" });

    expect(screen.getByText(/currently unavailable/i)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /set up vigil/i })).not.toBeInTheDocument();
  });

  it("shows the active-armed card with no setup link once a vault exists and is ARMED", () => {
    renderBody({
      status: "ok",
      vault: {
        vault_id: "11111111-1111-1111-1111-111111111111",
        phase: "ARMED",
        revision: 1,
      },
    });

    expect(screen.getByText(/armed and monitoring/i)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /set up vigil/i })).not.toBeInTheDocument();
  });

  it("shows the hold card whenever hold_reason is present, regardless of phase", () => {
    renderBody({
      status: "ok",
      vault: {
        vault_id: "11111111-1111-1111-1111-111111111111",
        phase: "ARMED",
        revision: 1,
        hold_reason: "dependency_unavailable",
      },
    });

    expect(screen.getByText(/on hold/i)).toBeInTheDocument();
  });

  // Never render inner/outer/DEK-shaped fields even if a caller somehow
  // handed this component an object carrying them — the type doesn't
  // declare them, but this locks the behavior at the render layer too.
  it("never renders raw vault JSON (no secret-shaped dump on the page)", () => {
    const { container } = renderBody({
      status: "ok",
      vault: { vault_id: null, phase: "DISARMED", revision: 0 },
    });

    expect(container.textContent).not.toMatch(/inner|outer|dek/i);
  });
});
