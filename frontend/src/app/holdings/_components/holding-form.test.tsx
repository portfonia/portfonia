import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { createHolding, updateHolding, push } = vi.hoisted(() => ({
  createHolding: vi.fn(),
  updateHolding: vi.fn(),
  push: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, createHolding, updateHolding };
});
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));

import { LocaleProvider } from "@/app/_components/locale-provider";
import type { HoldingOut } from "@/lib/api";
import { HoldingForm } from "./holding-form";

const EXISTING: HoldingOut = {
  id: "11111111-1111-1111-1111-111111111111",
  name: "Apple Inc.",
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
};

function renderForm(initial?: HoldingOut) {
  return render(
    <LocaleProvider>
      <HoldingForm initial={initial} />
    </LocaleProvider>,
  );
}

describe("HoldingForm", () => {
  beforeEach(() => {
    push.mockClear();
    createHolding.mockReset();
    updateHolding.mockReset();
    createHolding.mockResolvedValue(EXISTING);
    updateHolding.mockResolvedValue(EXISTING);
  });

  it("creates a holding without calling the parser and returns to the edit list", async () => {
    const user = userEvent.setup();
    renderForm();
    await user.type(screen.getByLabelText(/^name$/i), "Apple Inc.");
    await user.click(screen.getByRole("button", { name: /save holding/i }));
    await waitFor(() => expect(createHolding).toHaveBeenCalled());
    const payload = createHolding.mock.calls[0][0] as { ticker: string | null; issues: unknown };
    expect(payload.issues).toEqual([]);
    expect(updateHolding).not.toHaveBeenCalled();
    expect(push).toHaveBeenCalledWith("/holdings/edit");
  });

  it("disables the merged ticker/fund code field for cash and requires current value", async () => {
    const user = userEvent.setup();
    renderForm();
    await user.selectOptions(screen.getByLabelText(/^type$/i), "cash");
    expect(screen.getByLabelText(/^ticker$/i)).toBeDisabled();
    expect(screen.getByLabelText(/^current value$/i)).toBeRequired();
    expect(screen.queryByLabelText(/^shares$/i)).not.toBeInTheDocument();
  });

  it("merges ticker/fund code into one field that switches label and target with asset type (issue #319 item 7)", async () => {
    const user = userEvent.setup();
    renderForm();
    await user.type(screen.getByLabelText(/^name$/i), "E Fund Blue Chip");

    expect(screen.getByLabelText(/^ticker$/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/^fund code$/i)).not.toBeInTheDocument();
    await user.type(screen.getByLabelText(/^ticker$/i), "AAPL");

    await user.selectOptions(screen.getByLabelText(/^type$/i), "fund");
    expect(screen.queryByLabelText(/^ticker$/i)).not.toBeInTheDocument();
    const fundField = screen.getByLabelText(/^fund code$/i);
    // Switching asset_type clears the other underlying field so a value
    // typed under the previous type never survives hidden.
    expect(fundField).toHaveValue("");
    await user.type(fundField, "110011");
    await user.click(screen.getByRole("button", { name: /save holding/i }));

    await waitFor(() => expect(createHolding).toHaveBeenCalled());
    const payload = createHolding.mock.calls[0][0] as {
      ticker: string | null;
      fund_code: string | null;
    };
    expect(payload.ticker).toBeNull();
    expect(payload.fund_code).toBe("110011");
  });

  it("prompts before discarding a dirty form", async () => {
    const user = userEvent.setup();
    renderForm(EXISTING);
    await user.clear(screen.getByLabelText(/^name$/i));
    await user.type(screen.getByLabelText(/^name$/i), "Renamed");
    await user.click(screen.getByRole("button", { name: /back to edit list/i }));
    expect(push).not.toHaveBeenCalled();
    expect(screen.getByText(/discard unsaved changes/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^discard$/i }));
    expect(push).toHaveBeenCalledWith("/holdings/edit");
  });

  it("patches an existing holding on save", async () => {
    const user = userEvent.setup();
    renderForm(EXISTING);
    await user.click(screen.getByRole("button", { name: /save holding/i }));
    await waitFor(() => expect(updateHolding).toHaveBeenCalledWith(EXISTING.id, expect.any(Object)));
    expect(createHolding).not.toHaveBeenCalled();
  });

  it("labels the empty market option auto-detect, distinct from Other", () => {
    renderForm();
    const select = screen.getByLabelText(/^market/i);
    const options = [...select.querySelectorAll("option")].map((o) => ({
      value: (o as HTMLOptionElement).value,
      label: o.textContent,
    }));
    const empty = options.find((o) => o.value === "");
    const other = options.find((o) => o.value === "Other");
    expect(empty?.label).toMatch(/auto-detect from ticker/i);
    expect(other?.label).toMatch(/^other$/i);
    expect(empty?.label).not.toEqual(other?.label);
  });
});
