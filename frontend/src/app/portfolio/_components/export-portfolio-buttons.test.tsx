import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { exportPortfolio } = vi.hoisted(() => ({
  exportPortfolio: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, exportPortfolio };
});
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));

const { downloadFile } = vi.hoisted(() => ({ downloadFile: vi.fn() }));
vi.mock("@/lib/template", async () => {
  const actual = await vi.importActual<typeof import("@/lib/template")>("@/lib/template");
  return { ...actual, downloadFile };
});

import { LocaleProvider } from "@/app/_components/locale-provider";
import { ExportPortfolioButtons } from "./export-portfolio-buttons";

beforeEach(() => {
  exportPortfolio.mockReset();
  downloadFile.mockReset();
});

describe("ExportPortfolioButtons", () => {
  it.each([
    ["en", "en"],
    ["zh-Hans", "zh"],
    ["zh-Hant", "zh-Hant"],
  ])("passes UI locale %s to both portfolio downloads as %s (#585)", async (locale, expected) => {
    const store = new Map([["portfonia:locale", locale]]);
    Object.defineProperty(window, "localStorage", {
      value: {
        getItem: (key: string) => store.get(key) ?? null,
        setItem: (key: string, value: string) => void store.set(key, value),
        clear: () => store.clear(),
      }, configurable: true,
    });
    exportPortfolio.mockResolvedValue({ blob: new Blob(["data"]), filename: "portfolio.md" });
    const user = userEvent.setup();
    render(<LocaleProvider><ExportPortfolioButtons baseCurrency="USD" /></LocaleProvider>);
    for (const format of ["xlsx", "md"]) {
      await user.click(screen.getByRole("button", { name: new RegExp(`\\.${format}`, "i") }));
      await waitFor(() => expect(exportPortfolio).toHaveBeenCalledWith(format, "USD", expected));
      await screen.findByRole("alertdialog");
      await user.keyboard("{Escape}");
      await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    }
    store.clear();
  });
  it("downloads an xlsx file only after confirming (#679)", async () => {
    const blob = new Blob(["binary"]);
    exportPortfolio.mockResolvedValue({ blob, filename: "portfolio-x.xlsx" });
    const user = userEvent.setup();
    render(
      <LocaleProvider>
        <ExportPortfolioButtons baseCurrency="USD" />
      </LocaleProvider>,
    );

    await user.click(screen.getByRole("button", { name: /\.xlsx/i }));

    await waitFor(() => expect(exportPortfolio).toHaveBeenCalledWith("xlsx", "USD", "en"));
    // #679: fetched, but nothing is saved until the dialog is confirmed.
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("portfolio-x.xlsx");
    expect(dialog).toHaveTextContent("Holdings snapshot, base currency USD, English");
    expect(downloadFile).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: /^download$/i }));
    expect(downloadFile).toHaveBeenCalledExactlyOnceWith(blob, "portfolio-x.xlsx");
  });

  it("cancelling the md confirmation saves nothing (#679)", async () => {
    const blob = new Blob(["| a |"]);
    exportPortfolio.mockResolvedValue({ blob, filename: "portfolio-x.md" });
    const user = userEvent.setup();
    render(
      <LocaleProvider>
        <ExportPortfolioButtons baseCurrency="CNY" />
      </LocaleProvider>,
    );

    await user.click(screen.getByRole("button", { name: /\.md/i }));

    await waitFor(() => expect(exportPortfolio).toHaveBeenCalledWith("md", "CNY", "en"));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("portfolio-x.md");
    await user.click(within(dialog).getByRole("button", { name: /cancel/i }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(downloadFile).not.toHaveBeenCalled();
  });

  it("shows an error message when the download fails", async () => {
    exportPortfolio.mockRejectedValue(new Error("network down"));
    const user = userEvent.setup();
    render(
      <LocaleProvider>
        <ExportPortfolioButtons baseCurrency="USD" />
      </LocaleProvider>,
    );

    await user.click(screen.getByRole("button", { name: /\.md/i }));

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(downloadFile).not.toHaveBeenCalled();
  });

  it("is disabled while a currency switch is in flight", () => {
    render(
      <LocaleProvider>
        <ExportPortfolioButtons baseCurrency="USD" disabled />
      </LocaleProvider>,
    );

    expect(screen.getByRole("button", { name: /\.xlsx/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /\.md/i })).toBeDisabled();
  });
});
