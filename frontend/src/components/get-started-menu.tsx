"use client";

// Single account/navigation entry point in the top bar (issue #207). Content
// comes from a small registry gated on session status: a logged-out visitor
// sees ONLY Log in; authenticated users see the app entries, their own email,
// and Log out. Future entries (B6 questionnaire, Settings, Vigil) are one new
// row in MENU_ENTRIES once their routes ship.
import { usePathname } from "next/navigation";
import { ChevronDown } from "lucide-react";

import { useHomeMessages } from "@/app/_components/locale-provider";
import { useSession, markOptimisticLogout } from "@/hooks/use-session";
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
// "Home" is first — an explicit way back to "/" from any inner page, on top
// of the brand-link click target (issue #214 follow-up).
//
// `homeNavKey` names the matching HomeMessages.nav field for entries with a
// zh translation on the home route (PR #215 review — the Home entry
// originally fell back to the English-only messages.menu map even on the
// home route, same gap as questionnaire already had, just more visible
// since Home is now the first item).
const AUTHED_ENTRIES = [
  { id: "home", href: "/", homeNavKey: "home" },
  { id: "holdings", href: "/holdings", homeNavKey: "holdings" },
  // B6 (issue #129 checkpoint B6): the one new row this file's own header
  // comment predicted. No zh translation yet (messages.ts is English-only
  // outside the home route), so no homeNavKey.
  { id: "questionnaire", href: "/questionnaire" },
] as const;

export function GetStartedMenu() {
  const pathname = usePathname();
  const isHome = pathname === "/";
  const t = useHomeMessages();
  const session = useSession();
  // Idle auto-logout (R6): reuses the same Server Action as manual logout,
  // tagged so /login can show the expired-session banner.
  useIdleLogout(session.status, (reason) => void logout(reason));

  // Render nothing until the verified session check resolves — avoids a
  // login/logout flash on first paint. The one exception: right after a
  // login redirect, markPendingLogin() tags this as a known-reason wait, so
  // show something instead of a blank spot where the menu used to be
  // (issue #214 follow-up — a real user report of "clicked, nothing
  // happened" on the login path, not just logout).
  if (session.status === "checking") {
    if (session.pendingReason === "login") {
      return (
        <span className="text-sm text-foreground/60">{messages.menu.loggingIn}</span>
      );
    }
    return null;
  }

  const triggerLabel = isHome ? t.nav.menu : messages.menu.trigger;
  const loginLabel = isHome ? t.nav.login : messages.menu.login;
  const logoutLabel = isHome ? t.nav.logout : messages.menu.logout;

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
              {isHome && "homeNavKey" in entry
                ? t.nav[entry.homeNavKey]
                : messages.menu[entry.id]}
            </MenuItemLink>
          ))}
          <MenuSeparator />
          <div
            className="truncate px-3 py-2 text-xs text-foreground/60"
            title={session.email}
          >
            {session.email}
          </div>
          <MenuItemButton
            onClick={() => {
              // Signing out is the user's own explicit action, not something
              // that can fail from the UI's perspective — flip immediately
              // instead of waiting on a verified getUser() round-trip over
              // the auth.portfonia.com proxy (issue #214: a real user report
              // of "clicked Log out, nothing happened for a while").
              markOptimisticLogout();
              void logout();
            }}
          >
            {logoutLabel}
          </MenuItemButton>
        </>
      )}
    </MenuDropdown>
  );
}
