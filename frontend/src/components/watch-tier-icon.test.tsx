import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LocaleProvider } from "@/app/_components/locale-provider";
import { WatchTierIcon } from "./watch-tier-icon";

function renderIcon(tier: "watch" | "focus" | "critical" | null | undefined) {
  return render(
    <LocaleProvider>
      <WatchTierIcon tier={tier} />
    </LocaleProvider>,
  );
}

describe("WatchTierIcon", () => {
  it("renders nothing for a holding with no watch tier", () => {
    const { container } = renderIcon(null);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when the tier is undefined", () => {
    const { container } = renderIcon(undefined);
    expect(container).toBeEmptyDOMElement();
  });

  it("labels the watch tier accessibly", () => {
    renderIcon("watch");
    expect(screen.getByRole("img", { name: "Watch" })).toBeInTheDocument();
  });

  it("labels the focus tier accessibly", () => {
    renderIcon("focus");
    expect(screen.getByRole("img", { name: "Focus" })).toBeInTheDocument();
  });

  it("labels the critical tier accessibly", () => {
    renderIcon("critical");
    expect(screen.getByRole("img", { name: "Critical" })).toBeInTheDocument();
  });
});
