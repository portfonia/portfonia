import { describe, expect, it } from "vitest";

import { filenameFromContentDisposition } from "./template";

describe("filenameFromContentDisposition", () => {
  it("reads a quoted filename from Content-Disposition", () => {
    expect(
      filenameFromContentDisposition(
        'attachment; filename="holdings-20260902-051530Z.md"',
        "holdings.md",
      ),
    ).toBe("holdings-20260902-051530Z.md");
  });

  it("falls back when the header is missing", () => {
    expect(filenameFromContentDisposition(null, "holdings.md")).toBe("holdings.md");
  });
});
