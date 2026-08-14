import type { Metadata } from "next";

import { HomeSections } from "./_components/home-sections";

export const metadata: Metadata = {
  title: "Portfonia — What's worth noticing",
};

export default function HomePage() {
  return <HomeSections />;
}
