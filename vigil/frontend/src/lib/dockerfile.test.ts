// @vitest-environment node
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const dockerfile = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), "../../Dockerfile"),
  "utf8",
);

describe("vigil frontend Dockerfile", () => {
  it("is a bun Next.js multi-stage build, not nginx-static", () => {
    expect(dockerfile).toContain("oven/bun:1.4.0-alpine");
    expect(dockerfile).toContain("bun run build");
    expect(dockerfile).toContain("node:22-alpine");
    expect(dockerfile).not.toMatch(/nginx/);
  });
});
