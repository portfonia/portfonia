import type { Metadata } from "next";
import { getRouteLocale } from "@/lib/seo-server";
import { buildPageMetadata } from "@/lib/seo";
import { GuidesIndex } from "./_components/guides-index";
export async function generateMetadata(): Promise<Metadata> { return buildPageMetadata("guides", (await getRouteLocale()) ?? "en"); }
export default function Page() { return <GuidesIndex />; }
