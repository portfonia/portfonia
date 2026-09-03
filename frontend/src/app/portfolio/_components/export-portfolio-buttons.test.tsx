import { render, screen, waitFor } from "@testing-library/react";
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
  it("downloads an xlsx file when the xlsx button is clicked", async () => {
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
    expect(downloadFile).toHaveBeenCalledWith(blob, "portfolio-x.xlsx");
  });

  it("downloads a md file when the md button is clicked", async () => {
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
    expect(downloadFile).toHaveBeenCalledWith(blob, "portfolio-x.md");
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
