"use client";

// Styled wrappers around Base UI's Menu primitive (already a dependency via
// the shadcn-for-React-19 setup) so feature code composes accessible menus
// without repeating positioning/popup classes. Keyboard nav, Esc, outside-
// click and focus management come from the primitive itself.
import Link from "next/link";
import { Check } from "lucide-react";
import { Menu } from "@base-ui/react/menu";

import { cn } from "@/lib/utils";

export function MenuDropdown({
  trigger,
  children,
  triggerClassName,
  disabled,
}: {
  trigger: React.ReactNode;
  children: React.ReactNode;
  triggerClassName?: string;
  disabled?: boolean;
}) {
  return (
    <Menu.Root>
      <Menu.Trigger
        disabled={disabled}
        className={cn(
          "inline-flex shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground transition-all outline-none select-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:opacity-50",
          triggerClassName,
        )}
      >
        {trigger}
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Positioner
          align="end"
          sideOffset={8}
          className="z-30 outline-none"
        >
          <Menu.Popup className="min-w-52 rounded-lg border border-border bg-card p-1 text-card-foreground shadow-lg">
            {children}
          </Menu.Popup>
        </Menu.Positioner>
      </Menu.Portal>
    </Menu.Root>
  );
}

export function MenuItemLink({
  href,
  children,
}: {
  href: string;
  children: React.ReactNode;
}) {
  return (
    <Menu.LinkItem
      href={href}
      closeOnClick
      render={<Link href={href} />}
      className="flex items-center gap-2 rounded-md px-3 py-2 text-sm outline-none data-[highlighted]:bg-muted data-[highlighted]:text-foreground"
    >
      {children}
    </Menu.LinkItem>
  );
}

export function MenuItemButton({
  onClick,
  disabled,
  children,
}: {
  onClick: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <Menu.Item
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm outline-none",
        disabled
          ? "cursor-not-allowed opacity-50"
          : "cursor-pointer data-[highlighted]:bg-muted data-[highlighted]:text-foreground",
      )}
    >
      {children}
    </Menu.Item>
  );
}

export function MenuSeparator() {
  return <div role="separator" className="my-1 h-px bg-border" />;
}

// Multi-select menu item backed by Base UI's Menu.CheckboxItem (issue #360
// Phase 2: benchmark / dataset-filter dropdowns). Stays open on click
// (closeOnClick defaults to false for CheckboxItem) so a user can toggle
// several options in one pass. The Check indicator renders only while
// checked, so the item's accessible name is just its label.
export function MenuItemCheckbox({
  checked,
  onCheckedChange,
  disabled,
  children,
}: {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <Menu.CheckboxItem
      checked={checked}
      onCheckedChange={onCheckedChange}
      disabled={disabled}
      className={cn(
        "flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm outline-none select-none",
        disabled
          ? "cursor-not-allowed opacity-50"
          : "cursor-pointer data-[highlighted]:bg-muted data-[highlighted]:text-foreground",
      )}
    >
      <Menu.CheckboxItemIndicator className="flex size-4 shrink-0 items-center justify-center">
        <Check aria-hidden="true" className="size-3.5" />
      </Menu.CheckboxItemIndicator>
      {children}
    </Menu.CheckboxItem>
  );
}
