import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 5174",
    reuseExistingServer: true,
    url: "http://127.0.0.1:5174",
  },
  use: {
    baseURL: "http://127.0.0.1:5174",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
