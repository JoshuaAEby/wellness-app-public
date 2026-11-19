import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8093",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "../backend/static",
    emptyOutDir: true,
    // NO manualChunks (removed 2026-10-21). A {vendor: [react…], charts: [recharts]} split
    // shipped a blank screen: recharts' own deps landed in the charts chunk and touched
    // React's internals before the vendor chunk had evaluated (circular chunk import —
    // "Cannot read properties of undefined (reading '__SECRET_INTERNALS…')"). `vite dev`
    // doesn't bundle, so only a production build shows it. The lazy tabs still split
    // automatically. Check any chunking change with `vite build && vite preview`.
  },
});
