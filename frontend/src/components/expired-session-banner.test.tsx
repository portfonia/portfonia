import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ExpiredSessionBanner } from "./expired-session-banner";

describe("ExpiredSessionBanner", () => {
  it("renders the expired-session notice when reason=expired", () => {
    render(<ExpiredSessionBanner reason="expired" />);

    expect(
      screen.getByText(/session ended after 15 minutes of inactivity/i),
    ).toBeInTheDocument();
  });

  it("renders nothing for any other or missing reason", () => {
    const { container, rerender } = render(<ExpiredSessionBanner />);
    expect(container).toBeEmptyDOMElement();

    rerender(<ExpiredSessionBanner reason="something_else" />);
    expect(container).toBeEmptyDOMElement();
  });
});
