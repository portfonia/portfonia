import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { LocaleProvider } from "@/app/_components/locale-provider";
import { LocaleSwitcher } from "./locale-switcher";

function renderSwitcher() {
  return render(
    <LocaleProvider>
      <LocaleSwitcher />
    </LocaleProvider>,
  );
}

describe("LocaleSwitcher", () => {
  it("renders a button trigger (not a native select) carrying the current flag", async () => {
    renderSwitcher();

    const trigger = await screen.findByRole("button", { name: /language/i });
    expect(trigger.querySelector(".fi-us")).toBeInTheDocument();
  });

  it("opens a menu offering all three locales with their own flags", async () => {
    const user = userEvent.setup();
    renderSwitcher();

    await user.click(await screen.findByRole("button", { name: /language/i }));

    await waitFor(() => expect(screen.getByRole("menu")).toBeInTheDocument());
    const en = screen.getByRole("menuitem", { name: "English" });
    const zhHans = screen.getByRole("menuitem", { name: "简体中文" });
    const zhHant = screen.getByRole("menuitem", { name: "繁體中文" });
    expect(en.querySelector(".fi-us")).toBeInTheDocument();
    expect(zhHans.querySelector(".fi-cn")).toBeInTheDocument();
    // Issue #350 item 4: Traditional Chinese -> Taiwan flag, an explicit
    // product-owner choice (not Hong Kong or a generic "CN" flag).
    expect(zhHant.querySelector(".fi-tw")).toBeInTheDocument();
  });

  it("switches locale on click, updating the trigger's flag to the new selection", async () => {
    // The trigger's own accessible name is itself locale-dependent (it
    // renders tMenu("language"), which becomes "语言" once zh-Hans is
    // selected) — query by role alone (this component renders exactly one
    // button) rather than an English-only name regex.
    const user = userEvent.setup();
    renderSwitcher();

    await user.click(await screen.findByRole("button"));
    await waitFor(() => expect(screen.getByRole("menu")).toBeInTheDocument());
    await user.click(screen.getByRole("menuitem", { name: "简体中文" }));

    await waitFor(() =>
      expect(screen.getByRole("button").querySelector(".fi-cn")).toBeInTheDocument(),
    );
  });
});
