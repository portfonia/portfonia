import { render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createTranslator } from "next-intl";
import { ApiError } from "@/lib/api";
import { NextIntlClientProvider } from "next-intl";
import { expect, it, vi } from "vitest";
import en from "@/locales/en.json";

const { listHoldingChanges, previewHoldingRollback, rollbackHoldings } = vi.hoisted(() => ({ listHoldingChanges: vi.fn(), previewHoldingRollback: vi.fn(), rollbackHoldings: vi.fn() }));
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));
vi.mock("@/lib/api", async importOriginal => ({ ...await importOriginal<typeof import("@/lib/api")>(), listHoldingChanges, previewHoldingRollback, rollbackHoldings }));
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

it("disables blocked rollback, confirms preview counts and reloads on version conflict", async () => {
  const user = userEvent.setup();
  const group = { id: "allowed", changed_at: "2026-10-15T18:00:00-04:00", source: "web", token: null, rollback_to: null, rollback_available: true, rows: [] };
  const data = { start: "2026-10-01", end: "2026-10-15", truncated: false, items: [group, { ...group, id: "blocked", rollback_available: false }] };
  listHoldingChanges.mockReset().mockResolvedValue(data);
  previewHoldingRollback.mockResolvedValue({ target_changed_at: group.changed_at, groups: 3, restored: 1, removed: 2, modified: 4, version: "42" });
  rollbackHoldings.mockRejectedValue(new ApiError(409, "version_conflict"));
  const t = createTranslator({ locale: "en", messages: en, namespace: "holdingChanges" });
  render(<NextIntlClientProvider locale="en" messages={en}><ChangesPage /></NextIntlClientProvider>);
  const buttons = await screen.findAllByRole("button", { name: t("rollbackButton") });
  expect(buttons[1]).toBeDisabled();
  await user.click(buttons[0]);
  expect(previewHoldingRollback).toHaveBeenCalledWith("allowed");
  const dialog = await screen.findByRole("alertdialog");
  const time = new Intl.DateTimeFormat("en", { timeZone: "America/New_York", dateStyle: "medium", timeStyle: "long" }).format(new Date(group.changed_at));
  expect(within(dialog).getByText(t("rollbackSummary", { groups: 3, time, restored: 1, removed: 2, modified: 4 }))).toBeInTheDocument();
  await user.click(within(dialog).getByRole("button", { name: t("rollbackConfirm") }));
  await waitFor(() => expect(rollbackHoldings).toHaveBeenCalledWith("allowed", "42"));
  await waitFor(() => expect(listHoldingChanges).toHaveBeenCalledTimes(2));
  expect(screen.getByRole("alert")).toHaveTextContent(t("versionConflict"));
});
