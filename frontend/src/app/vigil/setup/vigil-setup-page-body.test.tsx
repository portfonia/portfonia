import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { LocaleProvider } from "@/app/_components/locale-provider";

const { useVigilSetup } = vi.hoisted(() => ({ useVigilSetup: vi.fn() }));
vi.mock("./_lib/use-vigil-setup", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./_lib/use-vigil-setup")>();
  return { ...actual, useVigilSetup };
});

import { VigilSetupPageBody } from "./vigil-setup-page-body";

function baseHookState(overrides: Partial<ReturnType<typeof useVigilSetup>> = {}) {
  return {
    vaultStatus: { vault_id: null, phase: "DISARMED", revision: 0 },
    vaultStatusError: false,
    file: null,
    setFile: vi.fn(),
    hasPassword: null,
    setHasPassword: vi.fn(),
    password: "",
    setPassword: vi.fn(),
    passwordConfirm: "",
    setPasswordConfirm: vi.fn(),
    recipients: [{ email: "", email_confirm: "" }],
    setRecipients: vi.fn(),
    message: "",
    setMessage: vi.fn(),
    intervalDays: 30,
    setIntervalDays: vi.fn(),
    graceHours: 72,
    setGraceHours: vi.fn(),
    phase: "form",
    progress: 0,
    errorMessage: null,
    errorCode: null,
    validationErrors: [],
    submit: vi.fn(),
    cancel: vi.fn(),
    sendDrill: vi.fn(),
    activate: vi.fn(),
    drillUiState: "idle",
    armed: false,
    ...overrides,
  };
}

function renderPage(overrides: Partial<ReturnType<typeof useVigilSetup>> = {}) {
  useVigilSetup.mockReturnValue(baseHookState(overrides));
  return render(
    <LocaleProvider>
      <VigilSetupPageBody />
    </LocaleProvider>,
  );
}

describe("VigilSetupPageBody", () => {
  it("renders the file picker, password choice, recipients, and schedule fields", () => {
    renderPage();

    expect(screen.getByLabelText(/file/i)).toBeInTheDocument();
    expect(screen.getByText(/set a password/i)).toBeInTheDocument();
    expect(screen.getByText(/no password/i)).toBeInTheDocument();
    expect(screen.getByLabelText("Recipient 1 email")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /encrypt and upload/i })).toBeInTheDocument();
  });

  it("shows the no-password warning only once that choice is explicitly made", () => {
    const { rerender } = renderPage({ hasPassword: null });
    expect(screen.queryByText(/server-held data key alone/i)).not.toBeInTheDocument();

    useVigilSetup.mockReturnValue(baseHookState({ hasPassword: false }));
    rerender(
      <LocaleProvider>
        <VigilSetupPageBody />
      </LocaleProvider>,
    );
    expect(screen.getByText(/server-held data key alone/i)).toBeInTheDocument();
  });

  it("shows password fields only when 'set a password' is chosen", () => {
    renderPage({ hasPassword: true });
    expect(screen.getByLabelText(/^password$/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/confirm password/i)).toBeInTheDocument();
  });

  it("shows the active-arrangement note when the vault is not DISARMED", () => {
    renderPage({ vaultStatus: { vault_id: "v1", phase: "ARMED", revision: 3 } });
    expect(screen.getByText(/does not pause it/i)).toBeInTheDocument();
  });

  it("does not show the active-arrangement note for a DISARMED/no-vault status", () => {
    renderPage({ vaultStatus: { vault_id: null, phase: "DISARMED", revision: 0 } });
    expect(screen.queryByText(/does not pause it/i)).not.toBeInTheDocument();
  });

  it("shows a single busy indicator with upload progress while submitting", () => {
    renderPage({ phase: "submitting", progress: 0.42 });
    expect(screen.getByText(/submitting/i)).toBeInTheDocument();
  });

  // #529 (#516 finding 18): no named micro-stages surface to the user —
  // internal pipeline steps (configure/init/encrypt/upload) stay sequential
  // awaits behind one busy phase.
  it("never surfaces named micro-stages (configuring/preparing/encrypting/uploading) while submitting", () => {
    renderPage({ phase: "submitting", progress: 0.1 });
    expect(screen.queryByText(/configuring|preparing|encrypting|uploading/i)).not.toBeInTheDocument();
  });

  it("shows a Cancel button while submitting, and calls cancel() on click", async () => {
    const cancel = vi.fn();
    renderPage({ phase: "submitting", cancel });
    const cancelButton = screen.getByRole("button", { name: /cancel/i });
    await userEvent.click(cancelButton);
    expect(cancel).toHaveBeenCalledTimes(1);
  });

  it("shows the ready-awaiting-drill state with an explicit send-confirmation button", () => {
    renderPage({ phase: "ready", drillUiState: "idle" });
    expect(screen.getByText(/awaiting drill/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /send account confirmation/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^activate$/i })).not.toBeInTheDocument();
  });

  it("shows pending/sent/unknown/expired/confirmed drill states", () => {
    renderPage({ phase: "ready", drillUiState: "pending" });
    expect(screen.getByText(/confirmation email is queued/i)).toBeInTheDocument();
  });

  it("shows an explicit activate button only after confirmation", async () => {
    const activate = vi.fn();
    renderPage({ phase: "ready", drillUiState: "confirmed", activate });
    const button = screen.getByRole("button", { name: /^activate$/i });
    await userEvent.click(button);
    expect(activate).toHaveBeenCalledTimes(1);
  });

  it("shows a Retry button (not the initial submit label) after an error", () => {
    renderPage({ phase: "error", errorCode: "apiError" });
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  it("calls submit() when the form is submitted", async () => {
    const submit = vi.fn();
    renderPage({ submit });
    await userEvent.click(screen.getByRole("button", { name: /encrypt and upload/i }));
    expect(submit).toHaveBeenCalledTimes(1);
  });
});
