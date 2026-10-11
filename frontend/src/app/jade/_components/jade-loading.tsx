"use client";

import { useTranslations } from "next-intl";
import { Loader2 } from "lucide-react";
import { Card, CardContent, CardHeader } from "@/components/ui/card";

export function JadeLoading() {
  const t = useTranslations();
  const common = useTranslations("common");
  return <Card>
    <CardHeader><h1 className="font-heading text-2xl font-medium">{t("jade.title")}</h1></CardHeader>
    <CardContent className="px-4">
      <p role="status" aria-live="polite" className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" aria-hidden="true" />{common("calculating")}
      </p>
    </CardContent>
  </Card>;
}
