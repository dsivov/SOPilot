import fs from "node:fs";
import path from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Remote-dev machine: always bind 0.0.0.0; the browser talks only to this
// origin and the proxy reaches the API locally (no CORS anywhere).
// HTTPS via self-signed cert in certs/ (required for microphone access from a
// remote origin — the voice channel). Accept the browser warning once.
const certDir = path.resolve(__dirname, "certs");
const https =
  fs.existsSync(path.join(certDir, "dev.crt")) && fs.existsSync(path.join(certDir, "dev.key"))
    ? { key: fs.readFileSync(path.join(certDir, "dev.key")), cert: fs.readFileSync(path.join(certDir, "dev.crt")) }
    : undefined;

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5174,
    https,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8100",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
    },
  },
});
