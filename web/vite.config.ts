import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development the browser talks only to Vite (port 5173). Vite forwards /api and /ws to the
// FastAPI service on port 8000, the same way Nginx will in production.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
});
