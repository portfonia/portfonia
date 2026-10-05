import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { useSyncExternalStore } from "react";
const { listReports, listeners } = vi.hoisted(() => ({ listReports: vi.fn(), listeners: new Set<() => void>() }));
vi.mock("@/lib/api", () => ({ listReports }));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(useSyncExternalStore((cb) => { listeners.add(cb); return () => listeners.delete(cb); }, () => window.location.search)),
  usePathname: () => "/reports",
  useRouter: () => ({ push: (url: string) => { window.history.pushState(null, "", url); listeners.forEach(cb => cb()); } }),
}));
import { LocaleProvider } from "@/app/_components/locale-provider";
import { ReportsList } from "./reports-list";
const items = Array.from({ length: 20 }, (_, i) => ({ id: `r${i}`, kind: "report" as const, report_date: "2026-10-03", report_type: "incremental", session_node: i === 0 ? "after_close" : i === 1 ? "weekend_snapshot" : "manual", status: i === 18 ? "needs_review" : i === 19 ? "in_progress" : "success", display_state: i === 18 ? "under_review" as const : i === 19 ? "generating" as const : "available" as const, generated_at: null, created_at: "2026-10-03T00:00:00Z" }));
const page = { items, page: 1, page_size: 20, total: 25, kinds: ["report" as const] };
function mount(value = page, initialLoadError = false) { return render(<LocaleProvider><ReportsList initialPage={value} initialLoadError={initialLoadError} /></LocaleProvider>); }
beforeEach(() => { window.history.replaceState(null, "", "/reports"); vi.clearAllMocks(); listReports.mockResolvedValue(page); });
it("acceptance_14 renders rows, paging and bodyless badges", () => {
  mount();
  expect(screen.getByText("Page 1 of 2")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Next" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
  expect(screen.getAllByTestId("report-row")).toHaveLength(20);
  expect(screen.getAllByRole("link")).toHaveLength(18);
  for (const label of ["Under review", "Generating"]) { const badge = screen.getByText(label); expect(badge.closest("a")).toBeNull(); }
  expect(screen.getByText("Mon/Wed/Fri")).toBeInTheDocument();
  expect(screen.getByText("Weekly")).toBeInTheDocument();
});
it("acceptance_15 updates URL and request for sort and dates", async () => {
  const user = userEvent.setup(); mount();
  await user.selectOptions(screen.getByRole("combobox", { name: "Sort" }), "asc");
  await waitFor(() => expect(listReports).toHaveBeenLastCalledWith(expect.objectContaining({ sort: "asc", page: "1", kind: "report" })));
  expect(new URLSearchParams(window.location.search).get("sort")).toBe("asc");
  const from = screen.getByLabelText("From (ET)");
  const to = screen.getByLabelText("To (ET)");
  const { fireEvent } = await import("@testing-library/react");
  fireEvent.change(from, { target: { value: "2026-10-01" } });
  fireEvent.change(to, { target: { value: "2026-10-03" } });
  await waitFor(() => expect(listReports).toHaveBeenLastCalledWith(expect.objectContaining({ date_from: "2026-10-01", date_to: "2026-10-03", sort: "asc" })));
  expect(new URLSearchParams(window.location.search).get("date_from")).toBe("2026-10-01");
  expect(new URLSearchParams(window.location.search).get("date_to")).toBe("2026-10-03");
});
it("acceptance_16 has exactly one selected category", () => {
  mount(); const select = screen.getByRole("combobox", { name: "Category" });
  expect(within(select).getAllByRole("option")).toHaveLength(1);
  expect(select).toHaveValue("report");
  expect(within(select).getByRole("option")).toHaveTextContent("Reports");
});
it("acceptance_17 renders empty states", async () => {
  const empty = { ...page, items: [], total: 0 }; const view = mount(empty);
  expect(screen.getByText("No reports yet")).toBeInTheDocument(); view.unmount();
  window.history.replaceState(null, "", "/reports?date_from=2026-10-01"); listReports.mockResolvedValue(empty); mount(empty);
  expect(await screen.findByText("No reports in this range")).toBeInTheDocument();
});
it("restores query on refresh and Back", async () => {
  window.history.replaceState(null, "", "/reports?page=2&sort=asc&kind=report");
  listReports.mockResolvedValue({ ...page, page: 2, items: items.slice(0, 5) }); mount();
  await waitFor(() => expect(listReports).toHaveBeenCalledWith({ page: "2", sort: "asc", kind: "report" }));
  expect(await screen.findByText("Page 2 of 2")).toBeInTheDocument();
  window.history.replaceState(null, "", "/reports");
  listReports.mockResolvedValue(page);
  const { act } = await import("@testing-library/react"); act(() => listeners.forEach(cb => cb()));
  expect(await screen.findByText("Page 1 of 2")).toBeInTheDocument();
});
it("shows a translated load error", () => { mount(page, true); expect(screen.getByRole("alert")).toHaveTextContent("Couldn't load your reports. Try refreshing the page."); });

it("daily_acceptance_12 history labels daily_close as Daily", () => {
  mount({ ...page, items: [{ ...items[0], session_node: "daily_close" }], total: 1 });
  expect(screen.getByText("Daily")).toBeInTheDocument();
});


it("polish_660_acceptance_6 aligns page typography", () => {
  mount();
  const heading = screen.getByRole("heading", { level: 1 });
  expect(heading).toHaveClass("font-heading", "text-2xl", "font-medium");
  expect(heading).not.toHaveClass("font-serif");
  expect(heading).not.toHaveClass("font-semibold");
  expect(screen.getByText("Page 1 of 2").closest(".text-sm")).not.toBeNull();
  expect(screen.getByLabelText("Sort").parentElement).toHaveClass("text-muted-foreground");
});
