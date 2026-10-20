import type { Metadata } from "next";
import { getRouteLocale } from "@/lib/seo-server";
import { buildPageMetadata } from "@/lib/seo";
import { whatIfExample } from "../_lib/example-what-if";
import { WhatIfGuide } from "../_components/what-if-guide";
export async function generateMetadata(): Promise<Metadata> { return buildPageMetadata("guideWhatIf", (await getRouteLocale()) ?? "en"); }
export default function Page() { return <WhatIfGuide example={whatIfExample()} />; }
