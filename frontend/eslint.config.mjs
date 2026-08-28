import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";
import i18next from "eslint-plugin-i18next";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  {
    // Structural guard for issue #209's catalog mechanism: fail a PR that
    // introduces a hardcoded user-facing string literal in source under
    // app/ or components/, instead of a key from src/locales/*.json. Scoped
    // to those two dirs (not the whole of src/) so it never flags the
    // catalog files themselves, lib/ utilities, or hooks — none of which
    // render JSX.
    files: ["src/app/**/*.{ts,tsx}", "src/components/**/*.{ts,tsx}"],
    ignores: ["**/*.test.{ts,tsx}"],
    plugins: { i18next },
    rules: {
      "i18next/no-literal-string": [
        "error",
        {
          // These attributes never carry user-facing copy in this codebase
          // (styling hooks, DOM wiring, HTML semantics, technical values) —
          // flagging them would be pure noise, not a real i18n gap.
          ignoreAttribute: [
            "className",
            "id",
            "htmlFor",
            "type",
            "name",
            "href",
            "rel",
            "key",
            "variant",
            "size",
            "accept",
            "autoComplete",
            "colSpan",
            "minLength",
            "role",
            "aria-hidden",
            "data-next-link",
            "data-testid",
            "viewBox",
            "fill",
            "stroke",
            "strokeWidth",
            "width",
            "height",
            "d",
          ],
        },
      ],
    },
  },
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
    // Vendored third-party widget bundle (issue #231) — self-hosted so
    // /forgot-password never loads it from an external CDN, but it's
    // minified upstream code we don't own and don't want lint findings
    // against. See public/altcha.js's own header comment for provenance.
    "public/altcha.js",
  ]),
]);

export default eslintConfig;
