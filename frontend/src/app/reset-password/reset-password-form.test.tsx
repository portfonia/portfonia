import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { updateUser, push } = vi.hoisted(() => ({
  updateUser: vi.fn(),
  push: vi.fn(),
}));

vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
vi.mock("@/lib/supabase/browser", () => ({
  createClient: () => ({ auth: { updateUser } }),
}));

import { LocaleProvider } from "@/app/_components/locale-provider";
import { ResetPasswordForm } from "./reset-password-form";

function renderForm() {
  return render(
    <LocaleProvider>
      <ResetPasswordForm />
    </LocaleProvider>,
  );
}

const password = () => screen.getByLabelText(/^password$/i);
const confirmPassword = () => screen.getByLabelText(/^confirm password$/i);

describe("ResetPasswordForm", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("blocks submit when the passwords do not match, without calling Supabase", async () => {
    const user = userEvent.setup();
    renderForm();

    await user.type(password(), "correcthorse");
    await user.type(confirmPassword(), "wronghorse");
    await user.click(screen.getByRole("button", { name: /update password/i }));

    expect(await screen.findByText(/do not match/i)).toBeInTheDocument();
    expect(updateUser).not.toHaveBeenCalled();
  });

  it("calls supabase.auth.updateUser with the new password when they match", async () => {
    updateUser.mockResolvedValue({ error: null });
    const user = userEvent.setup();
    renderForm();

    await user.type(password(), "correcthorse");
    await user.type(confirmPassword(), "correcthorse");
    await user.click(screen.getByRole("button", { name: /update password/i }));

    await waitFor(() =>
      expect(updateUser).toHaveBeenCalledWith({ password: "correcthorse" }),
    );
  });

  it("shows a success message and redirects to /login after a successful update", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    updateUser.mockResolvedValue({ error: null });
    const user = userEvent.setup();
    renderForm();

    await user.type(password(), "correcthorse");
    await user.type(confirmPassword(), "correcthorse");
    await user.click(screen.getByRole("button", { name: /update password/i }));

    expect(await screen.findByRole("status")).toHaveTextContent(/password has been updated/i);
    await vi.runAllTimersAsync();
    expect(push).toHaveBeenCalledWith("/login");
    vi.useRealTimers();
  });

  it("shows a generic failure message when Supabase rejects the update (e.g. expired link)", async () => {
    updateUser.mockResolvedValue({ error: { message: "Auth session missing!" } });
    const user = userEvent.setup();
    renderForm();

    await user.type(password(), "correcthorse");
    await user.type(confirmPassword(), "correcthorse");
    await user.click(screen.getByRole("button", { name: /update password/i }));

    expect(await screen.findByText(/could not update your password/i)).toBeInTheDocument();
    expect(push).not.toHaveBeenCalled();
  });
});
