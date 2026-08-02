import { expect, test, type Route } from "@playwright/test";

/**
 * Плитки «Учёта ДЗ/КЗ» и расшифровка долга сотрудникам.
 *
 * Налоговый контур отдаёт суммы Decimal'ом, то есть СТРОКОЙ, а остальные источники —
 * числом. Сложение превращалось в конкатенацию («874274.91168584.43» — две точки),
 * Number(...) давал NaN, и Intl.NumberFormat('ru-RU') печатал «не число». Проверяем
 * ровно эту комбинацию типов, как её отдаёт прод.
 */

function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

const STAFF_ROW = {
  employee_id: "employee-1",
  full_name: "Шевченко Люба",
  position: "Повар",
  staff_group: "staff",
  basis: "production",
  earned_to_date: 11829,
  on_demand_accrued: 0,
  on_demand_paid: 0,
  on_demand_debt: 0,
  already_advanced: 0,
  advances_outstanding: 0,
  finalized_unpaid: 0,
  loans_outstanding: 0,
  salary_payouts_outstanding: 0,
  vacation_payable: 10000,
  salary_payable: 21829,
  fund_payable: 42062,
  fund_current_year_payable: 42062,
  fund_prior_years_payable: 0,
  production_deposit_payable: 20000,
  courier_deposit_payable: 0,
  deposit_payable: 20000,
  payable: 83891,
  receivable: 0,
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
  await page.route("**/api/v1/accounting/suppliers/balances**", (route) =>
    fulfillJson(route, { items: [], receivable_total: 362649.51, payable_total: 332259.2 }),
  );
  await page.route("**/api/v1/accounting/suppliers/staff-payable**", (route) =>
    fulfillJson(route, {
      as_of: "2026-08-02",
      total: 83891,
      receivable_total: 0,
      salary_total: 21829,
      vacation_total: 10000,
      fund_total: 42062,
      fund_current_year_total: 42062,
      fund_prior_years_total: 0,
      production_deposit_total: 20000,
      courier_deposit_total: 0,
      deposit_total: 20000,
      items: [STAFF_ROW],
    }),
  );
  // Как на проде: Decimal → строка.
  await page.route("**/api/v1/taxes/debt**", (route) =>
    fulfillJson(route, {
      as_of: "2026-08-02",
      payable_total: "168584.43",
      items: [],
      wallet: { balance: "0" },
    }),
  );
  await page.route(/\/api\/v1\/accounting\/suppliers(\?.*)?$/, (route) =>
    fulfillJson(route, { items: [], needs_review_total: 0 }),
  );
});

test("итог кредиторки складывается, а не конкатенируется", async ({ page }) => {
  await page.goto("/dz-kz");

  // 332 259,2 + 83 891 + 168 584,43 = 584 734,63 → «584 735 ₽» при округлении до рубля.
  await expect(page.getByText(/584\s735/)).toBeVisible();
  // «не число» — это Intl.NumberFormat('ru-RU').format(NaN).
  await expect(page.getByText("не число")).toHaveCount(0);
});

test("отпускные видны в расшифровке долга сотрудникам", async ({ page }) => {
  await page.goto("/dz-kz");

  await expect(page.getByText(/в т\.ч\. отпускные/)).toContainText("10 000");
});
