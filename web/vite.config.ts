import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development the UI talks to the stack's published ports; in the container
// nginx proxies the same paths (see nginx.conf).
const proxy = (prefix: string, port: number) => ({
  target: `http://localhost:${port}`,
  changeOrigin: true,
  rewrite: (path: string) => path.replace(new RegExp(`^${prefix}`), ""),
});

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": proxy("/api", 8200),
      "/chaos": proxy("/chaos", 8100),
      "/prom": proxy("/prom", 9090),
      "/platform": proxy("/platform", 8050),
    },
  },
});
