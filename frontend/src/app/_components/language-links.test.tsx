import { createEvent, fireEvent, render, screen } from "@testing-library/react";
import { renderToString } from "react-dom/server";
import { beforeEach, expect, it, vi } from "vitest";
const { setLocale } = vi.hoisted(() => ({ setLocale: vi.fn() }));
vi.mock("./locale-provider", () => ({ useLocale: () => ({ locale: "zh-Hans", setLocale }) }));
vi.mock("next/link", () => ({ default: () => { throw new Error("Language links must use native anchors"); } }));
import { LanguageLinks } from "./language-links";
import { LOCALES } from "@/locales";

beforeEach(() => { setLocale.mockClear(); window.history.replaceState(null, "", "/zh-Hans/pricing"); });
it("7f server-renders plain language anchors and an unlinked current locale", () => {
  const html = renderToString(<LanguageLinks />);
  expect(html).toContain('href="/pricing"');
  expect(html).toContain('href="/zh-Hant/pricing"');
  expect(html).toContain('aria-current="page"');
  render(<LanguageLinks />);
  expect(screen.getByText(LOCALES[1].label)).not.toHaveAttribute("href");
  expect(screen.getByText(LOCALES[1].label)).toHaveAttribute("aria-current", "page");
});
it("calls setLocale without preventing the full document navigation", () => {
  render(<LanguageLinks />);
  const link = screen.getByRole("link", { name: "English" });
  const event = createEvent.click(link, { bubbles: true, cancelable: true });
  fireEvent(link, event);
  expect(setLocale).toHaveBeenCalledWith("en");
  expect(event.defaultPrevented).toBe(false);
});
it("does not render a row for a protected app path", () => {
  window.history.replaceState(null, "", "/portfolio");
  const { container } = render(<LanguageLinks />);
  expect(container).toBeEmptyDOMElement();
});
