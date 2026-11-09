import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Port 5173 is one of the origins cao-server allows by default (3000, 5173, 5174).
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, strictPort: true },
  test: {
    environment: "node",
    include: ["core/__tests__/**/*.test.ts", "view/__tests__/**/*.test.{ts,tsx}"],
    coverage: {
      provider: "v8",
      include: ["core/**/*.ts"],
      exclude: ["core/__tests__/**", "core/index.ts", "core/types.ts"],
      reporter: [["text", { skipFull: false }]],
      thresholds: { lines: 100, branches: 100, functions: 100, statements: 100 },
    },
  },
});
