import type { NextConfig } from "next";

// Ring 0: proxy /api/* to the local FastAPI backend so the browser talks to a
// single same-origin host (no CORS). The frontend never touches the DB directly.
const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

const AUTH_ORIGIN = process.env.NEXT_PUBLIC_SUPABASE_URL ?? "https://auth.portfonia.com";

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
    const csp = [
      "default-src 'self'",
      "script-src 'self'",
      "style-src 'self' 'unsafe-inline'",
      "img-src 'self' data:",
      "font-src 'self'",
      `connect-src 'self' ${AUTH_ORIGIN}`,
      "object-src 'none'",
      "base-uri 'self'",
      "form-action 'self'",
      "frame-ancestors 'none'",
    ].join("; ");
    return [
      {
        source: "/auth/vigil",
        headers: [
          { key: "Cache-Control", value: "no-store" },
          { key: "Referrer-Policy", value: "no-referrer" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Content-Security-Policy", value: csp },
          { key: "Cross-Origin-Opener-Policy", value: "unsafe-none" },
        ],
      },
    ];
  },
};

export default nextConfig;
