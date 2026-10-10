/** @type {import('next').NextConfig} */
// Local launcher builds with `next build` + `next start` (a Node server), while
// the Docker image builds a fully static export served by FastAPI. Only switch
// to static export when explicitly requested so local usage keeps working.
const isStaticExport = process.env.NEXT_STATIC_EXPORT === "1";

const nextConfig = {
  ...(isStaticExport
    ? {
        output: "export",
        // Emit /route/index.html so the backend can serve each route directly.
        trailingSlash: true,
        images: { unoptimized: true },
      }
    : {}),
  // On Vercel the FastAPI backend is mounted at /api on the same origin (see
  // the root vercel.json), so the browser calls it relatively. Local builds
  // keep using NEXT_PUBLIC_API_URL (or the localhost default) from .env.local.
  ...(process.env.VERCEL
    ? { env: { NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL || "/api" } }
    : {}),
  turbopack: {},
  webpack: (config) => {
    config.resolve.alias.canvas = false;
    return config;
  },
};

module.exports = nextConfig;
