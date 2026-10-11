import type { Metadata } from "next";
import { getRouteLocale } from "@/lib/seo-server";
import { buildPageMetadata } from "@/lib/seo";
import { styleExample } from "../_lib/example-batch2";
import { Batch2Body } from "../_components/batch2-body";
export async function generateMetadata(): Promise<Metadata> { return buildPageMetadata("guideStyle", (await getRouteLocale()) ?? "en"); }
export default function Page() { return <Batch2Body kind="style" example={styleExample()} />; }
