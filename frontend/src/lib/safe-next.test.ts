import { expect, it } from "vitest";
import { safeNext } from "./safe-next";

it.each([
  ["/portfolio?focus=currency", "/portfolio?focus=currency"],
  ["/holdings/changes", "/holdings/changes"],
  ["", "/portfolio"],
  ["//evil.example", "/portfolio"],
  ["https://evil.example", "/portfolio"],
  ["/\\evil.example", "/portfolio"],
])("828 safeNext(%s) returns %s", (raw, expected) => {
  expect(safeNext(raw)).toBe(expected);
});
