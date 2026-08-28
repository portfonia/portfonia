// Type declaration for the <altcha-widget> custom element (issue #231).
// The widget JS is loaded at runtime from public/altcha.js (self-hosted,
// see that file's header comment) — this file only teaches JSX about the
// element's tag and the small set of attributes forgot-password-form.tsx
// actually uses. Full attribute list: frontend/node_modules/altcha/README.md.
import type { DetailedHTMLProps, HTMLAttributes } from "react";

declare module "react" {
  namespace JSX {
    interface IntrinsicElements {
      "altcha-widget": DetailedHTMLProps<HTMLAttributes<HTMLElement>, HTMLElement> & {
        challengeurl?: string;
        name?: string;
        hidefooter?: boolean;
        hidelogo?: boolean;
      };
    }
  }
}

export {};
