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

  // #529 (#516 finding 11): display state is phase/hold_reason-agnostic once
  // a vault exists — those fields aren't populated by any real write path
  // yet, so the dashboard must not branch on them even though they're
  // present on the payload here.
  it("shows the disarmed card with no setup link once a vault exists, regardless of phase/hold_reason on the payload", () => {
    renderBody({
      status: "ok",
      vault: {
        vault_id: "11111111-1111-1111-1111-111111111111",
        phase: "ARMED",
        revision: 1,
        hold_reason: "dependency_unavailable",
      },
    });

    expect(screen.getByText(/currently disarmed/i)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /set up vigil/i })).not.toBeInTheDocument();
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
