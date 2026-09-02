import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { deleteHolding, reorderHoldings, push } = vi.hoisted(() => ({
  deleteHolding: vi.fn(),
  reorderHoldings: vi.fn(),
  push: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, deleteHolding, reorderHoldings };
});
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));

import { LocaleProvider } from "@/app/_components/locale-provider";
import type { HoldingOut } from "@/lib/api";
import { HoldingsEditor } from "./holdings-editor";

function holding(partial: Partial<HoldingOut> & Pick<HoldingOut, "id" | "name">): HoldingOut {
  return {
    ticker: "AAPL",
    fund_code: null,
    currency: "USD",
    shares: "10",
    avg_cost: "150",
    current_value: null,
    pricing_mode: "auto",
    asset_type: "stock",
    broker: "Fidelity",
    account: null,
    portfolio: null,
    notes: null,
    last_manual_update: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    position: 0,
    ...partial,
  };
}

const AAPL = holding({ id: "11111111-1111-1111-1111-111111111111", name: "Apple Inc." });
const MSFT = holding({
  id: "22222222-2222-2222-2222-222222222222",
  name: "Microsoft",
  ticker: "MSFT",
});

function renderEditor(
  holdings: HoldingOut[] = [AAPL, MSFT],
  opts?: { onboardingIncomplete?: boolean },
) {
  return render(
    <LocaleProvider>
      <HoldingsEditor
        initialHoldings={holdings}
        onboardingIncomplete={opts?.onboardingIncomplete}
      />
    </LocaleProvider>,
  );
}

describe("HoldingsEditor", () => {
  beforeEach(() => {
    push.mockClear();
    deleteHolding.mockReset();
    reorderHoldings.mockReset();
    deleteHolding.mockResolvedValue(undefined);
    reorderHoldings.mockImplementation(async (ids: string[]) =>
      ids.map((id) => (id === AAPL.id ? AAPL : MSFT)),
    );
  });

  it("clicking a row navigates to /holdings/[id]", async () => {
    const user = userEvent.setup();
    renderEditor();
    await user.click(screen.getByText("Apple Inc."));
    expect(push).toHaveBeenCalledWith(`/holdings/${AAPL.id}`);
  });

  it("Add holding links to /holdings/new", () => {
    renderEditor();
    expect(screen.getByRole("link", { name: /add holding/i })).toHaveAttribute(
      "href",
      "/holdings/new",
    );
  });

  it("delete asks for confirm then calls deleteHolding", async () => {
    const user = userEvent.setup();
    renderEditor();
    await user.click(screen.getAllByRole("button", { name: /^delete$/i })[0]);
    expect(deleteHolding).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: /^delete$/i, hidden: false }));
    // The dialog action is "Delete" as well — click the confirm in the dialog.
    const confirms = screen.getAllByRole("button", { name: /^delete$/i });
    await user.click(confirms[confirms.length - 1]);
    await waitFor(() => expect(deleteHolding).toHaveBeenCalledWith(AAPL.id));
    expect(screen.queryByText("Apple Inc.")).not.toBeInTheDocument();
  });

  it("reverts order when reorderHoldings fails", async () => {
    reorderHoldings.mockRejectedValue(new Error("nope"));
    renderEditor();
    const handles = screen.getAllByRole("button", { name: /drag to reorder/i });
    fireEvent.dragStart(handles[0]);
    const rows = screen.getAllByText(/Apple Inc.|Microsoft/);
    const msftRow = rows.find((el) => el.textContent === "Microsoft")?.closest("tr");
    expect(msftRow).toBeTruthy();
    fireEvent.dragOver(msftRow!);
    fireEvent.drop(msftRow!);
    await waitFor(() => expect(reorderHoldings).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.getByText(/could not save the new order/i)).toBeInTheDocument(),
    );
    const names = screen.getAllByRole("row").slice(1).map((row) => row.textContent);
    expect(names[0]).toContain("Apple Inc.");
    expect(names[1]).toContain("Microsoft");
  });

  it("shows an onboarding banner linking to the upload step", () => {
    renderEditor([AAPL], { onboardingIncomplete: true });
    expect(screen.getByRole("link", { name: /go to holdings upload/i })).toHaveAttribute(
      "href",
      "/holdings?onboarding=1",
    );
  });
});
