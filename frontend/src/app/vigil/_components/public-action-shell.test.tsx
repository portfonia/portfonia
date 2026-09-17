import { render, screen } from "@testing-library/react";
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

  it.each(["confirm", "retrieve", "revoke"] as const)(
    "renders the %s title and the shared unavailable copy",
    (action) => {
      render(
        <LocaleProvider>
          <PublicActionShell action={action} />
        </LocaleProvider>,
      );

      expect(screen.getByText(/isn't active yet/i)).toBeInTheDocument();
    },
  );

  // A02 / A06 (#450 Design section 5/9): mounting or GETting a public shell
  // must fetch NO private metadata and take NO business action — no
  // /vigil/public/* endpoint exists yet at this checkpoint, and even once
  // it does, a mere page load must never itself invoke it.
  it("makes no network request on mount", () => {
    const fetchMock = vi.fn();
    global.fetch = fetchMock;

    render(
      <LocaleProvider>
        <PublicActionShell action="confirm" />
      </LocaleProvider>,
    );

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("strips a token fragment from the URL without acting on it", () => {
    window.history.pushState(null, "", "/vigil/confirm#some-token-abc");

    render(
      <LocaleProvider>
        <PublicActionShell action="confirm" />
      </LocaleProvider>,
    );

    expect(window.location.hash).toBe("");
  });
});
