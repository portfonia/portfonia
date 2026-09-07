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
) {
  render(
    <LocaleProvider>
      <MultiSelectMenu
        label="Markets"
        options={OPTIONS}
        selected={selected}
        onChange={onChange}
        disabled={disabled}
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
});
