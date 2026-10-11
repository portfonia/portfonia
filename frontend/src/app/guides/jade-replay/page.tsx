import type { Metadata } from "next";
import { getRouteLocale } from "@/lib/seo-server";
import { buildPageMetadata } from "@/lib/seo";
import { calculateExample } from "../_lib/example";
import { GuideBody } from "../_components/guide-body";
export async function generateMetadata(): Promise<Metadata> { return buildPageMetadata("guideReplay", (await getRouteLocale()) ?? "en"); }
export default function Page() { return <GuideBody kind="replay" example={calculateExample()} />; }
