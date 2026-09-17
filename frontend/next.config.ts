import type { NextConfig } from "next";

// Ring 0: proxy /api/* to the local FastAPI backend so the browser talks to a
// single same-origin host (no CORS). The frontend never touches the DB directly.
const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

// Issue #453 / #450 Design section 5: the three public Vigil action shells
// carry a scoped link token and must never be cached or leaked via
// Referrer. blacktomb42 review (PR #506): this was called out in the
// design but not actually wired up as response headers — scoped narrowly
// to these three exact paths so no other product page changes behavior.
const VIGIL_PUBLIC_SHELL_PATHS = ["/vigil/confirm", "/vigil/retrieve", "/vigil/revoke"];

const nextConfig: NextConfig = {
  output: "standalone",
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${BACKEND_URL}/:path*`,
      },
    ];
  },
  async headers() {
    return VIGIL_PUBLIC_SHELL_PATHS.map((source) => ({
      source,
      headers: [
        { key: "Cache-Control", value: "no-store" },
        { key: "Referrer-Policy", value: "no-referrer" },
      ],
    }));
  },
};

export default nextConfig;
