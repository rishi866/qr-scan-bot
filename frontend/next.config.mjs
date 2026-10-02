/** Static export: `npm run build` produces ./out which Caddy (or the FastAPI process) serves. */
/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "export",
  trailingSlash: true, // /users/ -> users/index.html on any static host
  images: { unoptimized: true },
  reactStrictMode: true,
};

export default nextConfig;
