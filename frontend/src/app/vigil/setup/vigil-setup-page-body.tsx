"use client";

import { useTranslations } from "next-intl";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

// Protected placeholder (issue #453) — real configuration/upload UI ships
// in #454/#455 once the backend has anywhere to persist it. No form
// fields, no fetch: this page exists now so the route and its auth
// boundary are real before the feature behind it is.
export function VigilSetupPageBody() {
  const t = useTranslations("vigil");

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("title")}</CardTitle>
      </CardHeader>
      <CardContent>
        <p>{t("setupPagePlaceholder")}</p>
      </CardContent>
    </Card>
  );
}
