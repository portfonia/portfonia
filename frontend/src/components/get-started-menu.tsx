"use client";

// Single account/navigation entry point in the top bar (issue #207). Content
// comes from a small registry gated on session status: a logged-out visitor
// sees ONLY Log in; authenticated users see the app entries, their own email,
// and Log out. Future entries (Settings, Vigil) are one new row in
// AUTHED_ENTRIES once their routes ship.
//
// issue #209: labels used to branch on `isHome` between two parallel label
// sets (home-messages.ts's `nav.*` vs this file's own English-only
// `messages.menu.*`) — that split was the root cause of the mixed-language
// menu bug (issue #207/PR #208: Get Started/Login/Logout came from
// home-messages, Holdings came from the English-only map). Now there is
// exactly one `menu` namespace in the shared catalog, read the same way on
// every route.
import { useState } from "react";
import { Briefcase, ChevronDown, ClipboardList, LogIn, LogOut, Pencil, User } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useTranslations } from "next-intl";

import {
  useSession,
  markOptimisticLogout,
  revalidateSession,
} from "@/hooks/use-session";
import { useIdleLogout } from "@/hooks/use-idle-logout";
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
// "Profile" is first (issue #220 — replaces the #214-follow-up placeholder
// "Home" entry; the way back to "/" is the brand-link click only now, no
// second entry for it).
const AUTHED_ENTRIES = [
  { id: "profile", href: "/profile", Icon: User },
  { id: "holdings", href: "/holdings", Icon: Briefcase },
  { id: "editHoldings", href: "/holdings/edit", Icon: Pencil },
  { id: "questionnaire", href: "/questionnaire", Icon: ClipboardList },
] as const satisfies { id: string; href: string; Icon: LucideIcon }[];

export function GetStartedMenu() {
  const t = useTranslations("menu");
  const session = useSession();
  const [logoutFailed, setLogoutFailed] = useState(false);

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
      {t("logoutFailed")}
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
          <span className="text-sm text-foreground/60">{t("loggingIn")}</span>
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
            {t("trigger")}
            <ChevronDown className="size-4 opacity-80" />
          </>
        }
      >
        {session.status === "guest" && (
          <MenuItemLink href="/login">
            <LogIn aria-hidden="true" className="size-4" />
            {t("login")}
          </MenuItemLink>
        )}

        {session.status === "authed" && (
          <>
            {AUTHED_ENTRIES.map(({ id, href, Icon }) => (
              <MenuItemLink key={id} href={href}>
                <Icon aria-hidden="true" className="size-4" />
                {t(id)}
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
              <LogOut aria-hidden="true" className="size-4" />
              {t("logout")}
            </MenuItemButton>
          </>
        )}
      </MenuDropdown>
    </div>
  );
}
