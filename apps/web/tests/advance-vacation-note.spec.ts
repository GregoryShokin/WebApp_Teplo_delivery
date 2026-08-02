import { expect, test, type Route } from "@playwright/test";

/**
 * Диалог «Выдать аванс»: сумма и пояснение видны ОДНОВРЕМЕННО.
 *
 * Раньше нота подменяла собой строку «Доступно к авансу», поэтому у сотрудника с
 * отпускными пояснение прятало само число. Проверяем оба случая: с нотой и без.
 */

const employeeId = "employee-1";

function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

/** «Доступно к авансу: 11 829 ₽» + отпускные, которые в аванс не входят. */
const WITH_VACATION = {
  employee_id: employeeId,
  as_of: "2026-08-02",
  period_start: "2026-07-28",
  period_end: "2026-08-02",
  basis: "production",
  earned_to_date: 11829,
  already_advanced: 0,
  available: 11829,
  note: "Отпускные 10 000 ₽ выплатятся ведомостью 04.08.2026 и в аванс не входят",
  payout_reached: false,
};

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
  await page.route("**/api/v1/payroll/advances/issue-wallets**", (route) =>
    fulfillJson(route, [{ id: "payroll", code: "payroll", name: "Сейф" }]),
  );
  await page.route("**/api/v1/payroll/advances/upcoming-payslips**", (route) =>
    fulfillJson(route, []),
  );
  await page.route(/\/api\/v1\/payroll\/advances(\?.*)?$/, (route) => fulfillJson(route, []));
  await page.route(/\/api\/v1\/employees\/?(\?.*)?$/, (route) =>
    fulfillJson(route, [
      {
        id: employeeId,
        full_name: "Шевченко Люба",
        position: "Повар",
        status: "active",
        category: "category_1",
      },
    ]),
  );
});

async function openIssueDialog(page: import("@playwright/test").Page) {
  await page.goto("/payroll/advances");
  await page.getByRole("button", { name: "Добавить аванс" }).click();
  const dialog = page.getByRole("dialog");
  // EmployeeCombobox — собственный виджет на <button>, не нативный select.
  await dialog.getByRole("button", { name: "Выберите сотрудника" }).click();
  await dialog.getByRole("button", { name: "Шевченко Люба" }).click();
}

test("сумма и пояснение про отпускные показаны вместе", async ({ page }) => {
  await page.route("**/api/v1/payroll/advances/availability**", (route) =>
    fulfillJson(route, WITH_VACATION),
  );

  await openIssueDialog(page);

  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText(/Доступно к авансу/)).toBeVisible();
  await expect(dialog.getByText(/11\s829/)).toBeVisible();
  await expect(dialog.getByText(/Отпускные .* в аванс не входят/)).toBeVisible();

  await dialog.screenshot({ path: "test-results/advance-vacation-note.png" });
});

test("без отпуска пояснения нет, сумма на месте", async ({ page }) => {
  await page.route("**/api/v1/payroll/advances/availability**", (route) =>
    fulfillJson(route, { ...WITH_VACATION, note: null }),
  );

  await openIssueDialog(page);

  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText(/Доступно к авансу/)).toBeVisible();
  await expect(dialog.getByText(/Отпускные/)).toHaveCount(0);
});
