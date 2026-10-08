import { getRouteLocale } from "@/lib/seo-server";
import type { Metadata } from "next";
import { buildPageMetadata } from "@/lib/seo";
import { AboutBody } from "./about-body";
export async function generateMetadata(): Promise<Metadata> {
  return buildPageMetadata("about", (await getRouteLocale()) ?? "en");
}
export default function AboutPage() { return <AboutBody />; }
