"use client";

import { useTranslations } from "next-intl";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useConsumeLinkToken } from "@/hooks/use-consume-link-token";

export type PublicVigilAction = "confirm" | "retrieve" | "revoke";

const TITLE_KEYS: Record<PublicVigilAction, string> = {
  confirm: "confirmTitle",
  retrieve: "retrieveTitle",
  revoke: "revokeTitle",
};

// Scaffolding only (issue #453): the real /vigil/public/* endpoints and
// their confirm/retrieve/revoke business logic don't exist until #460-462.
// This shell exists now so the route, its exemption from proxy.ts's
// Supabase lookup, and the no-GetStartedMenu header shape (site-header.tsx)
// are all correct before any real action lands here. It still consumes and
// strips the URL fragment token (useConsumeLinkToken) per Design section 6,
// even though nothing acts on the value yet — the currently-unused
// variable is intentional: replacing it with a real POST is a later
// checkpoint's change, not a rewrite of the URL-handling contract.
export function PublicActionShell({ action }: { action: PublicVigilAction }) {
  const t = useTranslations("vigil.public");
  useConsumeLinkToken();

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t(TITLE_KEYS[action])}</CardTitle>
      </CardHeader>
      <CardContent>
        <p>{t("unavailable")}</p>
      </CardContent>
    </Card>
  );
}
