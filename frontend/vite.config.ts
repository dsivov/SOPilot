import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Remote-dev machine: always bind 0.0.0.0; the browser talks only to this
// origin and the proxy reaches the API locally (no CORS anywhere).
export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5174,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8100",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
    },
  },
});
