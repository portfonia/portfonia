"use client";

import { useEffect } from "react";

import { useLocale } from "@/app/_components/locale-provider";
import { isLocale } from "@/locales";

export function SignupLang({ initialLang }: { initialLang: string | null }) {
  const { setLocale } = useLocale();

  useEffect(() => {
    if (initialLang && isLocale(initialLang)) setLocale(initialLang);
    // Apply only the initial signup link locale, not subsequent provider renders.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return null;
}
