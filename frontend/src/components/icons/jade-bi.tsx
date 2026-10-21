// Single source for the Jade bi mark (issue #818): the header logo, the Jade
// menu rows and the generated favicon (app/icon.tsx) all render this markup.
export const JADE_BI_SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24"><defs><radialGradient id="b" cx="40%" cy="35%" r="70%"><stop offset="0" stop-color="#5fb88a"/><stop offset=".55" stop-color="#2f8a5f"/><stop offset="1" stop-color="#1c5e40"/></radialGradient><filter id="c" x="0" y="0" width="100%" height="100%"><feTurbulence type="fractalNoise" baseFrequency="0.11" numOctaves="2" seed="4"/><feColorMatrix values="0 0 0 0 0.86 0 0 0 0 0.97 0 0 0 0 0.90 0 0 0 1.1 -0.45"/></filter><radialGradient id="s" cx="34%" cy="28%" r="30%"><stop offset="0" stop-color="#ffffff" stop-opacity=".55"/><stop offset="1" stop-color="#ffffff" stop-opacity="0"/></radialGradient><mask id="m"><rect width="24" height="24" fill="#000000"/><circle cx="12" cy="12" r="10.5" fill="#ffffff"/><circle cx="12" cy="12" r="4.3" fill="#000000"/></mask></defs><g mask="url(#m)"><rect width="24" height="24" fill="url(#b)"/><rect width="24" height="24" filter="url(#c)" opacity=".55"/><rect width="24" height="24" fill="url(#s)"/></g><circle cx="12" cy="12" r="10.3" fill="none" stroke="#123f2a" stroke-opacity=".45" stroke-width=".5"/><circle cx="12" cy="12" r="4.5" fill="none" stroke="#123f2a" stroke-opacity=".5" stroke-width=".5"/></svg>`;

export const JADE_BI_DATA_URI = `data:image/svg+xml,${encodeURIComponent(JADE_BI_SVG)}`;

export function JadeBi({ className }: { className?: string }) {
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img src={JADE_BI_DATA_URI} alt="" aria-hidden="true" className={className} />
  );
}
