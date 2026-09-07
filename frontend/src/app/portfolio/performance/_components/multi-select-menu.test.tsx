import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { LocaleProvider } from "@/app/_components/locale-provider";
import { MultiSelectMenu } from "./multi-select-menu";

const OPTIONS = [
  { value: "US", label: "US" },
  { value: "HK", label: "HK" },
  { value: "A-Share", label: "A-Share" },
];

function renderMenu(
  selected: string[] = [],
  onChange = vi.fn(),
  disabled = false,
  allMode: "none" | "all-options" = "none",
) {
  render(
    <LocaleProvider>
      <MultiSelectMenu
        label="Markets"
        options={OPTIONS}
        selected={selected}
        onChange={onChange}
        disabled={disabled}
        allMode={allMode}
      />
    </LocaleProvider>,
  );
  return onChange;
}

async function openMenu(user: ReturnType<typeof userEvent.setup>, label: string) {
  await user.click(screen.getByRole("button", { name: new RegExp(label) }));
  await waitFor(() => expect(screen.getByRole("menu")).toBeInTheDocument());
}

describe("MultiSelectMenu", () => {
  it("shows All when nothing is selected and the count once items are", async () => {
    const user = userEvent.setup();
    const onChange = renderMenu();

    expect(screen.getByRole("button", { name: /Markets/ })).toHaveTextContent("All");
    await openMenu(user, "Markets");

    const all = screen.getByRole("menuitemcheckbox", { name: "All" });
    expect(all).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("menuitemcheckbox", { name: "US" })).toHaveAttribute(
      "aria-checked",
      "false",
    );

    await user.click(screen.getByRole("menuitemcheckbox", { name: "US" }));
    expect(onChange).toHaveBeenCalledWith(["US"]);
  });

  it("toggling an already-selected option removes it", async () => {
    const user = userEvent.setup();
    const onChange = renderMenu(["US", "HK"]);

    expect(screen.getByRole("button", { name: /Markets/ })).toHaveTextContent("2 selected");
    await openMenu(user, "Markets");

    expect(screen.getByRole("menuitemcheckbox", { name: "US" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    await user.click(screen.getByRole("menuitemcheckbox", { name: "US" }));
    expect(onChange).toHaveBeenCalledWith(["HK"]);
  });

  it("clicking All while some are selected clears the selection", async () => {
    const user = userEvent.setup();
    const onChange = renderMenu(["US"]);

    await openMenu(user, "Markets");
    expect(screen.getByRole("menuitemcheckbox", { name: "All" })).toHaveAttribute(
      "aria-checked",
      "false",
    );

    await user.click(screen.getByRole("menuitemcheckbox", { name: "All" }));
    expect(onChange).toHaveBeenCalledWith([]);
  });

  it("menu stays open after an item click so multiple options can be toggled", async () => {
    const user = userEvent.setup();
    const onChange = renderMenu();

    await openMenu(user, "Markets");
    await user.click(screen.getByRole("menuitemcheckbox", { name: "US" }));
    await user.click(screen.getByRole("menuitemcheckbox", { name: "HK" }));

    expect(onChange).toHaveBeenNthCalledWith(1, ["US"]);
    expect(onChange).toHaveBeenNthCalledWith(2, ["HK"]);
    expect(screen.getByRole("menu")).toBeInTheDocument();
  });

  it("all-options mode: All means every option selected, never empty (review finding 1)", async () => {
    const user = userEvent.setup();
    const onChange = renderMenu(["US"], vi.fn(), false, "all-options");

    // Partial selection is NOT All in this mode.
    expect(screen.getByRole("button", { name: /Markets/ })).toHaveTextContent("1 selected");
    await openMenu(user, "Markets");
    expect(screen.getByRole("menuitemcheckbox", { name: "All" })).toHaveAttribute(
      "aria-checked",
      "false",
    );

    await user.click(screen.getByRole("menuitemcheckbox", { name: "All" }));

    expect(onChange).toHaveBeenCalledWith(["US", "HK", "A-Share"]);
  });

  it("all-options mode: reports All only when every option is selected", async () => {
    const user = userEvent.setup();
    const onChange = renderMenu(["US", "HK", "A-Share"], vi.fn(), false, "all-options");

    expect(screen.getByRole("button", { name: /Markets/ })).toHaveTextContent("All");
    await openMenu(user, "Markets");
    expect(screen.getByRole("menuitemcheckbox", { name: "All" })).toHaveAttribute(
      "aria-checked",
      "true",
    );

    // Clicking an already-active All row is a no-op (All is applied through
    // this row, never toggled off to an empty "All").
    await user.click(screen.getByRole("menuitemcheckbox", { name: "All" }));
    expect(onChange).not.toHaveBeenCalled();
  });

  it("none mode: clicking All while empty is a no-op (dimension semantics)", async () => {
    const user = userEvent.setup();
    const onChange = renderMenu([]);

    await openMenu(user, "Markets");
    expect(screen.getByRole("menuitemcheckbox", { name: "All" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    await user.click(screen.getByRole("menuitemcheckbox", { name: "All" }));
    expect(onChange).not.toHaveBeenCalled();
  });
});
