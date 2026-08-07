import type { Metadata } from "next";

import { HomeNav } from "./_components/home-nav";
import { HomeSections } from "./_components/home-sections";
import { HomeShell } from "./_components/home-shell";
import { LocaleProvider } from "./_components/locale-provider";

export const metadata: Metadata = {
  title: "Portfonia — What's worth noticing",
};

export default function HomePage() {
  return (
    <LocaleProvider>
      <HomeShell>
        <HomeNav />
        <HomeSections />
      </HomeShell>
    </LocaleProvider>
  );
}
