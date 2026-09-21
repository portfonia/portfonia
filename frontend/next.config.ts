import type { NextConfig } from "next";

// Ring 0: proxy /api/* to the local FastAPI backend so the browser talks to a
// single same-origin host (no CORS). The frontend never touches the DB directly.
const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

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
};

export default nextConfig;
