"use client";

// Single account/navigation entry point in the top bar (issue #207). Content
// comes from a small registry gated on session status: a logged-out visitor
// sees ONLY Log in; authenticated users see the app entries, their own email,
// and Log out. Future entries (B6 questionnaire, Settings, Vigil) are one new
// row in MENU_ENTRIES once their routes ship.
import { usePathname } from "next/navigation";
import { ChevronDown } from "lucide-react";

import { useHomeMessages } from "@/app/_components/locale-provider";
import { useSession } from "@/hooks/use-session";
import { useIdleLogout } from "@/hooks/use-idle-logout";
import { messages } from "@/lib/messages";
import { logout } from "@/lib/auth-actions";

import {
  MenuDropdown,
  MenuItemButton,
  MenuItemLink,
  MenuSeparator,
} from "@/components/ui/menu";

// Entries shown only to authenticated users, in display order. Signup is
// deliberately absent: closed beta, no self-serve registration (OQ-3).
const AUTHED_ENTRIES = [{ id: "holdings", href: "/holdings" }] as const;

export function GetStartedMenu() {
  const pathname = usePathname();
  const isHome = pathname === "/";
  const t = useHomeMessages();
  const session = useSession();
  // Idle auto-logout (R6): reuses the same Server Action as manual logout,
  // tagged so /login can show the expired-session banner.
  useIdleLogout(session.status, (reason) => void logout(reason));

  // Render nothing until the verified session check resolves — avoids a
  // login/logout flash on first paint.
  if (session.status === "checking") return null;

  const triggerLabel = isHome ? t.nav.menu : messages.menu.trigger;
  const loginLabel = isHome ? t.nav.login : messages.menu.login;
  const logoutLabel = isHome ? t.nav.logout : messages.menu.logout;
  const holdingsLabel = isHome ? t.nav.holdings : messages.menu.holdings;

  return (
    <MenuDropdown
      trigger={
        <>
          {triggerLabel}
          <ChevronDown className="size-4 opacity-80" />
        </>
      }
    >
      {session.status === "guest" && <MenuItemLink href="/login">{loginLabel}</MenuItemLink>}

      {session.status === "authed" && (
        <>
          {AUTHED_ENTRIES.map((entry) => (
            <MenuItemLink key={entry.id} href={entry.href}>
              {entry.id === "holdings" ? holdingsLabel : messages.menu[entry.id]}
            </MenuItemLink>
          ))}
          <MenuSeparator />
          <div
            className="truncate px-3 py-2 text-xs text-foreground/60"
            title={session.email}
          >
            {session.email}
          </div>
          <MenuItemButton onClick={() => void logout()}>{logoutLabel}</MenuItemButton>
        </>
      )}
    </MenuDropdown>
  );
}
