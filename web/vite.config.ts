/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The production build is served by the FastAPI app under the CSP
// `default-src 'self'` (Requirement 19.1). Nothing here may pull in a remote
// script, stylesheet or font.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    // During development the API runs separately (`uvicorn server.app:app`).
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // Emit every asset as a file under /assets. Inlined `data:` URIs would be
    // blocked by `default-src 'self'` for images and fonts.
    assetsInlineLimit: 0,
    sourcemap: false,
  },
  test: {
    environment: "jsdom",
  },
});
