import { render, cleanup } from "@testing-library/react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, expect, it } from "vitest";
import { catalogs } from "@/locales";
import { LocaleProvider } from "@/app/_components/locale-provider";
import { localizedPath } from "@/lib/seo";
const locales = ["en","es","zh-Hans","zh-Hant"] as const;
afterEach(cleanup);
it.each(locales)("737 A6 %s server-readable sections figures tables and bottom links",async locale=>{
  const { GuideBody }=await import("./_components/guide-body");const { calculateExample }=await import("./_lib/example");
  for(const kind of ["replay","var"] as const){
    window.history.replaceState(null,"",localizedPath(`/guides/jade-${kind=== 'replay'?'replay':'var'}`,locale));
    const html=renderToStaticMarkup(<LocaleProvider routeLocale={locale}><GuideBody kind={kind} example={calculateExample()}/></LocaleProvider>);
    const doc=new DOMParser().parseFromString(html,"text/html");const copy=catalogs[locale].guides;
    expect(doc.querySelector("h1")?.textContent).toBe(copy[kind].title);
    expect([...doc.querySelectorAll("h2")].map(n=>n.textContent)).toEqual(Object.values(copy[kind].sections).map(s=>s.heading));
    expect(doc.querySelectorAll("table")).toHaveLength(kind==='replay'?1:2);expect(doc.querySelectorAll("figcaption")).toHaveLength(1);
    for(const n of doc.querySelectorAll("caption,figcaption"))expect(n.textContent).toContain(copy.common.exampleLabel);
    expect(doc.querySelector('nav a[href="'+localizedPath('/pricing',locale)+'"]')).not.toBeNull();
    expect(doc.querySelector('nav a[href="'+localizedPath(kind==='replay'?'/guides/jade-var':'/guides/jade-replay',locale)+'"]')).not.toBeNull();
    if(kind==='var'&&locale==='en'){expect(doc.querySelector('[data-cell="portfolio-95-daily-var"]')?.textContent).toBe('1.14%$1,144');}
  }
});
it.each([["replay", "light"], ["replay", "dark"], ["var", "light"], ["var", "dark"]] as const)("737 A12 375px %s %s tables wrap and charts fill parent",async(kind,theme)=>{
 const {GuideBody}=await import('./_components/guide-body');const {calculateExample}=await import('./_lib/example');
 const {container}=render(<LocaleProvider routeLocale="en"><div className={theme} style={{width:375}}><GuideBody kind={kind} example={calculateExample()}/></div></LocaleProvider>);
 for(const table of container.querySelectorAll('table'))expect(table).toHaveClass('table-fixed','w-full');
 expect(container.querySelector('[data-guide-chart]')).toHaveClass('w-full','min-w-0');expect(container.querySelector('[class*="overflow-x"]')).toBeNull();
 expect(container.querySelector('.recharts-responsive-container')).not.toBeNull();
});
