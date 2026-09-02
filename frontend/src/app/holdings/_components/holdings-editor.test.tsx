import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { deleteHolding, reorderHoldings, updateHolding, push } = vi.hoisted(() => ({
  deleteHolding: vi.fn(),
  reorderHoldings: vi.fn(),
  updateHolding: vi.fn(),
  push: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, deleteHolding, reorderHoldings, updateHolding };
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
    capture_supported: true,
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
    updateHolding.mockReset();
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

  it("has a back link to /holdings at the bottom (issue #319 item 3)", () => {
    renderEditor();
    expect(screen.getByRole("link", { name: /back to holdings/i })).toHaveAttribute(
      "href",
      "/holdings",
    );
  });

  describe("inline editing (issue #319 items 4-5)", () => {
    it("edits shares on blur without navigating to the detail page", async () => {
      const user = userEvent.setup();
      updateHolding.mockResolvedValue({ ...AAPL, shares: "25" });
      renderEditor();

      const sharesInput = screen.getAllByDisplayValue("10")[0];
      await user.clear(sharesInput);
      await user.type(sharesInput, "25");
      await user.tab();

      await waitFor(() =>
        expect(updateHolding).toHaveBeenCalledWith(AAPL.id, { shares: 25 }),
      );
      expect(push).not.toHaveBeenCalled();
    });

    it("stopPropagation on the editable cell click so it does not also navigate", async () => {
      const user = userEvent.setup();
      renderEditor();
      const sharesInput = screen.getAllByDisplayValue("10")[0];
      await user.click(sharesInput);
      expect(push).not.toHaveBeenCalled();
    });

    it("rolls back the field on a failed save", async () => {
      const user = userEvent.setup();
      updateHolding.mockRejectedValue(new Error("nope"));
      renderEditor();

      const avgCostInput = screen.getAllByDisplayValue("150")[0];
      await user.clear(avgCostInput);
      await user.type(avgCostInput, "999");
      await user.tab();

      await waitFor(() => expect(updateHolding).toHaveBeenCalled());
      await waitFor(() =>
        expect(screen.getByText(/could not update this holding/i)).toBeInTheDocument(),
      );
      expect(screen.getAllByDisplayValue("150")[0]).toBeInTheDocument();
    });

    it("edits pricing_mode via a select and saves immediately", async () => {
      const user = userEvent.setup();
      updateHolding.mockResolvedValue({ ...AAPL, pricing_mode: "manual" });
      renderEditor();

      const selects = screen.getAllByDisplayValue("Auto (market price)");
      await user.selectOptions(selects[0], "manual");

      await waitFor(() =>
        expect(updateHolding).toHaveBeenCalledWith(AAPL.id, { pricing_mode: "manual" }),
      );
      expect(push).not.toHaveBeenCalled();
    });

    it("renders current_value (not avg_cost) as the editable cell for a cash/wmf row", () => {
      const cash = holding({
        id: "33333333-3333-3333-3333-333333333333",
        name: "USD Cash",
        ticker: null,
        asset_type: "cash",
        shares: null,
        avg_cost: null,
        current_value: "50000",
        pricing_mode: "manual",
      });
      renderEditor([cash]);
      expect(screen.getByDisplayValue("50000")).toBeInTheDocument();
      // pricing_mode is locked to manual for cash/wmf, matching holding-form.tsx.
      const select = screen.getByDisplayValue("Manual") as HTMLSelectElement;
      expect(select).toBeDisabled();
    });

    it("clicking elsewhere on the row still navigates to the detail page", async () => {
      const user = userEvent.setup();
      renderEditor();
      await user.click(screen.getByText("Apple Inc."));
      expect(push).toHaveBeenCalledWith(`/holdings/${AAPL.id}`);
    });
  });

  describe("sortable headers (issue #319 item 12)", () => {
    it("sorting by ticker ascending calls reorderHoldings with the sorted id order", async () => {
      const user = userEvent.setup();
      renderEditor();
      await user.click(screen.getByRole("button", { name: /sort ascending: ticker/i }));
      await waitFor(() =>
        expect(reorderHoldings).toHaveBeenCalledWith([AAPL.id, MSFT.id]),
      );
    });

    it("sorting by ticker descending reverses the order", async () => {
      const user = userEvent.setup();
      renderEditor();
      await user.click(screen.getByRole("button", { name: /sort descending: ticker/i }));
      await waitFor(() =>
        expect(reorderHoldings).toHaveBeenCalledWith([MSFT.id, AAPL.id]),
      );
    });

    it("sorting by broker uses ticker as the secondary key", async () => {
      const user = userEvent.setup();
      const first = holding({
        id: "44444444-4444-4444-4444-444444444444",
        name: "Zeta Corp",
        ticker: "ZETA",
        broker: "Fidelity",
      });
      const second = holding({
        id: "55555555-5555-5555-5555-555555555555",
        name: "Alpha Corp",
        ticker: "ALPHA",
        broker: "Fidelity",
      });
      reorderHoldings.mockImplementation(async (ids: string[]) =>
        ids.map((id) => [first, second].find((h) => h.id === id)!),
      );
      renderEditor([first, second]);
      await user.click(screen.getByRole("button", { name: /sort ascending: broker/i }));
      await waitFor(() =>
        expect(reorderHoldings).toHaveBeenCalledWith([second.id, first.id]),
      );
    });
  });
});
