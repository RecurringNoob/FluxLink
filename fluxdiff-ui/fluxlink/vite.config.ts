
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The Flask viewer API.  Use 127.0.0.1, not "localhost": Node resolves
// "localhost" to the IPv6 address ::1 on many systems (notably Windows), but
// Flask listens on IPv4, so the proxy got ECONNREFUSED and answered 502.
const FLASK_API = "http://127.0.0.1:5000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: FLASK_API,
        changeOrigin: true,
      },
      "/images": {
        target: FLASK_API,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
  },
});