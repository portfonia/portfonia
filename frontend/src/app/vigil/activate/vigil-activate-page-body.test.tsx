import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LocaleProvider } from "@/app/_components/locale-provider";

const {
  armVigilVault,
  getVigilVaultStatus,
  listVigilConfirmationEmails,
  pauseVigilVault,
  sendVigilConfirmationEmailVerification,
  routerPush,
  VigilApiErrorCtor,
  VigilRevisionConflictErrorCtor,
} = vi.hoisted(() => {
  class VigilApiErrorCtor extends Error {
    status: number;
    detail: unknown;

    constructor(status: number, detail: unknown) {
      super(`vigil request failed: ${status}`);
      this.status = status;
      this.detail = detail;
    }
  }
  class VigilRevisionConflictErrorCtor extends Error {
    currentRevision: number;

    constructor(currentRevision: number) {
      super("vigil vault revision conflict");
      this.currentRevision = currentRevision;
    }
  }
  return {
    armVigilVault: vi.fn(),
    getVigilVaultStatus: vi.fn(),
    listVigilConfirmationEmails: vi.fn(),
    pauseVigilVault: vi.fn(),
    sendVigilConfirmationEmailVerification: vi.fn(),
    routerPush: vi.fn(),
    VigilApiErrorCtor,
    VigilRevisionConflictErrorCtor,
  };
});

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: routerPush }) }));
vi.mock("@/lib/vigil/api", () => ({
  armVigilVault,
  getVigilVaultStatus,
  listVigilConfirmationEmails,
  pauseVigilVault,
  sendVigilConfirmationEmailVerification,
  VigilApiError: VigilApiErrorCtor,
  VigilRevisionConflictError: VigilRevisionConflictErrorCtor,
}));

import { VigilActivatePageBody } from "./vigil-activate-page-body";

const EMAILS = [{ id: "e1", address: "owner@example.com", verified_at: null }];
const PENDING = {
  vault_id: "v1",
  phase: "DISARMED",
  revision: 4,
  pending: {
    config_id: "c1",
    object_id: "o1",
    object_status: "ready",
    confirmation_email_id: "e1",
    confirmation_email_verified: false,
  },
};

function renderPage() {
  return render(
    <LocaleProvider>
      <VigilActivatePageBody />
    </LocaleProvider>,
  );
}

afterEach(() => {
  vi.resetAllMocks();
});

describe("VigilActivatePageBody", () => {
  it("restores an unverified pending candidate from server state and sends verification", async () => {
    getVigilVaultStatus.mockResolvedValue(PENDING);
    listVigilConfirmationEmails.mockResolvedValue(EMAILS);
    sendVigilConfirmationEmailVerification.mockResolvedValue({ revision: 5, email: EMAILS[0] });
    renderPage();

    expect(await screen.findByText(/owner@example.com/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /send verification/i }));

    expect(sendVigilConfirmationEmailVerification).toHaveBeenCalledWith("e1", 4);
  });

  it("activates the exact server-projected candidate after email verification", async () => {
    getVigilVaultStatus.mockResolvedValue({
      ...PENDING,
      revision: 7,
      pending: { ...PENDING.pending, confirmation_email_verified: true },
    });
    listVigilConfirmationEmails.mockResolvedValue([
      { ...EMAILS[0], verified_at: "2026-09-19T00:00:00Z" },
    ]);
    armVigilVault.mockResolvedValue({ phase: "ARMED", revision: 8 });
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: /^activate$/i }));

    expect(armVigilVault).toHaveBeenCalledWith({
      expected_revision: 7,
      config_id: "c1",
      object_id: "o1",
    });
  });

  it("pauses an active vault using its authoritative revision", async () => {
    getVigilVaultStatus.mockResolvedValue({
      vault_id: "v1",
      phase: "CHALLENGE_2",
      revision: 9,
      pending: null,
    });
    listVigilConfirmationEmails.mockResolvedValue(EMAILS);
    pauseVigilVault.mockResolvedValue({ phase: "DISARMED", revision: 10 });
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: /^pause$/i }));

    expect(pauseVigilVault).toHaveBeenCalledWith(9);
  });

  it("returns an expired session to login with the Activate destination", async () => {
    getVigilVaultStatus.mockRejectedValue(new VigilApiErrorCtor(401, "unauthorized"));
    listVigilConfirmationEmails.mockResolvedValue(EMAILS);
    renderPage();

    await waitFor(() =>
      expect(routerPush).toHaveBeenCalledWith(
        "/login?reason=expired&next=/vigil/activate",
      ),
    );
  });
});
