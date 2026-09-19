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
});
