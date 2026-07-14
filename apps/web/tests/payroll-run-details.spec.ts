import { expect, test, type Route } from "@playwright/test";

const runId = "run-details";
const periodId = "period-details";
const employeeId = "employee-details";

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

  await page.route(/\/api\/v1\/employees\/?(\?.*)?$/, (route) => fulfillJson(route, [employee()]));
  await page.route("**/api/v1/settings**", (route) => fulfillJson(route, []));
  await page.route(`**/api/v1/payroll/runs/${runId}/lines`, (route) =>
    fulfillJson(route, [payrollLine()]),
  );
  await page.route(`**/api/v1/payroll/runs/${runId}`, (route) => fulfillJson(route, payrollRun()));
});

test("shows one payroll statement and opens the employee breakdown modal", async ({ page }) => {
  await page.goto(`/payroll/runs/${runId}`);

  await expect(page.getByRole("button", { name: "Состав начислений", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Выплаты", exact: true })).toHaveCount(0);
  await expect(page.getByRole("columnheader", { name: "Сотрудник" })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Начислено" })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "К выплате" })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Остаток" })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Статус" })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Удержано" })).toHaveCount(0);
  await expect(page.getByRole("columnheader", { name: "Выдача депозита" })).toHaveCount(0);

  const employeeRow = page.getByRole("row", { name: /София Колесникова/ });
  await expect(employeeRow).toContainText("6 550 ₽");
  await expect(employeeRow).toContainText("Пиццерист");

  await employeeRow.click();

  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("heading", { name: "София Колесникова" })).toBeVisible();
  await expect(dialog).toContainText("7–13 июля");
  await expect(dialog.getByText("Оклад", { exact: true }).first()).toBeVisible();
  await expect(dialog.getByText("5 000 ₽", { exact: true }).first()).toBeVisible();
  await expect(dialog.getByText("Премии", { exact: true }).first()).toBeVisible();
  await expect(dialog.getByText("1 200 ₽", { exact: true }).first()).toBeVisible();
  await expect(dialog.getByText("Премия за смену")).toBeVisible();
  await expect(dialog.getByText("Стажировка новичка")).toBeVisible();
  await expect(dialog.getByText("Смены и начисления")).toBeVisible();
  await expect(dialog.getByRole("cell", { name: "50 000 ₽" })).toBeVisible();
  await expect(dialog.getByText("2-я категория")).toBeVisible();
  await expect(dialog.getByText("category_2")).toHaveCount(0);
  await expect(dialog.getByText("Удержание депозита", { exact: true })).toBeVisible();
  await expect(dialog.getByText("В расчёте: 500 ₽")).toBeVisible();
  await expect(dialog.getByText("Удерживать", { exact: true })).toBeVisible();
  const penalties = dialog.locator("details").filter({ hasText: "Штрафы и удержания" });
  await penalties.locator("summary").click();
  await expect(penalties).toContainText("Удержание");
  await expect(penalties).toContainText("Недостача по ревизии");
  await expect(penalties).toContainText("хоккайдо цезарь");
  await expect(dialog.getByText("Итог выплаты")).toBeVisible();
});

test("includes a deposit-only amount in the payments register", async ({ page }) => {
  await page.unroute(/\/api\/v1\/employees\/?(\?.*)?$/);
  await page.route(/\/api\/v1\/employees\/?(\?.*)?$/, (route) =>
    fulfillJson(route, [
      {
        ...employee(),
        full_name: "Молоканова Светлана",
        position: "Повар",
      },
    ]),
  );
  await page.unroute(`**/api/v1/payroll/runs/${runId}/lines`);
  await page.route(`**/api/v1/payroll/runs/${runId}/lines`, (route) =>
    fulfillJson(route, [
      {
        ...payrollLine(),
        base_pay: 1225.28,
        premium: 0,
        percent_pay: 206,
        deduction: 1431.28,
        deposit_withholding: 719.91,
        deposit_payout: 2000,
        deposit_payout_scheduled: 2000,
        total_payable: 0,
      },
    ]),
  );
  await page.unroute(`**/api/v1/payroll/runs/${runId}`);
  await page.route(`**/api/v1/payroll/runs/${runId}`, (route) =>
    fulfillJson(route, {
      ...payrollRun(),
      summary: { ...payrollRun().summary, total_payable: 0 },
    }),
  );

  await page.goto(`/payroll/runs/${runId}`);

  const employeeRow = page.getByRole("row", { name: /Молоканова Светлана/ });
  const cells = employeeRow.getByRole("cell");
  await expect(cells.nth(1)).toContainText("0 ₽");
  await expect(cells.nth(1)).toContainText("+ депозит 2 000 ₽");
  await expect(cells.nth(2)).toHaveText("2 000 ₽");
  await expect(cells.nth(3)).toHaveText("2 000 ₽");

  await employeeRow.click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("зарплата 0 ₽");
  await expect(dialog).toContainText("депозит 2 000 ₽");
  await expect(dialog.getByRole("button", { name: "Отменить выдачу" })).toBeEnabled();
});

