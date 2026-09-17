import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { useConsumeLinkToken } from "./use-consume-link-token";

function TestComponent() {
  const token = useConsumeLinkToken();
  return <div data-testid="token">{token ?? "null"}</div>;
}

describe("useConsumeLinkToken", () => {
  afterEach(() => {
    window.history.replaceState(null, "", "/");
  });

  it("reads the token out of the URL fragment on mount", () => {
    window.history.pushState(null, "", "/vigil/confirm#abc123token");

    render(<TestComponent />);

    expect(screen.getByTestId("token")).toHaveTextContent("abc123token");
  });

  it("strips the fragment from the URL via replaceState (never left visible, never pushed to history)", () => {
    window.history.pushState(null, "", "/vigil/confirm#abc123token");

    render(<TestComponent />);

    expect(window.location.hash).toBe("");
    expect(window.location.pathname).toBe("/vigil/confirm");
  });

  it("returns null when there is no fragment", () => {
    window.history.pushState(null, "", "/vigil/confirm");

    render(<TestComponent />);

    expect(screen.getByTestId("token")).toHaveTextContent("null");
  });

  // Design section 6: "no local/sessionStorage" for the raw token — this
  // hook must not be the thing that puts it there. (localStorage itself is
  // unavailable in this project's vitest/jsdom setup — see
  // --localstorage-file — so only sessionStorage is checkable here.)
  it("never writes the token to sessionStorage", () => {
    window.history.pushState(null, "", "/vigil/confirm#abc123token");

    render(<TestComponent />);

    expect(window.sessionStorage.length).toBe(0);
  });
});
