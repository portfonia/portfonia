import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
const { useSession } = vi.hoisted(() => ({ useSession: vi.fn() }));
vi.mock("@/hooks/use-session", () => ({ useSession, markOptimisticLogout: vi.fn(), revalidateSession: vi.fn() }));
vi.mock("@/hooks/use-idle-logout", () => ({ useIdleLogout: vi.fn() }));
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));
import { NextIntlClientProvider } from "next-intl";
import { catalogs } from "@/locales";
import { GetStartedMenu } from "./get-started-menu";
beforeEach(() => { vi.clearAllMocks(); });
it.each(["en", "zh-Hans", "zh-Hant"] as const)("A8 authenticated Jade entry in %s", async (locale) => {
  useSession.mockReturnValue({ status: "authed", email: "jade@example.com", advanced: true, jade: true });
  render(<NextIntlClientProvider locale={locale} messages={catalogs[locale]}><GetStartedMenu /></NextIntlClientProvider>);
  const trigger = screen.getByRole("button");
  expect(trigger).toHaveClass("jade-surface"); expect(trigger).not.toHaveClass("bg-advanced");
  await userEvent.click(trigger);
  const link = await screen.findByRole("menuitem", { name: catalogs[locale].menu.jade });
  expect(link).toHaveAttribute("href", "/jade");
  const entries = screen.getAllByRole("menuitem");
  expect(entries.indexOf(link)).toBe(entries.findIndex(el => el.getAttribute("href") === "/portfolio/performance") + 1);
});
it("A8 guest has no Jade entry", async () => {
  useSession.mockReturnValue({ status: "guest" });
  render(<NextIntlClientProvider locale="en" messages={catalogs.en}><GetStartedMenu /></NextIntlClientProvider>);
  await userEvent.click(screen.getByRole("button"));
  expect(screen.queryByRole("menuitem", { name: "Jade" })).not.toBeInTheDocument();
});
it("D10.9 Daily retains gold trigger", () => {
  useSession.mockReturnValue({ status: "authed", email: "daily@example.com", advanced: true, jade: false });
  render(<NextIntlClientProvider locale="en" messages={catalogs.en}><GetStartedMenu /></NextIntlClientProvider>);
  expect(screen.getByRole("button")).toHaveClass("bg-advanced");
  expect(screen.getByRole("button")).not.toHaveClass("jade-surface");
});
it.each(["light", "dark"])("375px %s Jade trigger retains texture and flat fallback", theme => {
  vi.stubGlobal("innerWidth", 375);
  document.documentElement.classList.toggle("dark", theme === "dark");
  useSession.mockReturnValue({ status: "authed", email: "jade@example.com", advanced: true, jade: true });
  render(<div style={{ width: 375 }}><NextIntlClientProvider locale="en" messages={catalogs.en}><GetStartedMenu /></NextIntlClientProvider></div>);
  expect(window.innerWidth).toBe(375);
  expect(screen.getByRole("button")).toHaveClass("jade-surface");
  expect(screen.getByRole("button")).not.toHaveClass("bg-advanced");
  document.documentElement.classList.remove("dark");
  vi.unstubAllGlobals();
});
it("authenticated Jade entry uses a jade disc drawn as one thick ring", async () => {
  useSession.mockReturnValue({ status: "authed", email: "jade@example.com", advanced: true, jade: true });
  render(<NextIntlClientProvider locale="en" messages={catalogs.en}><GetStartedMenu /></NextIntlClientProvider>);
  await userEvent.click(screen.getByRole("button"));
  const entry = await screen.findByRole("menuitem", { name: "Jade" });
  const svg = entry.querySelector("svg");
  expect(svg).toBeInTheDocument();
  expect(svg).not.toHaveClass("lucide-gem");
  const circles = entry.querySelectorAll("svg circle");
  expect(circles).toHaveLength(1);
  expect(Array.from(circles, circle => [
    circle.getAttribute("cx"), circle.getAttribute("cy"), circle.getAttribute("r"),
    circle.getAttribute("stroke-width"),
  ])).toEqual([["12", "12", "7.5", "5"]]);
});
