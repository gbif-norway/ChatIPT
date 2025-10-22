/** @type {import('next').NextConfig} */
const nextConfig = {
  // Remove standalone output for Kaniko compatibility
  // output: 'standalone',
  experimental: {
    outputFileTracingRoot: undefined,
  },
}

module.exports = nextConfig
