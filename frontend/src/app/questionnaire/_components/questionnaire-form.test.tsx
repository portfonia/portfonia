import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { putInvestmentContext, push } = vi.hoisted(() => ({
  putInvestmentContext: vi.fn(),
  push: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, putInvestmentContext };
});
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
// lib/api.ts's real (importActual'd) exports now import logout() from
// auth-actions.ts, which pulls in the server-only-guarded Supabase server
// client — that throws when bundled into a Client Component test unless
// mocked here (same pattern as get-started-menu.test.tsx).
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));

import { LocaleProvider } from "@/app/_components/locale-provider";
import { QuestionnaireForm } from "./questionnaire-form";
import type { InvestmentContext } from "@/lib/api";

function renderForm(
  initialContext: InvestmentContext | null,
  mode?: "onboarding" | "edit",
) {
  return render(
    <LocaleProvider>
      <QuestionnaireForm initialContext={initialContext} mode={mode} />
    </LocaleProvider>,
  );
}

describe("QuestionnaireForm", () => {
  beforeEach(() => {
    push.mockClear();
  });

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

  it("edit mode (default): Save navigates to /profile (issue #221 §2.2)", async () => {
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

    await waitFor(() => expect(push).toHaveBeenCalledWith("/profile"));
  });

  it("onboarding mode: Save and Skip both route to /holdings?onboarding=1 (issue #280 §9.1)", async () => {
    putInvestmentContext.mockResolvedValue({
      questionnaire: {},
      questionnaire_version: "v1",
      free_text: null,
      updated_at: "2026-08-25T00:00:00Z",
    });
    const user = userEvent.setup();
    renderForm(null, "onboarding");
    expect(screen.getByRole("link", { name: /skip/i })).toHaveAttribute(
      "href",
      "/holdings?onboarding=1",
    );
    for (let i = 0; i < 8; i++) {
      await user.click(screen.getByRole("button", { name: /next/i }));
    }
    await user.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(push).toHaveBeenCalledWith("/holdings?onboarding=1"));
    expect(push).not.toHaveBeenCalledWith("/welcome");
  });

  it("edit mode: Skip links to /profile", () => {
    renderForm(null, "edit");
    expect(screen.getByRole("link", { name: /skip/i })).toHaveAttribute("href", "/profile");
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

  it("shows a question-level hint under the legend (issue #333)", () => {
    renderForm(null);
    expect(
      screen.getByText(
        "Used only to gauge how much background context your reports should assume — not stored as a precise figure and never used for asset-allocation math.",
      ),
    ).toBeInTheDocument();
  });

  it("shows option-level hints under each option for an in-scope dim (issue #333)", async () => {
    const user = userEvent.setup();
    renderForm(null);
    await user.click(screen.getByRole("button", { name: /next/i })); // -> markets step
    expect(
      screen.getByText(
        "US-listed equities, priced in USD — most sensitive to Fed policy and US economic data.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Any market outside the three above, e.g. Europe, Japan, Korea.",
      ),
    ).toBeInTheDocument();
  });

  it("does not render option-level hints for an excluded dim (asset_scale, issue #333)", () => {
    renderForm(null);
    // asset_scale has a question-level hint but no optionHints entry at all —
    // the option buttons must render only their label, no extra hint line.
    const button = screen.getByRole("button", { name: "Under $100K" });
    expect(button.parentElement).toHaveTextContent("Under $100K");
    expect(button.parentElement?.querySelectorAll("p")).toHaveLength(0);
  });

  it("does not render option-level hints for the other excluded dim (horizon, issue #333)", async () => {
    const user = userEvent.setup();
    renderForm(null);
    for (let i = 0; i < 3; i++) {
      await user.click(screen.getByRole("button", { name: /next/i })); // -> horizon step
    }
    expect(screen.getByText(/typical holding period/i)).toBeInTheDocument();
    const button = screen.getByRole("button", { name: /medium-term/i });
    expect(button.parentElement?.querySelectorAll("p")).toHaveLength(0);
  });
});
