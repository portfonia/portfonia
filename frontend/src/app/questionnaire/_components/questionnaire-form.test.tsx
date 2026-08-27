import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

const { putInvestmentContext } = vi.hoisted(() => ({
  putInvestmentContext: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, putInvestmentContext };
});

import { LocaleProvider } from "@/app/_components/locale-provider";
import { QuestionnaireForm } from "./questionnaire-form";
import type { InvestmentContext } from "@/lib/api";

function renderForm(initialContext: InvestmentContext | null) {
  return render(
    <LocaleProvider>
      <QuestionnaireForm initialContext={initialContext} />
    </LocaleProvider>,
  );
}

describe("QuestionnaireForm", () => {
  it("starts on the first question with all defaults pre-selected", () => {
    renderForm(null);
    expect(screen.getByText(/question 1 of 9/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "$100K – $500K" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("advances through Next and reaches the free-text step at the end", async () => {
    const user = userEvent.setup();
    renderForm(null);
    for (let i = 0; i < 8; i++) {
      await user.click(screen.getByRole("button", { name: /next/i }));
    }
    expect(screen.getByText(/question 9 of 9/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/anything else worth knowing/i)).toBeInTheDocument();
  });

  it("clicking straight to Save with all defaults submits the default questionnaire", async () => {
    putInvestmentContext.mockResolvedValue({
      questionnaire: {},
      questionnaire_version: "v1",
      free_text: null,
      updated_at: "2026-08-25T00:00:00Z",
    });
    const user = userEvent.setup();
    renderForm(null);
    for (let i = 0; i < 8; i++) {
      await user.click(screen.getByRole("button", { name: /next/i }));
    }
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(putInvestmentContext).toHaveBeenCalled());
    const [submitted, freeText] = putInvestmentContext.mock.calls[0] as [
      Record<string, unknown>,
      string | null,
    ];
    expect(submitted.style).toBe("GROWTH");
    expect(submitted.horizon).toBe("LONG");
    expect(freeText).toBeNull();
  });

  it("returns to the first question after a successful save (issue #214)", async () => {
    // Re-entering /questionnaire from the menu while already on that route
    // is a same-path Link click (Next.js treats it as a no-op, no remount),
    // so the wizard must reset itself rather than rely on being remounted.
    putInvestmentContext.mockResolvedValue({
      questionnaire: {},
      questionnaire_version: "v1",
      free_text: null,
      updated_at: "2026-08-25T00:00:00Z",
    });
    const user = userEvent.setup();
    renderForm(null);
    for (let i = 0; i < 8; i++) {
      await user.click(screen.getByRole("button", { name: /next/i }));
    }
    expect(screen.getByText(/question 9 of 9/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(screen.getByText(/question 1 of 9/i)).toBeInTheDocument());
  });

  it("toggling a multi-select option changes the answer without affecting others", async () => {
    const user = userEvent.setup();
    renderForm(null);
    await user.click(screen.getByRole("button", { name: /next/i })); // -> markets step
    const usButton = screen.getByRole("button", { name: "US" });
    const hkButton = screen.getByRole("button", { name: "Hong Kong" });
    expect(usButton).toHaveAttribute("aria-pressed", "false");
    await user.click(usButton);
    expect(usButton).toHaveAttribute("aria-pressed", "true");
    expect(hkButton).toHaveAttribute("aria-pressed", "false");
    await user.click(usButton);
    expect(usButton).toHaveAttribute("aria-pressed", "false");
  });

  it("Back returns to the previous question without losing the current answer", async () => {
    const user = userEvent.setup();
    renderForm(null);
    await user.click(screen.getByRole("button", { name: "Over $2M" }));
    await user.click(screen.getByRole("button", { name: /next/i }));
    expect(screen.getByText(/question 2 of 9/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /back/i }));
    expect(screen.getByText(/question 1 of 9/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Over $2M" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("pre-fills from an existing saved context instead of the defaults", () => {
    renderForm({
      questionnaire: {
        asset_scale: "OVER_2M",
        markets: ["HK"],
        style: "VALUE",
        horizon: "SHORT",
        risk_appetite: "CONSERVATIVE",
        sectors_of_interest: [],
        objective: "INCOME",
        intel_focus: "FUNDAMENTALS",
      },
      questionnaire_version: "v1",
      free_text: "existing note",
      updated_at: "2026-08-25T00:00:00Z",
    });
    expect(screen.getByRole("button", { name: "Over $2M" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("shows the save-failed error message when the API call rejects", async () => {
    const { ApiError } = await import("@/lib/api");
    putInvestmentContext.mockRejectedValue(new ApiError(422, "unrecognized style"));
    const user = userEvent.setup();
    renderForm(null);
    for (let i = 0; i < 8; i++) {
      await user.click(screen.getByRole("button", { name: /next/i }));
    }
    await user.click(screen.getByRole("button", { name: /^save$/i }));
    expect(await screen.findByText("unrecognized style")).toBeInTheDocument();
  });
});
