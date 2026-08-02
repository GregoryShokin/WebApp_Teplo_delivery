import { expect, test, type Route } from "@playwright/test";

/**
 * Отпуск, оплаченный закрытой ведомостью, заморожен в интерфейсе.
 *
 * Бэкенд вернёт 422 на попытку перенести или отменить такой отпуск, но узнавать об этом
 * после клика — плохо: менеджер уже успел набрать новые даты. Строка не должна открываться
 * на редактирование вовсе, а причина видна в подсказке.
 */

function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function period(overrides: Record<string, unknown> = {}) {
  return {
    id: "vac-1",
    employee_id: "emp-1",
    employee_full_name: "Шевченко Люба",
    date_start: "2026-08-02",
    date_end: "2026-08-11",
    days_count: 10,
    payout_date: "2026-08-04",
    payout_amount: 10000,
    status: "paid",
    settled_payout_date: null,
    comment: null,
    created_by_label: null,
    created_at: "2026-08-01T12:00:00+03:00",
    ...overrides,
  };
}

function mockRoster(page: import("@playwright/test").Page, settled: string | null) {
  return page.route("**/api/v1/vacations/roster**", (route) =>
    fulfillJson(route, [
      {
        employee_id: "emp-1",
        employee_full_name: "Шевченко Люба",
        position: "Повар",
        year: 2026,
        limit: 20,
        used: 10,
        remaining: 10,
        periods: [period({ settled_payout_date: settled })],
      },
    ]),
  );
}

test.beforeEach(async ({ page }) => {
  await page.route("**/api/v1/auth/refresh", (route) =>
    fulfillJson(route, {
      access_token: "test-token",
      refresh_token: "test-refresh-token",
      token_type: "bearer",
      user: {
        id: "user-owner",
        email: "owner@example.com",
        full_name: "Владелец",
        roles: ["owner"],
      },
    }),
  );
  await page.route("**/api/v1/vacations/payout-dates**", (route) =>
    fulfillJson(route, ["2026-08-11", "2026-08-18"]),
  );
});

test("у оплаченного отпуска заморожены деньги, но не комментарий", async ({ page }) => {
  await mockRoster(page, "2026-08-04");
  await page.goto("/schedule/vacations");

  const chip = page.getByRole("button", { name: /02\.08.*11\.08/ });
  await expect(chip).toHaveAttribute("title", /выплачены ведомостью от 04\.08\.2026/);
  await chip.click();

  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText(/Отпускные выплачены ведомостью от 04\.08\.2026/)).toBeVisible();
  // Денежные поля заблокированы — иначе второй транш.
  await expect(dialog.locator("#vacation-date-start")).toBeDisabled();
  await expect(dialog.locator("#vacation-date-end")).toBeDisabled();
  await expect(dialog.getByRole("button", { name: "Отменить отпуск" })).toBeDisabled();
  // Комментарий бэкенд разрешает — значит и форма должна.
  await expect(dialog.locator("#vacation-comment")).toBeEnabled();
});

test("незамороженный отпуск редактируется целиком", async ({ page }) => {
  await mockRoster(page, null);
  await page.goto("/schedule/vacations");

  await page.getByRole("button", { name: /02\.08.*11\.08/ }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText(/Отпускные выплачены ведомостью/)).toHaveCount(0);
  await expect(dialog.locator("#vacation-date-start")).toBeEnabled();
  await expect(dialog.getByRole("button", { name: "Отменить отпуск" })).toBeEnabled();
});
