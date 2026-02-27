import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  env: {
    CAO_SERVER_URL: process.env.CAO_SERVER_URL || "http://localhost:9889",
  },
};

export default nextConfig;
