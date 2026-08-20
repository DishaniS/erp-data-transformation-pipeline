import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // Loopback only. The backend's CORS allow-list must contain this exact
    // origin (see frontend/.env.example and the README).
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
  },
  test: {
    // Node, not jsdom: every test here exercises pure logic (URL building,
    // upload routing, error mapping, polling termination) and needs no DOM.
    environment: "node",
    globals: true,
  },
});