test("offers a full payment from the employee modal after finalization", async ({ page }) => {
  await page.unroute(`**/api/v1/payroll/runs/${runId}`);
  await page.route(`**/api/v1/payroll/runs/${runId}`, (route) =>
    fulfillJson(route, {
      ...payrollRun(),
      status: "finalized",
      period: {
        ...payrollRun().period,
        status: "finalized",
        finalized_at: "2026-07-14T12:30:00+03:00",
      },
    }),
  );
  await page.route(`**/api/v1/payroll/runs/${runId}/bank-draft**`, (route) =>
    route.fulfill({ status: 404 }),
  );

  await page.goto(`/payroll/runs/${runId}`);
  await page.getByRole("row", { name: /София Колесникова/ }).click();

  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("button", { name: "Выплатить 6 550 ₽" })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Выплатить частично" })).toBeVisible();
});

function fulfillJson(route: Route, body: unknown) {
  if (route.request().method() === "OPTIONS") {
    return route.fulfill({ status: 204 });
  }
  return route.fulfill({
    body: JSON.stringify(body),
    contentType: "application/json",
    status: 200,
  });
}

function payrollRun() {
  return {
    id: runId,
    period_id: periodId,
    started_at: "2026-07-14T12:19:00+03:00",
    finished_at: "2026-07-14T12:19:30+03:00",
    status: "completed",
    blocking_issues: [],
    summary: {
      line_count: 1,
      employee_count: 1,
      total_payable: 6550,
    },
    is_imported_legacy: false,
    payout_cash_total: 0,
    payout_cash_wallet_id: null,
    needs_recalc: false,
    period: {
      id: periodId,
      period_type: "week",
      start_date: "2026-07-07",
      end_date: "2026-07-13",
      payroll_date: "2026-07-14",
      status: "open",
      finalized_at: null,
      finalized_by_user_id: null,
    },
  };
}

function employee() {
  return {
    id: employeeId,
    full_name: "София Колесникова",
    iiko_id: "iiko-employee-details",
    position: "Пиццерист",
    category: "category_2",
    default_cooking_station: "pizza",
    is_senior: false,
    is_deputy_senior: false,
    is_courier_placeholder: false,
    status: "active",
    hire_date: "2026-01-01",
    tenure_started_at: "2026-01-01",
    fire_date: null,
    fire_reason: null,
    requires_role_review: false,
    requires_position_review: false,
    role_review_payload: null,
    in_personal_report: true,
    pin_assumed_from_iiko: false,
    pin_set_at: null,
    iiko_sync_at: null,
    created_at: "2026-01-01T00:00:00+03:00",
    updated_at: "2026-01-01T00:00:00+03:00",
    assignments: [],
    active_notice: null,
  };
}

function payrollLine() {
  return {
    id: "line-details",
    run_id: runId,
    employee_id: employeeId,
    role: "pizza",
    base_pay: 5000,
    premium: 1200,
    percent_pay: 850,
    vacation_pay: 0,
    ndfl_withheld: 0,
    fund_accrual: 300,
    deduction: 500,
    deposit_withholding: 500,
    deposit_payout: 0,
    deposit_payout_scheduled: 0,
    advance_issued: 0,
    ndfl_deduction: 0,
    total_payable: 6550,
    deposit_excluded_for_run: false,
    deposit_exclusion_reason: null,
    payment_status: "pending",
    paid_amount: null,
    paid_at: null,
    paid_method: null,
    payment_comment: null,
    amount_cash: 0,
    amount_account: 6550,
    payout_status: "pending",
    draft_status: null,
    overpaid_amount: 0,
    on_demand: false,
    on_demand_accrued: 0,
    on_demand_paid: 0,
    on_demand_debt: 0,
    components: {
      days: [
        {
          date: "2026-07-07",
          role: "pizza",
          category: "category_2",
          hours: 10,
          base_pay: 5000,
          percent_pay: 850,
          vacation_pay: 0,
          fund_accrual: 300,
          daily_revenue: 50000,
        },
      ],
      adjustments: {
        bonuses: [
          {
            id: "bonus-details",
            work_date: "2026-07-07",
            category: "Премия за смену",
            amount: 1200,
            comment: "Стажировка новичка",
          },
        ],
        penalties: [
          {
            id: "penalty-details",
            work_date: "2026-07-07",
            category: "Недостача по ревизии",
            amount: 500,
            comment: "хоккайдо цезарь",
          },
        ],
      },
    },
  };
}
