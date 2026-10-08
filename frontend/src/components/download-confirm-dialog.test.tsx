import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { downloadFile } = vi.hoisted(() => ({ downloadFile: vi.fn() }));
vi.mock("@/lib/template", async () => {
  const actual = await vi.importActual<typeof import("@/lib/template")>("@/lib/template");
  return { ...actual, downloadFile };
});
vi.mock("@/lib/auth-actions", () => ({ logout: vi.fn() }));

import { LocaleProvider } from "@/app/_components/locale-provider";
import { DownloadConfirmDialog, formatFileSize } from "./download-confirm-dialog";

beforeEach(() => {
  downloadFile.mockReset();
});

describe("formatFileSize (#679)", () => {
  it.each([
    [512, "512 B"],
    [1536, "1.5 KB"],
    [20480, "20.0 KB"],
  ])("formats %d bytes as %s", (bytes, expected) => {
    expect(formatFileSize(bytes)).toBe(expected);
  });
});

describe("DownloadConfirmDialog (#679)", () => {
  const blob = new Blob(["x".repeat(1536)]);
  const pending = { blob, filename: "a.md", description: "Holdings upload template" };

  it("shows description, file name and size, and saves only on confirm", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <LocaleProvider routeLocale={null}>
        <DownloadConfirmDialog pending={pending} onClose={onClose} />
      </LocaleProvider>,
    );

    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("Holdings upload template");
    expect(dialog).toHaveTextContent("a.md");
    expect(dialog).toHaveTextContent("1.5 KB");
    expect(downloadFile).not.toHaveBeenCalled();

    await user.click(within(dialog).getByRole("button", { name: /^download$/i }));

    expect(downloadFile).toHaveBeenCalledExactlyOnceWith(blob, "a.md");
    expect(onClose).toHaveBeenCalled();
  });

  it("cancel closes without saving", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <LocaleProvider routeLocale={null}>
        <DownloadConfirmDialog pending={pending} onClose={onClose} />
      </LocaleProvider>,
    );

    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: /cancel/i }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(downloadFile).not.toHaveBeenCalled();
  });

  it("renders nothing while no download is pending", () => {
    render(
      <LocaleProvider routeLocale={null}>
        <DownloadConfirmDialog pending={null} onClose={vi.fn()} />
      </LocaleProvider>,
    );

    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });
});
