import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "happy-dom",
    globals: true,
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["src/test/setup.ts"],
    css: true,
    coverage: {
      provider: "v8",
      include: [
        "src/shared/**/*.{ts,tsx}",
        "src/**/*View.tsx",
        "src/dashboard/Dashboard.tsx",
      ],
      reporter: ["text", "json", "json-summary"],
    },
  },
});
