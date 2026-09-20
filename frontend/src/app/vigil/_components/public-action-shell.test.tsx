import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LocaleProvider } from "@/app/_components/locale-provider";
import { PublicActionShell } from "./public-action-shell";

const originalFetch = global.fetch;

describe("PublicActionShell", () => {
  afterEach(() => {
    window.history.replaceState(null, "", "/");
    global.fetch = originalFetch;
    vi.resetAllMocks();
  });

  it("confirm shows an explicit button and does not fetch on mount", () => {
    const fetchMock = vi.fn();
    global.fetch = fetchMock;

    render(
      <LocaleProvider>
        <PublicActionShell />
      </LocaleProvider>,
    );

    expect(screen.getByRole("button", { name: /confirm/i })).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("strips a token fragment from the URL without acting on it", () => {
    window.history.pushState(null, "", "/vigil/confirm#some-token-abc");

    render(
      <LocaleProvider>
        <PublicActionShell />
      </LocaleProvider>,
    );

    expect(window.location.hash).toBe("");
  });

  it("links to Activate only for reusable email verification", async () => {
    window.history.pushState(null, "", "/vigil/confirm#email-token");
    global.fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ result: "email_verified", next_check_at: null }),
        { status: 200 },
      ),
    );
    const altcha = document.createElement("input");
    altcha.name = "altcha";
    altcha.value = "solved";
    document.body.appendChild(altcha);

    render(
      <LocaleProvider>
        <PublicActionShell />
      </LocaleProvider>,
    );
    const confirm = screen.getByRole("button", { name: /^confirm$/i });
    await waitFor(() => expect(confirm).toBeEnabled());
    await userEvent.click(confirm);

    expect(await screen.findByText(/email verified/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /continue to activate/i })).toHaveAttribute(
      "href",
      "/vigil/activate",
    );
    altcha.remove();
  });
});
