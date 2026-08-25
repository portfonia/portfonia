"use client";

import Link from "next/link";

import { useHomeMessages, useLocale } from "./locale-provider";

const CARD_ICONS = [
  <path key="ingest" d="M12 3v12m0 0l-4-4m4 4l4-4M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2" />,
  <path key="track" d="M3 12h4l2-7 4 14 2-7h6" />,
  <path key="brief" d="M4 4h16v16H4z M4 9h16 M9 9v11" />,
];

const TIER_KEYS = ["established", "probable", "speculative"] as const;

// Renders **bold** spans inside i18n table cells without pulling in a full
// markdown renderer.
function BoldText({ text }: { text: string }) {
  const parts = text.split("**");
  return (
    <>
      {parts.map((part, i) =>
        i % 2 === 1 ? <strong key={i} className="font-semibold text-foreground">{part}</strong> : <span key={i}>{part}</span>,
      )}
    </>
  );
}

const TABLE_CLS = "w-full min-w-[36rem] border-collapse text-left text-xs";
const TH_CLS =
  "whitespace-nowrap border-b border-white/15 px-2 py-2 font-mono font-medium uppercase tracking-wide text-foreground/50";
const TD_CLS = "border-b border-white/5 px-2 py-1.5 align-top text-foreground/70";

