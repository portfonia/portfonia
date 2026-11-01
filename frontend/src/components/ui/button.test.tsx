import { expect, test } from "vitest"

import { buttonVariants } from "./button"

test("destructive buttons use contrast-safe background and focus classes", () => {
  const classes = buttonVariants({ variant: "destructive" }).split(/\s+/)

  expect(classes).not.toContain("dark:bg-destructive/20")
  expect(classes).not.toContain("dark:hover:bg-destructive/30")
  expect(classes).not.toContain("focus-visible:border-destructive/40")
  expect(classes).toContain("focus-visible:border-destructive")
})
