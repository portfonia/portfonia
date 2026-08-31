import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { uploadHoldings, confirmHoldings, push } = vi.hoisted(() => ({
  uploadHoldings: vi.fn(),
  confirmHoldings: vi.fn(),
  push: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, uploadHoldings, confirmHoldings };
});
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
// lib/api.ts's real (importActual'd) exports now import logout() from
// auth-actions.ts, which pulls in the server-only-guarded Supabase server
// client — that throws when bundled into a Client Component test unless
// mocked here (same pattern as get-started-menu.test.tsx).
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));

import { LocaleProvider } from "@/app/_components/locale-provider";
import type { HoldingOut, UploadPreview } from "@/lib/api";
import { HoldingsManager } from "./holdings-manager";

const _PREVIEW: UploadPreview = {
  valid_rows: [
    {
      name: "Apple Inc.",
      ticker: "AAPL",
      fund_code: null,
      currency: "USD",
      shares: 10,
      avg_cost: 150,
      current_value: null,
      pricing_mode: "auto",
      asset_type: "stock",
      broker: "Fidelity",
      account: null,
      portfolio: null,
      notes: null,
      issues: [],
      confidence: 1,
    },
  ],
  issue_rows: [],
  broker_groups: [],
};

const _CONFIRMED: HoldingOut[] = [];

function renderManager(mode?: "onboarding" | "normal", initialHoldings: HoldingOut[] = []) {
  return render(
    <LocaleProvider>
      <HoldingsManager initialHoldings={initialHoldings} mode={mode} />
    </LocaleProvider>,
  );
}

async function uploadAndSave(user: ReturnType<typeof userEvent.setup>) {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  const file = new File(["content"], "holdings.md", { type: "text/markdown" });
  await user.upload(input, file);
  await screen.findByText(/parsed holdings/i);
  await user.click(screen.getByRole("button", { name: /^save holdings$/i }));
}

describe("HoldingsManager", () => {
  beforeEach(() => {
    push.mockClear();
    uploadHoldings.mockClear();
    confirmHoldings.mockClear();
    uploadHoldings.mockResolvedValue(_PREVIEW);
    confirmHoldings.mockResolvedValue(_CONFIRMED);
  });

  it("normal mode (default): shows Current holdings and Download template, Save stays on the page", async () => {
    const user = userEvent.setup();
    renderManager();
    expect(screen.getByText(/current holdings/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /download template/i })).toBeInTheDocument();

    await uploadAndSave(user);

    await waitFor(() => expect(confirmHoldings).toHaveBeenCalled());
    expect(push).not.toHaveBeenCalled();
  });

  it("onboarding mode: hides Current holdings and Download template (issue #221 §2.3)", () => {
    renderManager("onboarding");
    expect(screen.queryByText(/current holdings/i)).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /download template/i }),
    ).not.toBeInTheDocument();
  });

  it("onboarding mode: Skip for now links to /welcome without saving (issue #280 §9.1)", () => {
    renderManager("onboarding");
    const skip = screen.getByRole("link", { name: /skip for now/i });
    expect(skip).toHaveAttribute("href", "/welcome");
    expect(confirmHoldings).not.toHaveBeenCalled();
  });

  it("normal mode: no onboarding skip link", () => {
    renderManager();
    expect(screen.queryByRole("link", { name: /skip for now/i })).not.toBeInTheDocument();
  });

  it("onboarding mode: Save navigates to /welcome (issue #221 §2.3)", async () => {
    const user = userEvent.setup();
    renderManager("onboarding");

    await uploadAndSave(user);

    await waitFor(() => expect(push).toHaveBeenCalledWith("/welcome"));
  });
});