export function HomeSections() {
  const t = useHomeMessages();
  const { locale } = useLocale();

  return (
    <>
      <section id="top" className="relative overflow-hidden px-6 pb-16 pt-20 sm:pt-28">
        <div
          aria-hidden="true"
          className="hero-pulse pointer-events-none absolute left-1/2 top-[-220px] h-[900px] w-[900px] -translate-x-1/2 rounded-full"
          style={{
            background:
              "radial-gradient(circle, rgba(220,170,74,0.10) 0%, rgba(220,170,74,0.035) 38%, transparent 68%)",
          }}
        />
        <div className="relative mx-auto max-w-3xl">
          <div className="mb-5 inline-flex items-center gap-2 font-mono text-xs uppercase tracking-widest text-brand">
            <span className="h-1.5 w-1.5 rounded-full bg-brand" />
            {t.hero.eyebrow}
          </div>
          <h1 className="max-w-[15ch] font-serif text-4xl leading-[1.08] sm:text-6xl">
            {t.hero.titleLine1}
            <br />
            <em className={locale === "en" ? "text-brand italic" : "text-brand not-italic"}>
              {t.hero.titleAccent}
            </em>
          </h1>
          <p className="mt-6 max-w-[46ch] text-lg leading-relaxed text-foreground/70">
            {t.hero.sub}
          </p>
          <div className="mt-9 flex flex-wrap items-center gap-4">
            <Link
              href="/holdings"
              className="rounded-md bg-primary px-6 py-3 text-sm font-semibold text-primary-foreground"
            >
              {t.hero.ctaPrimary}
            </Link>
            <a href="#how" className="rounded-md border border-white/10 px-6 py-3 text-sm font-semibold">
              {t.hero.ctaSecondary} →
            </a>
          </div>
        </div>
      </section>

      <section id="how" className="px-6 py-16 sm:py-20">
        <div className="mx-auto max-w-4xl">
          <div className="mb-10 flex items-baseline justify-between gap-6 border-b border-white/10 pb-5">
            <h2 className="font-serif text-2xl sm:text-3xl">{t.how.heading}</h2>
            <span className="whitespace-nowrap font-mono text-xs uppercase tracking-wide text-foreground/45">
              {t.how.tag}
            </span>
          </div>

          <div className="grid gap-4 sm:grid-cols-3">
            {t.how.cards.map((card, i) => (
              <div
                key={card.title}
                className="flex flex-col gap-3 rounded-xl border border-white/10 bg-card p-6 shadow-[0_25px_50px_-22px_rgba(0,0,0,0.55)]"
              >
                <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-secondary text-brand">
                  <svg
                    aria-hidden="true"
                    width="16"
                    height="16"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.6"
                  >
                    {CARD_ICONS[i]}
                  </svg>
                </div>
                <h3 className="font-serif text-lg">{card.title}</h3>
                <p className="text-sm leading-relaxed text-foreground/70">{card.body}</p>
                <div className="mt-auto flex flex-wrap gap-2 border-t border-white/10 pt-3">
                  {card.tags.map((tag) => (
                    <span
                      key={tag}
                      className="rounded-full bg-secondary px-2.5 py-1 font-mono text-[11px] text-foreground/50"
                    >
                      {tag}
                    </span>
                  ))}
                </div>
              </div>
            ))}
          </div>

          <div className="mt-4 flex flex-wrap items-center justify-between gap-6 rounded-xl border border-white/10 bg-card p-6">
            <p className="max-w-[34ch] text-sm text-foreground/70">
              <strong className="font-semibold text-foreground">{t.how.confidenceLead}</strong>{" "}
              — {t.how.confidenceBody}
            </p>
            <div className="flex flex-wrap gap-2.5">
              {TIER_KEYS.map((key) => (
                <span
                  key={key}
                  className="inline-flex items-center gap-1.5 rounded-full border border-white/10 px-3 py-1.5 font-mono text-xs"
                >
                  <span
                    className="h-1.5 w-1.5 rounded-full"
                    style={{ backgroundColor: `var(--tier-${key})` }}
                  />
                  {t.how.tiers[key]}
                </span>
              ))}
            </div>
          </div>
        </div>
      </section>

      <section id="preview" className="px-6 py-16 sm:py-20">
        <div className="mx-auto max-w-4xl">
          <div className="mb-10 flex items-baseline justify-between gap-6 border-b border-white/10 pb-5">
            <h2 className="font-serif text-2xl sm:text-3xl">{t.preview.heading}</h2>
            <span className="whitespace-nowrap font-mono text-xs uppercase tracking-wide text-foreground/45">
              {t.preview.tag}
            </span>
          </div>

          <div className="rounded-2xl border border-dashed border-white/15 bg-card p-6 sm:p-8">
            <div className="mb-6 inline-flex items-center gap-2 rounded-full bg-secondary px-3 py-1 font-mono text-[11px] uppercase tracking-wide text-foreground/55">
              <span className="h-1.5 w-1.5 rounded-full bg-foreground/40" />
              {t.preview.badge}
            </div>

            {/* §1 Portfolio snapshot */}
            <h3 className="mb-3 font-serif text-base">{t.preview.snapshotTitle}</h3>
            <p className="mb-4 text-sm font-medium text-foreground/80">{t.preview.totalLine}</p>
            <div className="mb-6 overflow-x-auto">
              <table className={TABLE_CLS}>
                <thead>
                  <tr>
                    {t.preview.holdingsColumns.map((col) => (
                      <th key={col} className={TH_CLS}>{col}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {t.preview.holdingsRows.map((row, i) => (
                    <tr key={`holding-${i}`}>
                      {row.map((cell, j) => (
                        <td key={`holding-${i}-${j}`} className={TD_CLS}>
                          {j === 0 ? (
                            <span className="text-xs leading-snug"><BoldText text={cell} /></span>
                          ) : (
                            cell
                          )}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="mb-7 space-y-1.5 border-t border-white/10 pt-5 text-sm leading-relaxed text-foreground/70">
              {t.preview.distributionLines.map((line) => (
                <p key={line}><BoldText text={line} /></p>
              ))}
            </div>

            {/* §2 Macro signals */}
            <div className="space-y-2 border-t border-white/10 pt-6">
              <h3 className="font-serif text-base">{t.preview.macroTitle}</h3>
              <p className="text-sm leading-relaxed text-foreground/70">{t.preview.macroBody}</p>
            </div>

            {/* §2.5 Forward calendar */}
            <div className="mt-6 space-y-3 border-t border-white/10 pt-6">
              <h3 className="font-serif text-base">{t.preview.calendarTitle}</h3>
              <p className="text-sm leading-relaxed text-foreground/70">{t.preview.calendarNote}</p>
              <div className="overflow-x-auto">
                <table className={TABLE_CLS}>
                  <thead>
                    <tr>
                      {t.preview.calendarColumns.map((col) => (
                        <th key={col} className={TH_CLS}>{col}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {t.preview.calendarRows.map((row, i) => (
                      <tr key={`cal-${i}`}>
                        {row.map((cell, j) => (
                          <td key={`cal-${i}-${j}`} className={TD_CLS}>{cell}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            {/* §3 Holding analysis */}
            <div className="mt-6 space-y-2 border-t border-white/10 pt-6">
              <h3 className="font-serif text-base">{t.preview.analysisTitle}</h3>
              <p className="text-sm font-medium text-foreground/85">{t.preview.analysisHeading}</p>
              <p className="text-sm leading-relaxed text-foreground/70">{t.preview.analysisBody}</p>
            </div>

            {/* §4 Risk radar */}
            <div className="mt-6 space-y-3 border-t border-white/10 pt-6">
              <h3 className="font-serif text-base">{t.preview.radarTitle}</h3>
              <p className="text-sm leading-relaxed text-foreground/70">{t.preview.concentrationBody}</p>
              <div className="overflow-x-auto">
                <table className={TABLE_CLS}>
                  <thead>
                    <tr>
                      {t.preview.anomalyColumns.map((col) => (
                        <th key={col} className={TH_CLS}>{col}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {t.preview.anomalyRows.map((row, i) => (
                      <tr key={`anom-${i}`}>
                        {row.map((cell, j) => (
                          <td key={`anom-${i}-${j}`} className={TD_CLS}>{cell}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <ul className="space-y-1.5">
                {t.preview.anomalyBullets.map((bullet) => (
                  <li
                    key={bullet}
                    className="flex items-start gap-2.5 text-sm leading-relaxed text-foreground/70"
                  >
                    <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-brand/70" />
                    <span><BoldText text={bullet} /></span>
                  </li>
                ))}
              </ul>
              <div className="overflow-x-auto">
                <table className={TABLE_CLS}>
                  <thead>
                    <tr>
                      {t.preview.technicalColumns.map((col) => (
                        <th key={col} className={TH_CLS}>{col}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {t.preview.technicalRows.map((row, i) => (
                      <tr key={`tech-${i}`}>
                        {row.map((cell, j) => (
                          <td key={`tech-${i}-${j}`} className={TD_CLS}>{cell}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="whitespace-pre-line text-xs text-foreground/50">
                <BoldText text={t.preview.technicalNote.replace(/^> /, "")} />
              </div>
            </div>

            <p className="mt-6 border-t border-white/10 pt-5 text-sm text-foreground/60">
              {t.preview.footnote}
            </p>
          </div>
        </div>
      </section>

      <section id="boundary" className="px-6 py-16 sm:py-20">
        <div className="mx-auto max-w-4xl rounded-2xl border border-white/10 bg-card p-7 sm:p-11">
          <div className="mb-7 flex flex-wrap items-start justify-between gap-8">
            <h2 className="max-w-[18ch] font-serif text-2xl sm:text-3xl">{t.boundary.heading}</h2>
            <p className="max-w-[40ch] text-sm leading-relaxed text-foreground/70">{t.boundary.body}</p>
          </div>
          <ul className="grid gap-x-10 sm:grid-cols-2">
            {t.boundary.items.map((item) => (
              <li key={item} className="flex gap-3.5 border-t border-white/10 py-4 text-sm leading-snug text-foreground/70">
                <span className="shrink-0 font-mono text-foreground/40">—</span>
                {item}
              </li>
            ))}
          </ul>
        </div>
      </section>

      <section id="faq" className="px-6 py-16 sm:py-20">
        <div className="mx-auto max-w-4xl">
          <div className="mb-10 flex items-baseline justify-between gap-6 border-b border-white/10 pb-5">
            <h2 className="font-serif text-2xl sm:text-3xl">{t.faq.heading}</h2>
            <span className="whitespace-nowrap font-mono text-xs uppercase tracking-wide text-foreground/45">
              {t.faq.tag}
            </span>
          </div>
          <dl className="grid gap-x-10 gap-y-8 sm:grid-cols-2">
            {t.faq.items.map((item) => (
              <div key={item.q}>
                <dt className="mb-1.5 font-serif text-base">{item.q}</dt>
                <dd className="text-sm leading-relaxed text-foreground/70">{item.a}</dd>
              </div>
            ))}
          </dl>
        </div>
      </section>

      <section className="px-6 py-10">
        <div className="mx-auto flex max-w-4xl items-center gap-3.5 text-sm text-foreground/45">
          <span
            className="h-2 w-2 shrink-0 rounded-full bg-brand"
            style={{ boxShadow: "0 0 0 3px rgba(220,170,74,0.14)" }}
          />
          {t.status}
        </div>
      </section>

      <footer className="border-t border-white/10 px-6 py-10">
        <div className="mx-auto flex max-w-4xl flex-wrap items-center justify-between gap-5">
          <span className="font-serif text-base text-foreground/70">Portfonia</span>
          <span className="text-right font-mono text-xs leading-relaxed text-foreground/45">
            {t.footer.stack1}
            <br />
            {t.footer.stack2}
          </span>
        </div>
      </footer>
    </>
  );
}
