import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { expect, it, vi } from "vitest";
import en from "@/locales/en.json";

const { listHoldingChanges } = vi.hoisted(() => ({ listHoldingChanges: vi.fn() }));
vi.mock("@/lib/api", () => ({ listHoldingChanges }));
import ChangesPage from "./changes/page";
import { HoldingsHeading } from "./_components/holdings-heading";

it("links to history and renders groups, empty results and truncation", async () => {
  listHoldingChanges.mockResolvedValueOnce({ start: "2026-10-01", end: "2026-10-15", truncated: true, items: [{ id: "group", changed_at: "2026-10-15T18:00:00-04:00", source: "agent", token: { name: "sync", prefix: "pfa_AbCdEfGh" }, rows: [{ holding_id: "apple", holding_name: "Apple", kind: "updated", fields: [{ field: "shares", before: "10", after: "12" }] }] }] });
  const view = render(<NextIntlClientProvider locale="en" messages={en}><HoldingsHeading /><ChangesPage /></NextIntlClientProvider>);
  expect(screen.getByRole("link", { name: "Change history" })).toHaveAttribute("href", "/holdings/changes");
  expect(await screen.findByText("Apple")).toBeInTheDocument();
  expect(screen.getByText(/sync/)).toHaveTextContent("pfa_AbCdEfGh");
  expect(screen.getByText("Updated")).toBeInTheDocument();
  expect(screen.getByText("Shares")).toBeInTheDocument();
  expect(screen.getByText("10")).toBeInTheDocument();
  expect(screen.getByText("12")).toBeInTheDocument();
  expect(screen.getByText(/narrow the date range/i)).toBeInTheDocument();
  view.unmount();
  listHoldingChanges.mockResolvedValueOnce({ start: "2026-10-01", end: "2026-10-15", truncated: false, items: [] });
  render(<NextIntlClientProvider locale="en" messages={en}><ChangesPage /></NextIntlClientProvider>);
  expect(await screen.findByText("No changes in this range")).toBeInTheDocument();
});
