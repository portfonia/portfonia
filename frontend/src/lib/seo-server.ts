import { headers } from "next/headers";
import { isLocale, type Locale } from "@/locales";
import { ROUTE_LOCALE_HEADER } from "./seo";

export async function getRouteLocale(): Promise<Locale | null> {
  const locale = (await headers()).get(ROUTE_LOCALE_HEADER);
  return locale && isLocale(locale) ? locale : null;
}
