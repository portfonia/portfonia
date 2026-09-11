import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { updateHolding } = vi.hoisted(() => ({
  updateHolding: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, updateHolding };
});
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));

import { LocaleProvider } from "@/app/_components/locale-provider";
import type { HoldingOut } from "@/lib/api";
import { HoldingsGroupsEditor } from "./holdings-groups-editor";

function holding(partial: Partial<HoldingOut> & Pick<HoldingOut, "id" | "name">): HoldingOut {
  return {
    ticker: "AAPL",
    fund_code: null,
    currency: "USD",
    shares: "10",
    avg_cost: "150",
    current_value: "3000",
    pricing_mode: "auto",
    asset_type: "stock",
    capture_supported: true,
    broker: "Fidelity",
    account: null,
    portfolio: null,
    notes: null,
    watch_tier: null,
    last_manual_update: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    position: 0,
    ...partial,
  };
}

const AAPL = holding({
  id: "11111111-1111-1111-1111-111111111111",
  name: "Apple Inc.",
  portfolio: "Retirement",
  account: "Main",
});
const MSFT = holding({
  id: "22222222-2222-2222-2222-222222222222",
  name: "Microsoft",
  ticker: "MSFT",
});

function renderEditor(holdings: HoldingOut[] = [AAPL, MSFT]) {
  return render(
    <LocaleProvider>
      <HoldingsGroupsEditor initialHoldings={holdings} />
    </LocaleProvider>,
  );
}

describe("HoldingsGroupsEditor", () => {
  beforeEach(() => {
    updateHolding.mockReset();
  });

  it("lists every holding's name, ticker, broker, currency, and current value read-only", () => {
    renderEditor();
    expect(screen.getByText("Apple Inc.")).toBeInTheDocument();
    expect(screen.getByText("AAPL")).toBeInTheDocument();
    expect(screen.getAllByText("Fidelity").length).toBeGreaterThan(0);
    expect(screen.getAllByText("3000").length).toBeGreaterThan(0);
  });

  it("edits the group on blur", async () => {
    const user = userEvent.setup();
    updateHolding.mockResolvedValue({ ...AAPL, portfolio: "Growth" });
    renderEditor();

    const input = screen.getByDisplayValue("Retirement");
    await user.clear(input);
    await user.type(input, "Growth");
    await user.tab();

    await waitFor(() =>
      expect(updateHolding).toHaveBeenCalledWith(AAPL.id, { portfolio: "Growth" }),
    );
  });

  it("clearing a group sends null, not an empty string", async () => {
    const user = userEvent.setup();
    updateHolding.mockResolvedValue({ ...AAPL, portfolio: null });
    renderEditor();

    const input = screen.getByDisplayValue("Retirement");
    await user.clear(input);
    await user.tab();

    await waitFor(() =>
      expect(updateHolding).toHaveBeenCalledWith(AAPL.id, { portfolio: null }),
    );
  });

  it("edits the account on blur independently of group", async () => {
    const user = userEvent.setup();
    updateHolding.mockResolvedValue({ ...AAPL, account: "Joint" });
    renderEditor();

    const input = screen.getByDisplayValue("Main");
    await user.clear(input);
    await user.type(input, "Joint");
    await user.tab();

    await waitFor(() =>
      expect(updateHolding).toHaveBeenCalledWith(AAPL.id, { account: "Joint" }),
    );
  });

  it("rolls back the group value if the save fails", async () => {
    const user = userEvent.setup();
    updateHolding.mockRejectedValue(new Error("network down"));
    renderEditor();

    const input = screen.getByDisplayValue("Retirement");
    await user.clear(input);
    await user.type(input, "Growth");
    await user.tab();

    await waitFor(() => expect(screen.getByDisplayValue("Retirement")).toBeInTheDocument());
    expect(screen.getByText(/network down/)).toBeInTheDocument();
  });

  it("offers existing group/account values as suggestions, not a hardcoded list", () => {
    renderEditor();
    const groupInput = screen.getByDisplayValue("Retirement");
    const listId = groupInput.getAttribute("list");
    expect(listId).toBeTruthy();
    const datalist = document.getElementById(listId!);
    expect(datalist?.querySelector('option[value="Retirement"]')).not.toBeNull();
  });
});
