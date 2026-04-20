import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "path";

export default defineConfig({
  plugins: [react()],
  base: "/static/",
  build: {
    outDir: "hippie_django/hippie_website/static/hippie_website/js",
    emptyOutDir: true,
    manifest: true,
    rollupOptions: {
      input: {
        index:             resolve(__dirname, "frontend/index.jsx"),
        browse:            resolve(__dirname, "frontend/browse.jsx"),
        interaction_query: resolve(__dirname, "frontend/interaction_query.jsx"),
      },
    },
  },
  server: {
    origin: "http://localhost:5173",
  },
});
