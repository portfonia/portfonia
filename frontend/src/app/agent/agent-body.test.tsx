import { StrictMode } from "react";
import { render, screen, waitFor, cleanup, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { NextIntlClientProvider } from "next-intl";
import en from "@/locales/en.json";
import { catalogs, type Locale } from "@/locales";

const mocks = vi.hoisted(() => ({ session: { status: "guest" }, list: vi.fn(), create: vi.fn(), revoke: vi.fn(), fetch: vi.fn() }));
vi.mock("@/hooks/use-session", () => ({ useSession: () => mocks.session }));
vi.mock("@/lib/api", () => ({ listApiTokens: mocks.list, createApiToken: mocks.create, revokeApiToken: mocks.revoke }));
import { AgentBody } from "./agent-body";
import { RevokeResult } from "./revoke/revoke-result";

const stored = { id: "id-1", name: "My agent", prefix: "pfa_12345678", permission: "read_only" as const, created_at: "2026-10-04T12:00:00-04:00", expires_at: null, last_used_at: null, status: "active" };
function wrapper(children: React.ReactNode) {
  return <NextIntlClientProvider locale="en" messages={en}>{children}</NextIntlClientProvider>;
}
beforeEach(() => { vi.clearAllMocks(); mocks.session = { status: "guest" }; mocks.list.mockResolvedValue([]); vi.stubGlobal("fetch", mocks.fetch); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("acceptance_16 guests see sign-in without token settings", () => {
  render(wrapper(<AgentBody />));
  expect(screen.getByRole("heading", { name: "AI Agent" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/login");
  expect(screen.queryByLabelText("Token name")).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: en.agent.guideLink })).toHaveAttribute("href", "/agent/guide");
  expect(mocks.list).not.toHaveBeenCalled();
});

it("acceptance_16 show once, copy and revoke update the list", async () => {
  mocks.session = { status: "authed" };
  mocks.create.mockResolvedValue({ ...stored, token: "pfa_full_secret_shown_once" });
  mocks.list.mockResolvedValueOnce([]).mockResolvedValueOnce([stored]).mockResolvedValueOnce([]);
  mocks.revoke.mockResolvedValue(undefined);
  const user = userEvent.setup();
  const view = render(wrapper(<AgentBody />));
  await waitFor(() => expect(mocks.list).toHaveBeenCalledTimes(1));
  await user.type(screen.getByLabelText("Token name"), "My agent");
  expect(screen.getByRole("radio", { name: "Read-only" })).toBeChecked();
  await user.click(screen.getByRole("button", { name: "Create token" }));
  expect(await screen.findByText("pfa_full_secret_shown_once")).toBeInTheDocument();
  expect(mocks.create).toHaveBeenCalledWith("My agent", null, "read_only");
  expect(screen.getByRole("radio", { name: "Read-only" })).toBeChecked();
  await user.click(screen.getByRole("button", { name: "Copy token" }));
  expect(await navigator.clipboard.readText()).toBe("pfa_full_secret_shown_once");
  await user.click(screen.getByRole("button", { name: "Hide token" }));
  expect(screen.queryByText("pfa_full_secret_shown_once")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Revoke" }));
  await user.click(screen.getByRole("button", { name: "Confirm revoke" }));
  await waitFor(() => expect(mocks.revoke).toHaveBeenCalledWith("id-1"));
  await waitFor(() => expect(screen.queryByText("My agent")).not.toBeInTheDocument());
  view.unmount();
  render(wrapper(<AgentBody />));
  expect(screen.queryByText("pfa_full_secret_shown_once")).not.toBeInTheDocument();
});

it.each([true, false])("acceptance_16 revoke page posts once under StrictMode (%s)", async (ok) => {
  mocks.fetch.mockResolvedValue({ ok });
  const view = render(wrapper(<StrictMode><RevokeResult token="signed-link" /></StrictMode>));
  expect(await screen.findByRole(ok ? "status" : "alert")).toHaveTextContent(ok ? "All API token access for your account has been revoked." : "This link is invalid or has expired.");
  expect(mocks.fetch).toHaveBeenCalledTimes(1);
  expect(mocks.fetch).toHaveBeenCalledWith("/api/api-tokens/revoke-by-link", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: "signed-link" }) });
  view.rerender(wrapper(<StrictMode><RevokeResult token="signed-link" /></StrictMode>));
  expect(mocks.fetch).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});


it("797 renders endpoint headings in the approved order", async () => {
  mocks.session = { status: "authed" };
  render(<NextIntlClientProvider locale="en" messages={en}><AgentBody /></NextIntlClientProvider>);
  await waitFor(() => expect(mocks.list).toHaveBeenCalledTimes(1));
  const endpointsCard = screen.getByRole("heading", { name: en.agent.docs.endpointsTitle }).closest('[data-slot="card"]')!;
  const headings = Array.from(endpointsCard.querySelectorAll("h3"), heading => heading.textContent);
  expect(headings).toEqual((["token", "reports", "snapshots", "intel", "technical", "technicalSeries", "jade", "holdingsRead", "changes", "holdingsValidate", "holdingsAdd", "holdingsReplace", "holdingsPatch", "holdingsDelete", "holdingsReplaceAll"] as const).map(k => en.agent.docs[`${k}Heading`]));
});

it.each(Object.keys(catalogs) as Locale[])("acceptance_09 documents all endpoints in %s with existing limits and security copy", async (locale) => {
  mocks.session = { status: "authed" };
  const messages = catalogs[locale];
  render(<NextIntlClientProvider locale={locale} messages={messages}><AgentBody /></NextIntlClientProvider>);
  await waitFor(() => expect(mocks.list).toHaveBeenCalledTimes(1));
  const agent = messages.agent;
  expect(screen.getByRole("link", { name: agent.guideLink })).toHaveAttribute("href", locale === "en" ? "/agent/guide" : `/${locale}/agent/guide`);
  expect(screen.getByRole("heading", { name: agent.docs.gettingStartedTitle })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: agent.docs.endpointsTitle })).toBeInTheDocument();
  for (const copy of [agent.limitsTitle, agent.limits, agent.quiet, agent.notice, agent.tokenRule]) {
    expect(screen.getByText(copy)).toBeInTheDocument();
    expect(Object.values(agent.docs)).not.toContain(copy);
  }
  const endpointsCard = screen.getByRole("heading", { name: agent.docs.endpointsTitle }).closest('[data-slot="card"]')!;
  const endpoints = within(endpointsCard as HTMLElement);
  if (locale === "en") {
    expect(endpoints.getByText(agent.docs.endpointsLead)).toBeInTheDocument();
    expect(endpoints.getByText(agent.docs.endpointsClosing)).toBeInTheDocument();
    expect(endpointsCard.textContent).not.toContain("?");
    expect(endpointsCard.textContent).not.toContain("https://api.portfonia.com/agent/openapi.json");
  }
  for (const endpoint of ["GET /agent/v1/reports", "GET /agent/v1/snapshots", "GET /agent/v1/intel", "GET /agent/v1/holdings", "PUT /agent/v1/holdings", "POST /agent/v1/holdings", "PUT /agent/v1/holdings/{holding_id}", "PATCH /agent/v1/holdings/{holding_id}", "DELETE /agent/v1/holdings/{holding_id}", "POST /agent/v1/holdings/validate"]) {
    expect(endpoints.getByText(endpoint)).toBeInTheDocument();
  }
  expect(screen.getByRole("link", { name: "llms.txt" })).toHaveAttribute("href", "/llms.txt");
  expect(screen.getByRole("link", { name: "agent.md" })).toHaveAttribute("href", "/agent.md");
});


