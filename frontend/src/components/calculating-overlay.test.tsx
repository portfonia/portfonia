import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { CalculatingOverlay } from "./calculating-overlay";

it("A7 overlay uses caller label, inert controls and a covering pointer layer", () => {
  const handler = vi.fn();
  const { container, rerender } = render(<CalculatingOverlay active={false} label="Working"><button onClick={handler}>Action</button></CalculatingOverlay>);
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  expect(container.firstChild).not.toHaveAttribute("aria-busy");
  expect(screen.getByRole("button").parentElement).not.toHaveAttribute("inert");
  fireEvent.click(screen.getByRole("button"));
  expect(handler).toHaveBeenCalledTimes(1);
  handler.mockClear();
  rerender(<CalculatingOverlay active label="Working"><button onClick={handler}>Action</button></CalculatingOverlay>);
  expect(container.firstChild).toHaveAttribute("aria-busy", "true");
  expect(screen.getByRole("button").parentElement).toHaveAttribute("inert");
  const layer = screen.getByRole("status");
  expect(layer).toHaveTextContent("Working");
  expect(layer).toHaveAttribute("aria-live", "polite");
  expect(layer).toHaveClass("absolute", "inset-0", "z-10");
  expect(layer).not.toHaveClass("pointer-events-none");
  expect(layer.style.pointerEvents).not.toBe("none");
  expect(window.getComputedStyle(layer).pointerEvents).not.toBe("none");
});
