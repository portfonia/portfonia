"use client";

import { useTranslations } from "next-intl";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

// #529 (#516 finding 17): revoke product behavior ships with #462, not
// here. Until then this route stays public and reachable (proxy.ts /
// SiteHeader still list it, so a direct link 404s nowhere) but renders one
// static not-enabled note instead of a shell that pretends the product
// exists — no PublicActionShell, no token/altcha/confirm plumbing.
export default function VigilRevokePage() {
  const t = useTranslations("vigil.public");
  return (
    <main className="mx-auto w-full max-w-2xl px-6 py-10">
      <Card>
        <CardHeader>
          <CardTitle>{t("revokeTitle")}</CardTitle>
        </CardHeader>
        <CardContent>
          <p>{t("notEnabled")}</p>
        </CardContent>
      </Card>
    </main>
  );
}