it("polish_660_acceptance_6 aligns page typography", () => {
  render(wrapper(<AgentBody />));
  const heading = screen.getByRole("heading", { level: 1 });
  expect(heading).toHaveClass("font-heading", "text-2xl", "font-medium");
  expect(heading).not.toHaveClass("font-serif");
  expect(heading).not.toHaveClass("font-semibold");
  expect(screen.getByText(en.agent.intro)).toHaveClass("text-sm", "text-muted-foreground");
  for (const label of [en.agent.docs.gettingStartedTitle, en.agent.docs.endpointsTitle, en.agent.limitsTitle]) {
    expect(screen.getByText(label).closest('[data-slot="card-title"]')).toHaveClass("font-heading", "text-base", "font-medium");
    expect(screen.getByText(label).closest('[data-slot="card"]')).toHaveClass("text-sm");
  }
});

it.each(["en", "es", "zh-Hans", "zh-Hant"] as const)("754 current holdings endpoints are visible in %s", (locale) => {
  render(<NextIntlClientProvider locale={locale} messages={catalogs[locale]}><AgentBody /></NextIntlClientProvider>);
  expect(screen.getByRole("link", { name: catalogs[locale].agent.guideLink })).toHaveAttribute("href", locale === "en" ? "/agent/guide" : `/${locale}/agent/guide`);
  for (const endpoint of ["GET /agent/v1/holdings", "PUT /agent/v1/holdings", "POST /agent/v1/holdings", "POST /agent/v1/holdings/validate", "PUT /agent/v1/holdings/{holding_id}", "PATCH /agent/v1/holdings/{holding_id}", "DELETE /agent/v1/holdings/{holding_id}"]) {
    expect(screen.getByText(endpoint)).toBeInTheDocument();
  }
});

it("770 renders the Jade endpoint summary", () => {
  render(wrapper(<AgentBody />));
  expect(screen.getByRole("heading", { name: "Read Jade risk results" })).toBeInTheDocument();
  expect(screen.getByText("GET /agent/v1/jade/{tool}")).toBeInTheDocument();
  expect(screen.getByText("Active Jade; read-only and holdings-write tokens")).toBeInTheDocument();
});


it("775 verifies token permissions and reserves all eight holdings access lines for Jade", () => {
  render(wrapper(<AgentBody />));
  expect(screen.getByRole("heading", { name: "Verify token permissions" })).toBeInTheDocument();
  for (const prefix of ["holdingsRead", "holdingsReplaceAll", "holdingsAdd", "holdingsReplace", "holdingsPatch", "holdingsDelete", "holdingsValidate", "changes"] as const) {
    const card = screen.getByRole("heading", { name: en.agent.docs[`${prefix}Heading`] }).parentElement;
    expect(card).toHaveTextContent(`Applies during an active Jade period. ${["holdingsRead", "holdingsValidate", "changes"].includes(prefix) ? "Read-only tokens and holdings-write tokens are both accepted." : "Only a holdings-write token is accepted."}`);
  }
});
