import { expect, test, type Page, type Route } from "@playwright/test";

const employeeId = "personal-deposit-employee";
const runId = "personal-deposit-run";
const manualPayout = {
  id: "manual-payout",
  transaction_type: "payout",
  amount: 2000,
  run_id: null,
  created_at: "2026-09-07T10:00:00Z",
  happened_on: "2026-09-08",
  effective_date: "2026-09-08",
};

async function openReport(page: Page, report: ReturnType<typeof personalReport>) {
  await page.route("**/api/v1/auth/refresh", (route) => fulfillJson(route, {
    access_token: "test-token", refresh_token: "test-refresh", token_type: "bearer",
    user: { id: "owner", email: "owner@example.com", full_name: "Владелец", roles: ["owner"] },
  }));
  await page.route(/\/api\/v1\/employees\/?(\?.*)?$/, (route) => fulfillJson(route, [{
    id: employeeId, full_name: report.employee_name, position: "Повар", status: "active",
    in_personal_report: true, assignments: [],
  }]));
  await page.route("**/api/v1/settings**", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/payroll/runs**", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/payroll/periods**", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/payroll/employee-report**", (route) => fulfillJson(route, report));
  await page.goto("/payroll/personal");
  await expect(page.getByText("Детализация", { exact: true })).toBeVisible();
}

test("explains deposit withholding once and reconciles the payslip after rounding", async ({ page }) => {
  const report = personalReport();
  report.deposit_transactions = [
    { ...manualPayout, id: "old-accrual", transaction_type: "accrual", run_id: "old-run" },
    { ...manualPayout, id: "selected-accrual", transaction_type: "accrual", run_id: runId },
  ];
  await openReport(page, report);
  await page.getByRole("row", { name: /08.09.2026 — 14.09.2026/ }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("columnheader", { name: "Удержание депозита", exact: true })).toBeVisible();
  await expect(dialog.getByRole("columnheader", { name: "Округление выплаты", exact: true })).toBeVisible();
  await expect(dialog.getByText("В том числе депозит: 2 000 ₽")).toBeVisible();
  await expect(dialog.getByRole("cell", { name: "−2 000", exact: true })).toHaveCount(1);
  await expect(dialog.getByRole("cell", { name: "−2 255,90", exact: true }).first()).toBeVisible();
  await expect(dialog.getByRole("cell", { name: "−3", exact: true })).toBeVisible();
  const lastDay = dialog.getByRole("row", { name: /14.09.2026/ });
  await expect(lastDay.getByRole("cell").last()).toHaveText("+190,90");
  await expect(dialog.getByText("4 255,90 ₽", { exact: true })).toBeVisible();
  await expect(dialog.locator("tfoot")).toContainText("4 335 ₽");
});

test("shows a manual payout separately without adding it to the scheduled payout or salary", async ({ page }) => {
  const report = personalReport();
  report.periods[0].deposit_payout = 2000;
  report.periods[0].manual_deposit_payout = 2000;
  report.periods[0].manual_deposit_transactions = [manualPayout];
  report.deposit_transactions = [manualPayout, { ...manualPayout, id: "scheduled-payout", run_id: runId }];
  report.totals.deposit_payout = 2000;
  report.totals.manual_deposit_payout = 2000;
  await openReport(page, report);
  const row = page.getByRole("row", { name: /08.09.2026 — 14.09.2026/ });
  await expect(row.getByRole("cell", { name: "6 335 ₽", exact: true })).toBeVisible();
  await expect(page.getByText("Выдачи депозита без ведомости", { exact: true })).toHaveCount(0);
  await row.click();
  const dialog = page.getByRole("dialog");
  const separate = dialog.locator("section").filter({ hasText: "Выдачи депозита вне ведомости" });
  await expect(separate.getByRole("cell", { name: "+2 000 ₽", exact: true })).toHaveCount(1);
  await expect(separate.getByRole("cell", { name: "08.09.2026", exact: true })).toBeVisible();
  await expect(dialog.locator("tfoot")).toContainText("6 335 ₽");
  await expect(dialog.getByText("8 335 ₽", { exact: true })).toHaveCount(0);
  await expect(dialog.getByRole("columnheader", { name: "Выдача депозита", exact: true })).toBeVisible();
});

test("keeps a payout visible in weekly view without a payroll run and uses the actual date", async ({ page }) => {
  const report = personalReport();
  report.employee_name = "Авакумов Андрей";
  report.periods = [];
  report.daily = [];
  report.deposit_transactions = [manualPayout];
  report.totals.manual_deposit_payout = 2000;
  report.totals.total_payable = 0;
  await openReport(page, report);
  const standalone = page.locator("section").filter({ hasText: "Выдачи депозита без ведомости" }).last();
  await expect(standalone.getByRole("cell", { name: "08.09.2026", exact: true })).toBeVisible();
  await expect(standalone.getByRole("cell", { name: "+2 000 ₽", exact: true })).toHaveCount(1);
  await expect(standalone.getByRole("cell", { name: "07.09.2026", exact: true })).toHaveCount(0);
  await page.getByRole("tab", { name: "По дням", exact: true }).click();
  await expect(page.getByRole("cell", { name: "08.09.2026", exact: true })).toBeVisible();
  await expect(page.getByRole("cell", { name: "+2 000 ₽", exact: true })).toHaveCount(1);
  await expect(page.getByRole("cell", { name: "07.09.2026", exact: true })).toHaveCount(0);
});

function personalReport() {
  return {
    employee_id: employeeId, employee_name: "Абдурахманов Сергей", employee_position: "Повар",
    date_from: "2026-09-01", date_to: "2026-09-29", opening_balance: "0", closing_balance: "4335",
    fund_accumulated: 0, fund_outstanding: 0, shifts_count: 4,
    adjustments: [], deposit_transactions: [] as Array<typeof manualPayout | (Omit<typeof manualPayout, "run_id"> & { run_id: string })>,
    daily: [] as Array<Record<string, unknown>>,
    periods: [{
      period_id: "personal-deposit-period", run_id: runId, run_status: "finalized", role: "pizza, sushi",
      period_start: "2026-09-08", period_end: "2026-09-14", is_substitute: false, roles: [],
      base_pay: 8400, premium: 193.9, percent_pay: 0, vacation_pay: 0, ndfl_withheld: 0, fund_accrual: 0,
      deduction: 4255.9, deposit_withholding: 2000, deposit_payout: 0, payroll_rounding: 3,
      total_payable: 4335, bonus_total: 193.9, penalty_total: 2255.9,
      manual_deposit_payout: 0, manual_deposit_transactions: [] as Array<typeof manualPayout>,
      days: [
        { date: "2026-09-11", role: "pizza", base_pay: 2200 },
        { date: "2026-09-12", role: "pizza", base_pay: 2200 },
        { date: "2026-09-13", role: "sushi", base_pay: 2000 },
        { date: "2026-09-14", role: "sushi", base_pay: 2000 },
      ],
      adjustments: {
        bonuses: [{ id: "bonus", work_date: "2026-09-14", amount: 193.9, comment: "возврат ревизии" }],
        penalties: [{ id: "audit", work_date: "2026-09-09", amount: 2255.9, comment: "Недостача по ревизии" }],
      },
    }],
    totals: {
      base_pay: 8400, premium: 193.9, percent_pay: 0, vacation_pay: 0, ndfl_withheld: 0,
      fund_accrual: 0, deduction: 4255.9, deposit_withholding: 2000, deposit_payout: 0,
      bonus_total: 193.9, penalty_total: 2255.9, audit_penalty_total: "2255.90", total_payable: 4335,
      manual_deposit_payout: 0, payroll_rounding: 3,
    },
  };
}

function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
}
