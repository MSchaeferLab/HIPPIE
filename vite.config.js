import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "path";

export default defineConfig({
  plugins: [react()],
  base: "/hippienew/static/",
  build: {
    outDir: "hippie_django/hippie_website/static/hippie_website/js",
    emptyOutDir: true,
    manifest: true,
    rollupOptions: {
      input: {
        index:             resolve(__dirname, "frontend/index.jsx"),
        browse:            resolve(__dirname, "frontend/browse.jsx"),
        interaction_query: resolve(__dirname, "frontend/interaction_query.jsx"),
        ml_splits:         resolve(__dirname, "frontend/ml_splits.jsx"),
      },
    },
  },
  server: {
    origin: "http://localhost:5173",
    cors: true,
  },
});
