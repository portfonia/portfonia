import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

const { getUser, onAuthStateChange, unsubscribe, logout } = vi.hoisted(() => ({
  getUser: vi.fn(),
  onAuthStateChange: vi.fn(),
  unsubscribe: vi.fn(),
  logout: vi.fn(),
}));

vi.mock("@/lib/supabase/browser", () => ({
  createClient: () => ({
    auth: {
      getUser,
      onAuthStateChange: (cb: (...args: unknown[]) => void) => {
        onAuthStateChange(cb);
        return { data: { subscription: { unsubscribe } } };
      },
    },
  }),
}));

vi.mock("@/lib/auth-actions", () => ({ logout }));

import { AuthStatus } from "./auth-status";

describe("AuthStatus", () => {
  it("renders nothing while the session check is in flight (avoids a login/logout flash)", () => {
    getUser.mockReturnValue(new Promise(() => {})); // never resolves
    const { container } = render(<AuthStatus loginLabel="Log in" logoutLabel="Log out" />);

    expect(container).toBeEmptyDOMElement();
  });

  it("shows a Log in link when there is no session", async () => {
    getUser.mockResolvedValue({ data: { user: null } });
    render(<AuthStatus loginLabel="Log in" logoutLabel="Log out" />);

    expect(await screen.findByRole("link", { name: "Log in" })).toHaveAttribute(
      "href",
      "/login",
    );
  });

  it("shows the account email and a Log out button when there is a session", async () => {
    getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
    render(<AuthStatus loginLabel="Log in" logoutLabel="Log out" />);

    expect(await screen.findByText("a@b.com")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Log out" })).toBeInTheDocument();
  });

  it("calls the logout action when Log out is clicked", async () => {
    getUser.mockResolvedValue({ data: { user: { email: "a@b.com" } } });
    const user = userEvent.setup();
    render(<AuthStatus loginLabel="Log in" logoutLabel="Log out" />);

    await user.click(await screen.findByRole("button", { name: "Log out" }));

    await waitFor(() => expect(logout).toHaveBeenCalled());
  });

  it("unsubscribes from auth state changes on unmount", () => {
    getUser.mockResolvedValue({ data: { user: null } });
    const { unmount } = render(<AuthStatus loginLabel="Log in" logoutLabel="Log out" />);

    unmount();

    expect(unsubscribe).toHaveBeenCalled();
  });
});
