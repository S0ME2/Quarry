import { defineConfig } from "vitest/config";
const proxy = {
  "/api": {
    target: process.env.QUARRY_API_URL || "http://127.0.0.1:8000",
    changeOrigin: false,
  },
};
export default defineConfig({
  build: {
    rollupOptions: {
      onwarn(warning, warn) {
        // This browser-only SPA does not use React Server Component boundaries.
        if (
          warning.code === "MODULE_LEVEL_DIRECTIVE" &&
          warning.message.includes('"use client"') &&
          warning.id
            ?.replaceAll("\\", "/")
            .includes("/node_modules/@tanstack/react-query/")
        )
          return;
        warn(warning);
      },
    },
  },
  server: { proxy },
  preview: { proxy },
  test: { environment: "jsdom", setupFiles: ["./tests/setup.ts"] },
});
