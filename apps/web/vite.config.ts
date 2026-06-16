import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// vite.config выполняется в Node; объявляем process локально, чтобы tsc -b не
// требовал @types/node (server.port используется только dev-сервером).
declare const process: { env: Record<string, string | undefined> };

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": new URL("./src", import.meta.url).pathname,
    },
  },
  server: {
    port: Number(process.env.PORT) || 5173,
    // macOS + Docker bind-mount теряет file-watch события — поллинг чинит HMR.
    watch: { usePolling: true, interval: 300 },
  },
});
