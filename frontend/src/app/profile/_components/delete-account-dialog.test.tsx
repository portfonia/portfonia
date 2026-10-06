import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { LocaleProvider } from "@/app/_components/locale-provider";
import type { Me } from "@/lib/api";
import { ProfilePageBody } from "./profile-page-body";

const { signOut, replace } = vi.hoisted(() => ({ signOut: vi.fn(), replace: vi.fn() }));
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));
vi.mock("@/lib/supabase/browser", () => ({ createClient: () => ({ auth: { signOut } }) }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), replace }), usePathname: () => "/profile" }));
vi.mock("next/script", () => ({ default: () => null }));
const me: Me = {
  email: "user@example.com", credit_balance: "5.00", delivery_email: null,
  email_verified_at: null, delivery_email_verified_at: null, tos_accepted_at: null,
  has_questionnaire: true, has_holdings: true, missing: [], pending_email_verifications: [],
  report_language: "en", report_currency: "USD",
  subscription: { status: "inactive", type: null, expires_on: null, cancel_pending: false, next_adjustment_at: null },
};
const fetchMock = vi.fn<typeof fetch>();
let summary = { cash_balance: "0.00", refundable_cash: "0.00", gift_balance: "3.01", subscription_active: true };
let result = new Response(null, { status: 204 });
beforeEach(() => {
  signOut.mockReset();
  signOut.mockResolvedValue({ error: null });
  replace.mockReset();
  summary = { cash_balance: "0.00", refundable_cash: "0.00", gift_balance: "3.01", subscription_active: true };
  result = new Response(null, { status: 204 });
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (_url, options) => options?.method === "POST"
    ? result : Response.json(summary));
  vi.stubGlobal("fetch", fetchMock);
});
function renderProfile() { render(<LocaleProvider><ProfilePageBody me={me} hadLoadError={false} /></LocaleProvider>); }
async function open() {
  const trigger = screen.getByRole("button", { name: "Delete account" });
  expect(trigger).toBeEnabled();
  await userEvent.click(trigger);
  return screen.findByRole("alertdialog");
}
async function continueToCash() {
  summary.cash_balance = "12.00";
  const first = await open();
  await userEvent.type(within(first).getByRole("textbox"), "USER@example.com");
  await userEvent.click(within(first).getByRole("button", { name: "Continue" }));
  return screen.findByRole("alertdialog");
}
function solve() {
  const widget = document.querySelector("altcha-widget");
  if (!widget) throw new Error("missing account deletion Altcha widget");
  expect(widget).toHaveAttribute("challengeurl", "/api/me/account-deletion/altcha-challenge");
  fireEvent(widget, new CustomEvent("statechange", { detail: { state: "verified", payload: "solved" } }));
}
it("12 cash zero submits only the first dialog and logs out on 204", async () => {
  renderProfile();
  const dialog = await open();
  const confirm = within(dialog).getByRole("button", { name: "Delete account" });
  expect(confirm).toBeDisabled();
  await userEvent.type(within(dialog).getByRole("textbox"), "wrong@example.com");
  expect(confirm).toBeDisabled();
  await userEvent.clear(within(dialog).getByRole("textbox"));
  await userEvent.type(within(dialog).getByRole("textbox"), " USER@example.com ");
  await userEvent.click(confirm);
  await waitFor(() => expect(replace).toHaveBeenCalledExactlyOnceWith("/"));
  expect(signOut).toHaveBeenCalledExactlyOnceWith({ scope: "local" });
  expect(screen.queryByRole("alert")).toBeNull();
  expect(fetchMock).toHaveBeenCalledWith("/api/me/account-deletion", expect.objectContaining({
    method: "POST", body: JSON.stringify({ confirm_email: " USER@example.com ", relinquish_cash: "0.00", altcha: null }),
  }));
  expect(document.querySelector("altcha-widget")).toBeNull();
});
it.each(["0.00", "10.00"])("13 purchased credits require a fresh signature and proof; refundable %s", async (refundable) => {
  summary.refundable_cash = refundable;
  renderProfile();
  const dialog = await continueToCash();
  expect(within(dialog).getByText(/You are giving up 12.00 purchased credits/)).toBeInTheDocument();
  if (refundable === "0.00") expect(within(dialog).queryByText(/info@portfonia.com/)).toBeNull();
  else expect(within(dialog).getByText(/10.00.*info@portfonia.com/)).toBeInTheDocument();
  const confirm = within(dialog).getByRole("button", { name: "Give up 12.00 purchased credits and delete account" });
  expect(within(dialog).getByRole("textbox")).toHaveValue("");
  expect(confirm).toBeDisabled();
  solve();
  expect(confirm).toBeDisabled();
  await userEvent.type(within(dialog).getByRole("textbox"), "user@example.com");
  // Expiry/rejection events also remove the solved state.
  const widget = document.querySelector("altcha-widget");
  if (!widget) throw new Error("missing widget");
  fireEvent(widget, new CustomEvent("statechange", { detail: { state: "expired" } }));
  expect(confirm).toBeDisabled();
  solve();
  await userEvent.click(confirm);
  await waitFor(() => expect(replace).toHaveBeenCalledExactlyOnceWith("/"));
  expect(signOut).toHaveBeenCalledExactlyOnceWith({ scope: "local" });
  expect(screen.queryByRole("alert")).toBeNull();
  expect(fetchMock).toHaveBeenCalledWith("/api/me/account-deletion", expect.objectContaining({
    method: "POST", body: JSON.stringify({ confirm_email: "user@example.com", relinquish_cash: "12.00", altcha: "solved" }),
  }));
});
it.each([[409, "balance_changed", "Your balance changed. Please start again."],
  [400, "invalid captcha", "Verification expired or is invalid. Please start again."]])(
  "14 %s closes the flow and reopening starts at dialog one", async (status, detail, message) => {
    result = Response.json({ detail }, { status: Number(status) });
    renderProfile();
    const dialog = await continueToCash();
    await userEvent.type(within(dialog).getByRole("textbox"), "user@example.com");
    solve();
    await userEvent.click(within(dialog).getByRole("button", { name: /Give up/ }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(screen.getByRole("alert")).toHaveTextContent(String(message));
    const first = await open();
    expect(within(first).getByRole("textbox")).toHaveValue("");
    expect(within(first).getByRole("button", { name: "Continue" })).toBeDisabled();
  });
it.each(["first", "second"])("14 closing the %s dialog discards all inputs and proof", async (step) => {
  renderProfile();
  const dialog = step === "first" ? await open() : await continueToCash();
  await userEvent.type(within(dialog).getByRole("textbox"), "user@example.com");
  if (step === "second") solve();
  await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
  await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
  const first = await open();
  expect(within(first).getByRole("textbox")).toHaveValue("");
  expect(document.querySelector("altcha-widget")).toBeNull();
});
it.each(["0.00", "3.01"])("16 discloses tokens, fingerprint, re-registration and gift %s", async (gift) => {
  summary.gift_balance = gift;
  renderProfile();
  const dialog = await open();
  expect(within(dialog).getByText(/All your API tokens will be revoked/)).toBeInTheDocument();
  expect(within(dialog).getByText(/one-way fingerprint/)).toBeInTheDocument();
  expect(within(dialog).getByText(/you will need a new invitation/)).toBeInTheDocument();
  expect(within(dialog).getByText(/unused part of the current period/)).toBeInTheDocument();
  if (gift === "0.00") expect(within(dialog).queryByText(/gift credits/)).toBeNull();
  else expect(within(dialog).getByText("Your 3.01 gift credits will also be cleared.")).toBeInTheDocument();
});
it.each([[502, "Deletion failed. Nothing was changed; please retry."],
  [500, "Deletion could not be completed. Please contact info@portfonia.com."]])("shows the %s failure without logging out", async (status, message) => {
  result = Response.json({ detail: "fixture" }, { status: Number(status) });
  renderProfile();
  const dialog = await open();
  await userEvent.type(within(dialog).getByRole("textbox"), "user@example.com");
  await userEvent.click(within(dialog).getByRole("button", { name: "Delete account" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(String(message)));
  expect(signOut).not.toHaveBeenCalled();
  expect(replace).not.toHaveBeenCalled();
});

it("3 a thrown request error shows the failure and does not sign out", async () => {
  fetchMock.mockImplementation(async (_url, options) => {
    if (options?.method === "POST") throw new Error("network down");
    return Response.json(summary);
  });
  renderProfile();
  const dialog = await open();
  await userEvent.type(within(dialog).getByRole("textbox"), "user@example.com");
  await userEvent.click(within(dialog).getByRole("button", { name: "Delete account" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Deletion failed. Nothing was changed; please retry."));
  expect(signOut).not.toHaveBeenCalled();
  expect(replace).not.toHaveBeenCalled();
});
it("shows the negative balance contact message without deleting or signing out", async () => {
  summary.cash_balance = "-0.01";
  result = Response.json({ detail: "account has a negative balance; contact info@portfonia.com" }, { status: 409 });
  renderProfile();
  const dialog = await open();
  await userEvent.type(within(dialog).getByRole("textbox"), "user@example.com");
  await userEvent.click(within(dialog).getByRole("button", { name: "Delete account" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Your account has a negative credit balance. Please contact info@portfonia.com to delete your account.");
  expect(signOut).not.toHaveBeenCalled();
  expect(replace).not.toHaveBeenCalled();
});
