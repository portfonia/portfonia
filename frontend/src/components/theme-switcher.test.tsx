import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { NextIntlClientProvider } from "next-intl";
import { catalogs } from "@/locales";
import { ThemeSwitcher } from "./theme-switcher";

it("856 A5 hides the desktop trigger on phones and checks the current preference", async () => {
  const onSelect = vi.fn();
  render(<NextIntlClientProvider locale="en" messages={catalogs.en} timeZone="America/New_York"><ThemeSwitcher preference="light" onSelect={onSelect} /></NextIntlClientProvider>);
  const trigger = screen.getByRole("button");
  expect(trigger).toHaveClass("hidden", "sm:inline-flex");
  await userEvent.setup().click(trigger);
  const rows = await screen.findAllByRole("menuitemcheckbox");
  expect(rows.map(row => row.getAttribute("aria-checked"))).toEqual(["false", "true", "false"]);
  fireEvent.click(rows[1]);
  expect(onSelect).toHaveBeenLastCalledWith("light");
  fireEvent.click(rows[0]);
  expect(onSelect).toHaveBeenLastCalledWith("dark");
  fireEvent.click(rows[2]);
  expect(onSelect).toHaveBeenLastCalledWith("system");
});
