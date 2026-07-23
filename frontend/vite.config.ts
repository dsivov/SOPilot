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

// Prod-configurable bind & API target:
//   SOPILOT_UI_PORT  UI port (default 5174)
//   SOPILOT_API_URL  backend the /api proxy forwards to (default http://127.0.0.1:8100)
const uiPort = Number(process.env.SOPILOT_UI_PORT || 5174);
const apiTarget = process.env.SOPILOT_API_URL || "http://127.0.0.1:8100";

const serverConfig = {
  host: "0.0.0.0",
  port: uiPort,
  https,
  proxy: {
    "/api": {
      target: apiTarget,
      changeOrigin: true,
      rewrite: (p: string) => p.replace(/^\/api/, ""),
    },
  },
};

export default defineConfig({
  plugins: [react()],
  server: serverConfig,
  preview: serverConfig,
});
