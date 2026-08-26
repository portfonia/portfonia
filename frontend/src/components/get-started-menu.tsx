"use client";

// Single account/navigation entry point in the top bar (issue #207). Content
// comes from a small registry gated on session status: a logged-out visitor
// sees ONLY Log in; authenticated users see the app entries, their own email,
// and Log out. Future entries (B6 questionnaire, Settings, Vigil) are one new
// row in MENU_ENTRIES once their routes ship.
import { usePathname } from "next/navigation";
import { useState } from "react";
import { ChevronDown } from "lucide-react";

import { useHomeMessages } from "@/app/_components/locale-provider";
import {
  useSession,
  markOptimisticLogout,
  revalidateSession,
} from "@/hooks/use-session";
import { useIdleLogout } from "@/hooks/use-idle-logout";
import { messages } from "@/lib/messages";
import { isNextRedirectError } from "@/lib/next-redirect-error";
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
  const [logoutFailed, setLogoutFailed] = useState(false);

  const triggerLabel = isHome ? t.nav.menu : messages.menu.trigger;
  const loginLabel = isHome ? t.nav.login : messages.menu.login;
  const logoutLabel = isHome ? t.nav.logout : messages.menu.logout;
  const loggingInLabel = isHome ? t.nav.loggingIn : messages.menu.loggingIn;
  const logoutFailedLabel = isHome ? t.nav.logoutFailed : messages.menu.logoutFailed;

  const runLogout = (reason?: string) => {
    // Manual Log out must call logout() with no args (the idle hook passes
    // "expired"). Promise.resolve wraps the Server Action so a test mock
    // that returns undefined still has a .catch path.
    const result = reason === undefined ? logout() : logout(reason);
    void Promise.resolve(result).catch((err: unknown) => {
      if (isNextRedirectError(err)) return;
      revalidateSession();
      setLogoutFailed(true);
    });
  };

  // Idle auto-logout (R6): reuses the same Server Action as manual logout,
  // tagged so /login can show the expired-session banner.
  useIdleLogout(session.status, runLogout);

  const errorNotice = logoutFailed ? (
    <span role="alert" className="text-xs text-destructive">
      {logoutFailedLabel}
    </span>
  ) : null;

  // Render nothing until the verified session check resolves — avoids a
  // login/logout flash on first paint. The one exception: right after a
  // login redirect, markPendingLogin() tags this as a known-reason wait, so
  // show something instead of a blank spot where the menu used to be
  // (issue #214 follow-up — a real user report of "clicked, nothing
  // happened" on the login path, not just logout).
  if (session.status === "checking") {
    if (session.pendingReason === "login") {
      return (
        <div className="flex items-center gap-2">
          {errorNotice}
          <span className="text-sm text-foreground/60">{loggingInLabel}</span>
        </div>
      );
    }
    if (errorNotice) {
      return errorNotice;
    }
    return null;
  }

  return (
    <div className="flex items-center gap-2">
      {errorNotice}
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
                // Flip immediately so the click is not gated on the
                // auth.portfonia.com round-trip (issue #214). If logout()
                // then fails, runLogout revalidates and surfaces an error
                // so the UI does not keep claiming the session is gone.
                setLogoutFailed(false);
                markOptimisticLogout();
                runLogout();
              }}
            >
              {logoutLabel}
            </MenuItemButton>
          </>
        )}
      </MenuDropdown>
    </div>
  );
}
