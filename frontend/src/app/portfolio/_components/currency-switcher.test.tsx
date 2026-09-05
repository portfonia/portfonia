import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { LocaleProvider } from "@/app/_components/locale-provider";
import { CurrencySwitcher } from "./currency-switcher";

function renderSwitcher(onChange = vi.fn()) {
  render(
    <LocaleProvider>
      <CurrencySwitcher value="USD" onChange={onChange} />
    </LocaleProvider>,
  );
  return onChange;
}

describe("CurrencySwitcher", () => {
  it("lists all 7 display currencies, not just the 3 selectable ones (issue #354)", async () => {
    const user = userEvent.setup();
    renderSwitcher();

    await user.click(screen.getByRole("button", { name: /base currency/i }));
    await waitFor(() => expect(screen.getByRole("menu")).toBeInTheDocument());

    for (const code of ["USD", "CNY", "CNH", "GBP", "HKD", "TWD", "EUR"]) {
      expect(screen.getByRole("menuitem", { name: code })).toBeInTheDocument();
    }
  });

  it("USD, CNY, and HKD are enabled; the other 4 are disabled, not hidden", async () => {
    const user = userEvent.setup();
    renderSwitcher();

    await user.click(screen.getByRole("button", { name: /base currency/i }));
    await waitFor(() => expect(screen.getByRole("menu")).toBeInTheDocument());

    for (const code of ["USD", "CNY", "HKD"]) {
      expect(screen.getByRole("menuitem", { name: code })).not.toHaveAttribute(
        "aria-disabled",
        "true",
      );
    }
    for (const code of ["CNH", "GBP", "TWD", "EUR"]) {
      expect(screen.getByRole("menuitem", { name: code })).toHaveAttribute(
        "aria-disabled",
        "true",
      );
    }
  });

  it("clicking a disabled currency does not call onChange", async () => {
    const user = userEvent.setup();
    const onChange = renderSwitcher();

    await user.click(screen.getByRole("button", { name: /base currency/i }));
    await waitFor(() => expect(screen.getByRole("menu")).toBeInTheDocument());
    await user.click(screen.getByRole("menuitem", { name: "GBP" }));

    expect(onChange).not.toHaveBeenCalled();
  });

  it("clicking an enabled currency calls onChange with that currency", async () => {
    const user = userEvent.setup();
    const onChange = renderSwitcher();

    await user.click(screen.getByRole("button", { name: /base currency/i }));
    await waitFor(() => expect(screen.getByRole("menu")).toBeInTheDocument());
    await user.click(screen.getByRole("menuitem", { name: "CNY" }));

    expect(onChange).toHaveBeenCalledWith("CNY");
  });

  it("HKD is also selectable (issue #354 correction: 3 normalization targets, not 2)", async () => {
    const user = userEvent.setup();
    const onChange = renderSwitcher();

    await user.click(screen.getByRole("button", { name: /base currency/i }));
    await waitFor(() => expect(screen.getByRole("menu")).toBeInTheDocument());
    await user.click(screen.getByRole("menuitem", { name: "HKD" }));

    expect(onChange).toHaveBeenCalledWith("HKD");
  });

  it("renders a grayscale flag on the disabled currencies", async () => {
    const user = userEvent.setup();
    renderSwitcher();

    await user.click(screen.getByRole("button", { name: /base currency/i }));
    await waitFor(() => expect(screen.getByRole("menu")).toBeInTheDocument());

    const gbpItem = screen.getByRole("menuitem", { name: "GBP" });
    expect(gbpItem.querySelector(".fi-gb")).toHaveClass("grayscale");
    const cnyItem = screen.getByRole("menuitem", { name: "CNY" });
    expect(cnyItem.querySelector(".fi-cn")).not.toHaveClass("grayscale");
  });

  it("EUR uses the flag-icons EU flag, not a member state's flag", async () => {
    const user = userEvent.setup();
    renderSwitcher();

    await user.click(screen.getByRole("button", { name: /base currency/i }));
    await waitFor(() => expect(screen.getByRole("menu")).toBeInTheDocument());

    expect(
      screen.getByRole("menuitem", { name: "EUR" }).querySelector(".fi-eu"),
    ).toBeInTheDocument();
  });
});
