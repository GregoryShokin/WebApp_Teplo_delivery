import { defineConfig, devices } from "@playwright/test";

// Порт вынесен в переменную: над проектом работает несколько агентов сразу, и
// `reuseExistingServer` молча подхватил бы чужой dev-сервер из другого worktree —
// тесты бы шли по чужому коду. Свой слот: WEB_E2E_PORT=5213 npx playwright test.
const PORT = process.env.WEB_E2E_PORT ?? "5174";
const BASE_URL = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./tests",
  webServer: {
    command: `npm run dev -- --host 127.0.0.1 --port ${PORT} --strictPort`,
    // Свой слот задан явно → поднимаем ТОЛЬКО свой сервер. Иначе на занятом порту
    // playwright переиспользовал бы чужой (соседний worktree отвечает на 5213 — так и
    // случилось 31.07: три теста «упали» на чужой сборке). Со --strictPort занятый порт
    // теперь честно роняет запуск вместо тихой проверки не того кода.
    reuseExistingServer: process.env.WEB_E2E_PORT === undefined,
    url: BASE_URL,
  },
  use: {
    baseURL: BASE_URL,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
