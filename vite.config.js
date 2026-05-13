import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "path";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");  // "" = load all vars, not just VITE_*

  const staticPath = (env.DJANGO_STATIC_URL || env.APACHE_PUBLISHED_PATH || "").replace(/\/+$/, "");  // e.g. "/hippienew"
  const staticBase = staticPath
    ? `${staticPath}/static/`
    : "/static/";

  return {
    plugins: [react()],
    base: staticBase,
    build: {
      outDir: "hippie_django/hippie_website/static/hippie_website/js",
      emptyOutDir: true,
      manifest: true,
      rollupOptions: {
        input: {
          index: resolve(__dirname, "frontend/index.jsx"),
          browse: resolve(__dirname, "frontend/browse.jsx"),
          interaction_query: resolve(__dirname, "frontend/interaction_query.jsx"),
          ml_splits: resolve(__dirname, "frontend/ml_splits.jsx"),
        },
      },
    },
    server: {
      origin: "http://localhost:5173",
      cors: true,
    },
  };
});
