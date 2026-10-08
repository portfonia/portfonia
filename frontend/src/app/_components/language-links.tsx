"use client";

import { Fragment } from "react";
import { usePathname } from "next/navigation";
import { LOCALES } from "@/locales";
import { isSeoPath, localizedPath, splitLocalePrefix } from "@/lib/seo";
import { useLocale } from "./locale-provider";

export function LanguageLinks() {
  const pathname = usePathname();
  const { locale, rememberLocale } = useLocale();
  const { path } = splitLocalePrefix(typeof window === "undefined" ? pathname : window.location.pathname);
  if (!isSeoPath(path)) return null;

  return (
    <div className="mt-6 flex flex-wrap justify-center gap-4 text-sm text-foreground/60">
      {LOCALES.map((option, index) => <Fragment key={option.value}>
        {index > 0 && <span aria-hidden="true">·</span>}
        {option.value === locale ? (
        <span aria-current="page">{option.label}</span>
      ) : (
        <a href={localizedPath(path, option.value)} onClick={() => rememberLocale(option.value)} className="underline underline-offset-2">
          {option.label}
        </a>
      )}
      </Fragment>)}
    </div>
  );
}
