import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { LocaleProvider } from "@/app/_components/locale-provider";
import { BuyCreditsLink } from "./buy-credits-link";

it("links buyers to the signed-in Profile purchase block", () => {
  render(<LocaleProvider><BuyCreditsLink /></LocaleProvider>);
  expect(screen.getByRole("link", { name: "Buy credits" })).toHaveAttribute("href", "/profile");
});
