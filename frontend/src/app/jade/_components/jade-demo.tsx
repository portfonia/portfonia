"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { useLocalizedHref } from "@/app/_components/locale-provider";
import { GuideBody } from "@/app/guides/_components/guide-body";
import { Batch2Body } from "@/app/guides/_components/batch2-body";
import { WhatIfGuide } from "@/app/guides/_components/what-if-guide";
import type { Example } from "@/app/guides/_lib/example";
import type { StressExample, StyleExample, TrackingExample } from "@/app/guides/_lib/example-batch2";
import type { WhatIfExample } from "@/app/guides/_lib/example-what-if";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export type JadeDemoData = {
  example: Example;
  stress: StressExample;
  style: StyleExample;
  tracking: TrackingExample;
  whatIf: WhatIfExample;
};

export function JadeDemo({ data }: { data: JadeDemoData }) {
  const t = useTranslations("jade");
  const href = useLocalizedHref();
  const cards = [
    { key: "replay", guide: "replay", figure: <GuideBody kind="replay" example={data.example} figureOnly /> },
    { key: "tailRisk", guide: "var", figure: <GuideBody kind="var" example={data.example} figureOnly /> },
    { key: "stress", guide: "stress", figure: <Batch2Body kind="stress" example={data.stress} figureOnly /> },
    { key: "style", guide: "style", figure: <Batch2Body kind="style" example={data.style} figureOnly /> },
    { key: "tracking", guide: "tracking", figure: <Batch2Body kind="tracking" example={data.tracking} figureOnly /> },
    { key: "whatIf", guide: "what-if", figure: <WhatIfGuide example={data.whatIf} figureOnly /> },
  ];
  return <section className="flex min-w-0 flex-col gap-6">
    <h2 className="font-heading text-2xl">{t("demo.heading")}</h2>
    <p className="rounded-md border p-4 text-sm">{t("demo.notice")}</p>
    {cards.map(card => <Card key={card.key} className="min-w-0">
      <CardHeader><CardTitle>{t(`${card.key}.title`)}</CardTitle></CardHeader>
      <CardContent className="min-w-0 px-4">{card.figure}<Link className="text-sm underline" href={href(`/guides/jade-${card.guide}`)}>{t("guideLink")}</Link></CardContent>
    </Card>)}
  </section>;
}
