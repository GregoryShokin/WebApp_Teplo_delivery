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

  await page.route(/\/api\/v1\/employees\/?(\?.*)?$/, (route) =>
    fulfillJson(route, [employee()]),
  );
  await page.route("**/api/v1/settings**", (route) => fulfillJson(route, []));
  await page.route(`**/api/v1/payroll/runs/${runId}/lines`, (route) =>
    fulfillJson(route, [payrollLine()]),
  );
  await page.route(`**/api/v1/payroll/runs/${runId}`, (route) =>
    fulfillJson(route, payrollRun()),
  );
});

test("shows payroll components per employee and opens the shift breakdown", async ({ page }) => {
  await page.goto(`/payroll/runs/${runId}`);

  await expect(page.getByRole("columnheader", { name: "Оклад" })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Премия" })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "%" })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Удержание депозита" })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Нак. фонд" })).toBeVisible();

  const employeeRow = page.getByRole("row", { name: /София Колесникова/ });
  await expect(employeeRow).toContainText("5 000 ₽");
  await expect(employeeRow).toContainText("1 200 ₽");
  await expect(employeeRow).toContainText("850 ₽");
  await expect(employeeRow).toContainText("500 ₽");

  await page.getByRole("button", { name: "Выплаты", exact: true }).click();
  await expect(page.getByRole("columnheader", { name: "Начислено" })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Оклад" })).toHaveCount(0);

  await page.getByRole("button", { name: "Состав начислений", exact: true }).click();
  await employeeRow.click();

  await expect(page.getByText("Смены и компоненты")).toBeVisible();
  await expect(page.getByText("Выручка дня 50 000 ₽")).toBeVisible();
  await expect(page.getByText("Удержания")).toBeVisible();
  await expect(page.getByText("Депозит: 500 ₽")).toBeVisible();
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
        penalties: [],
      },
    },
  };
}
