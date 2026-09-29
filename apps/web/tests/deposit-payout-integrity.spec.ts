import { expect, test, type Page, type Route } from "@playwright/test";

import {
  depositTransactionEffectiveDate,
  sortDepositTransactions,
  transactionTypeLabel,
} from "../src/components/deposits/deposit-utils";

const employeeId = "b92abf65-969a-4cf6-a13e-8454082e0209";
const employeeName = "Абдурахманов Сергей";
const createdAt = "2026-09-07T08:03:00Z";
const cashAcknowledgement = `Подтверждаю: деньги выданы сейчас сотруднику ${employeeName}.`;

type PaymentRequest = { employeeId: string; body: Record<string, unknown> };

// Each API request is fulfilled in the browser. These regressions never call a live API.
async function mockApi(page: Page, scheduledEnabled = false) {
  const payments: PaymentRequest[] = [];
  const schedules: PaymentRequest[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/v1", "").replace(/\/$/, "");
    if (request.method() === "OPTIONS") {
      return fulfillJson(route, null);
    }
    if (path === "/auth/refresh") {
      return fulfillJson(route, {
        access_token: "test-token",
        refresh_token: "test-refresh-token",
        token_type: "bearer",
        user: {
          id: "test-owner",
          email: "owner@example.com",
          full_name: "Владелец",
          roles: ["owner"],
        },
      });
    }
    if (path === "/deposits") return fulfillJson(route, [deposit()]);
    if (path === "/deposits/scheduled-payout/settings") {
      return fulfillJson(route, { enabled: scheduledEnabled });
    }
    if (path === `/deposits/${employeeId}/transactions`) {
      return fulfillJson(route, transactions());
    }
    if (path === `/deposits/${employeeId}/payout`) {
      payments.push({ employeeId, body: request.postDataJSON() });
      return fulfillJson(route, {});
    }
    if (path === `/deposits/${employeeId}/schedule-payout`) {
      schedules.push({ employeeId, body: request.postDataJSON() });
      return fulfillJson(route, {});
    }
    if (path === "/employees") return fulfillJson(route, [employee()]);
    if (path === "/settings") {
      return fulfillJson(route, [
        { id: "auto", key: "payroll.deposit_auto_withholding_enabled", value: true },
        {
          id: "rules",
          key: "payroll.category_rules",
          value: { "4": { deposit_target: 7000, deposit_withholding: 2000 } },
        },
      ]);
    }
    return fulfillJson(route, []);
  });
  return { payments, schedules };
}

async function openPayout(page: Page) {
  await page.goto("/payroll/deposits");
  const row = page.getByRole("row", { name: new RegExp(employeeName) });
  await row.getByRole("button", { name: "Операция", exact: true }).click();
  await page.getByRole("menuitem", { name: "Выдать депозит", exact: true }).click();
  return page.getByRole("dialog", { name: "Выдать депозит", exact: true });
}

async function openIndividualDeposit(page: Page) {
  await page.goto("/staff");
  await page.getByRole("button", { name: new RegExp(employeeName) }).click();
  await page.getByRole("button", { name: "Индивидуальный депозит", exact: true }).click();
  return page.getByRole("dialog", { name: "Индивидуальный депозит", exact: true });
}

test("cash payout names the recipient, amount and account and requires a fresh acknowledgement", async ({
  page,
}) => {
  const { payments } = await mockApi(page);
  const form = await openPayout(page);
  await form.getByRole("spinbutton").fill("2000");
  await form.getByRole("combobox").selectOption("cash_safe");
  await form.getByRole("button", { name: "Выдать депозит", exact: true }).click();

  const confirmation = page.getByRole("dialog", { name: "Подтвердить выдачу денег?", exact: true });
  await expect(confirmation.getByText(employeeName, { exact: true })).toBeVisible();
  await expect(confirmation.getByText("Сейф", { exact: true })).toBeVisible();
  const issue = confirmation.getByRole("button", {
    name: /Выдать 2\s000\s₽ · Абдурахманов Сергей/,
  });
  await expect(issue).toBeDisabled();
  await expect(
    confirmation.getByText("Деньги выданы сейчас. Баланс депозита уменьшится на эту сумму.", {
      exact: true,
    }),
  ).toBeVisible();
  await confirmation.getByRole("checkbox", { name: cashAcknowledgement, exact: true }).check();
  await expect(issue).toBeEnabled();
  await confirmation.getByRole("button", { name: "Отмена", exact: true }).click();

  await form.getByRole("spinbutton").fill("1500");
  await form.getByRole("combobox").selectOption("cash_tk");
  await form.getByRole("button", { name: "Выдать депозит", exact: true }).click();
  const changedIssue = confirmation.getByRole("button", {
    name: /Выдать 1\s500\s₽ · Абдурахманов Сергей/,
  });
  await expect(changedIssue).toBeDisabled();
  await expect(
    confirmation.getByRole("checkbox", { name: cashAcknowledgement, exact: true }),
  ).not.toBeChecked();
  await expect(confirmation.getByText("Торговая касса Черникова", { exact: true })).toBeVisible();
  expect(payments).toEqual([]);
  await confirmation.getByRole("checkbox", { name: cashAcknowledgement, exact: true }).check();
  await changedIssue.click();
  await expect
    .poll(() => payments)
    .toEqual([
      {
        employeeId,
        body: { amount: "1500", comment: null, payout_method: "cash_tk", payout_mode: "immediate" },
      },
    ]);
});

test("cash reserve and bank draft confirmations describe future payment without a cash acknowledgement", async ({
  page,
}) => {
  const { payments } = await mockApi(page);
  const form = await openPayout(page);
  await form.getByRole("spinbutton").fill("2000");
  await form.getByRole("combobox").selectOption("cash_safe");
  await form.getByRole("button", { name: "Создать резерв", exact: true }).click();
  await form.getByRole("button", { name: "Выдать депозит", exact: true }).click();

  const confirmation = page.getByRole("dialog", { name: "Подтвердить операцию?", exact: true });
  await expect(confirmation.getByRole("checkbox")).toHaveCount(0);
  await expect(
    confirmation.getByText(
      "Будет создан резерв. Деньги сотруднику ещё не выданы; депозит спишется при выдаче резерва.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(
    confirmation.getByRole("button", { name: "Создать резерв", exact: true }),
  ).toBeEnabled();
  await confirmation.getByRole("button", { name: "Отмена", exact: true }).click();
  await form.getByRole("combobox").selectOption("bank_draft_sber");
  await form.getByRole("button", { name: "Выдать депозит", exact: true }).click();
  await expect(confirmation.getByRole("checkbox")).toHaveCount(0);
  await expect(confirmation.getByText("Сбербанк → Сейф (черновик)", { exact: true })).toBeVisible();
  await expect(
    confirmation.getByText(
      "Будет создан банковский черновик. Деньги сотруднику ещё не выданы; депозит спишется при выдаче.",
      { exact: true },
    ),
  ).toBeVisible();
  await confirmation
    .getByRole("button", { name: "Создать банковский черновик", exact: true })
    .click();
  await expect
    .poll(() => payments)
    .toEqual([
      {
        employeeId,
        body: {
          amount: "2000",
          comment: null,
          payout_method: "bank_draft_sber",
          payout_mode: "immediate",
        },
      },
    ]);
});

test("payroll scheduling does not confirm that cash has already been delivered", async ({
  page,
}) => {
  const { payments, schedules } = await mockApi(page, true);
  const form = await openPayout(page);
  await form.getByRole("spinbutton").fill("");
  await form.getByRole("button", { name: "Запланировать выдачу", exact: true }).click();
  const confirmation = page.getByRole("dialog", { name: "Подтвердить операцию?", exact: true });
  await expect(confirmation.getByRole("checkbox")).toHaveCount(0);
  await expect(
    confirmation.getByText("Весь остаток на момент выплаты", { exact: true }),
  ).toBeVisible();
  await expect(
    confirmation.getByText(
      "Выдача будет запланирована в ближайшей ведомости. Деньги сейчас не выдаются.",
      { exact: true },
    ),
  ).toBeVisible();
  await confirmation.getByRole("button", { name: "Запланировать выдачу", exact: true }).click();
  await expect.poll(() => schedules).toEqual([{ employeeId, body: { amount: null } }]);
  expect(payments).toEqual([]);
});

test("staff full-balance payout confirms the same named recipient and explicit trading cash channel", async ({
  page,
}) => {
  const { payments } = await mockApi(page);
  const dialog = await openIndividualDeposit(page);
  await dialog.getByRole("button", { name: "Опасные действия", exact: true }).click();
  await dialog.getByRole("button", { name: "Выплатить остаток", exact: true }).click();
  const confirmation = page.getByRole("dialog", { name: "Выплатить остаток?", exact: true });
  await expect(confirmation.getByText(employeeName, { exact: true })).toBeVisible();
  await expect(confirmation.getByText("Торговая касса Черникова", { exact: true })).toBeVisible();
  const issue = confirmation.getByRole("button", {
    name: /Выдать 6\s000\s₽ · Абдурахманов Сергей/,
  });
  await expect(issue).toBeDisabled();
  await confirmation.getByRole("checkbox", { name: cashAcknowledgement, exact: true }).check();
  await issue.click();
  await expect
    .poll(() => payments)
    .toEqual([
      {
        employeeId,
        body: {
          amount: "6000.00",
          comment: "Ручная выплата",
          payout_method: "cash_tk",
          payout_mode: "immediate",
        },
      },
    ]);
});

test("surplus cash confirmation resets after account changes while bank drafts remain future payments", async ({
  page,
}) => {
  const { payments } = await mockApi(page);
  await page.goto("/staff");
  await page.getByRole("button", { name: new RegExp(employeeName) }).click();
  await page.getByRole("button", { name: "Выдать излишек", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Излишек депозита", exact: true });
  const issue = dialog.getByRole("button", { name: /Выдать 1\s000\s₽ · Абдурахманов Сергей/ });
  await expect(issue).toBeDisabled();
  await dialog.getByRole("checkbox", { name: cashAcknowledgement, exact: true }).check();
  await expect(issue).toBeEnabled();
  await dialog.getByRole("combobox").selectOption("bank_draft");
  await expect(dialog.getByRole("checkbox")).toHaveCount(0);
  await expect(
    dialog.getByText("Будет создан банковский черновик. Деньги сотруднику ещё не выданы.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "Создать банковский черновик", exact: true }),
  ).toBeEnabled();
  await dialog.getByRole("combobox").selectOption("cash_safe");
  await expect(issue).toBeDisabled();
  await expect(
    dialog.getByRole("checkbox", { name: cashAcknowledgement, exact: true }),
  ).not.toBeChecked();
  await dialog.getByRole("checkbox", { name: cashAcknowledgement, exact: true }).check();
  await issue.click();
  await expect
    .poll(() => payments)
    .toEqual([
      {
        employeeId,
        body: {
          amount: "1000.00",
          comment: "Выдача излишка депозита",
          payout_method: "cash_safe",
          payout_mode: "immediate",
        },
      },
    ]);
});

test("deposit histories use the actual date, retain the recorded timestamp and label dismissal operations", async ({
  page,
}) => {
  await mockApi(page);
  await page.goto("/payroll/deposits");
  await page
    .getByRole("row", { name: new RegExp(employeeName) })
    .getByRole("button", { name: "История", exact: true })
    .click();
  const history = page.getByRole("dialog", { name: "История депозита", exact: true });
  await expect(history.locator("tbody tr").first()).toContainText("старый API");
  await expect(history.locator("tbody tr").nth(1)).toContainText("Выдача при увольнении");
  await expect(history.locator("tbody tr").nth(2)).toContainText("Списание при увольнении");
  const actual = history.getByRole("row", { name: /Выдача при увольнении/ }).locator("time");
  await expect(actual).toHaveText("08.09.2026");
  await expect(actual).toHaveAttribute("datetime", "2026-09-08");
  await expect(actual).toHaveAttribute("title", /Записано: 07.09.2026, 11:03/);

  const dialog = await openIndividualDeposit(page);
  await expect(dialog.locator("tbody tr").first()).toContainText("старый API");
  await expect(dialog.locator("tbody tr").nth(1)).toContainText("Выдача при увольнении");
  await expect(dialog.locator("tbody tr").nth(2)).toContainText("Списание при увольнении");
  await expect(
    dialog.getByRole("row", { name: /Выдача при увольнении/ }).locator("time"),
  ).toHaveText("08.09.2026");
  await expect(dialog.getByRole("row", { name: /Списание при увольнении/ })).toBeVisible();
  await expect(dialog.getByRole("row", { name: /Неизвестная операция/ })).toBeVisible();
  const fallback = dialog.getByRole("row", { name: /старый API/ }).locator("time");
  await expect(fallback).toHaveAttribute("datetime", "2026-09-08");
  await expect(fallback).toHaveText("08.09.2026");
});

test("date fallback uses Moscow midnight and transaction labels distinguish dismissal from accrual", () => {
  expect(depositTransactionEffectiveDate({ created_at: "2026-09-07T22:30:00Z" })).toBe(
    "2026-09-08",
  );
  expect(depositTransactionEffectiveDate({ created_at: "2026-09-07T22:30:00" })).toBe("2026-09-08");
  expect(
    depositTransactionEffectiveDate({ happened_on: "2026-09-08", created_at: createdAt }),
  ).toBe("2026-09-08");
  expect(
    depositTransactionEffectiveDate({
      effective_date: "2026-09-09",
      happened_on: "2026-09-08",
      created_at: createdAt,
    }),
  ).toBe("2026-09-09");
  const rows = [
    { id: "recorded-later", happened_on: "2026-09-07", created_at: "2026-09-09T10:00:00Z" },
    { id: "actual-later", happened_on: "2026-09-08", created_at: createdAt },
    { id: "same-day-later-record", happened_on: "2026-09-08", created_at: "2026-09-08T10:00:00Z" },
  ];
  expect(sortDepositTransactions(rows).map((row) => row.id)).toEqual([
    "same-day-later-record",
    "actual-later",
    "recorded-later",
  ]);
  expect(rows.map((row) => row.id)).toEqual([
    "recorded-later",
    "actual-later",
    "same-day-later-record",
  ]);
  expect(transactionTypeLabel("dismissal_payout")).toBe("Выдача при увольнении");
  expect(transactionTypeLabel("dismissal_writeoff")).toBe("Списание при увольнении");
  expect(transactionTypeLabel("accrual")).toBe("Накопление");
  expect(transactionTypeLabel("unrecognized")).toBe("Неизвестная операция");
});

function fulfillJson(route: Route, body: unknown) {
  const headers = {
    "access-control-allow-credentials": "true",
    "access-control-allow-headers": "authorization, content-type",
    "access-control-allow-methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
    "access-control-allow-origin": route.request().headers()["origin"] ?? "http://127.0.0.1:5423",
  };
  return route.fulfill(
    route.request().method() === "OPTIONS"
      ? { status: 204, headers }
      : { status: 200, contentType: "application/json", headers, body: JSON.stringify(body) },
  );
}

function deposit() {
  return {
    id: employeeId,
    full_name: employeeName,
    position: "Повар",
    category: "intern",
    balance: "6000.00",
    initial_balance: "0.00",
    target: "5000.00",
    withholding: "2000.00",
    surplus: "1000.00",
    is_excluded: false,
    excluded_until: null,
    progress_pct: "100.00",
  };
}

function employee() {
  return {
    id: employeeId,
    full_name: employeeName,
    iiko_id: "iiko-test",
    position: "Повар",
    category: "intern",
    default_cooking_station: "sushi",
    is_senior: false,
    is_deputy_senior: false,
    status: "active",
    hire_date: "2026-09-04",
    fire_date: null,
    fire_reason: null,
    pin_assumed_from_iiko: false,
    pin_set_at: null,
    iiko_sync_at: createdAt,
    created_at: createdAt,
    updated_at: createdAt,
    assignments: [
      {
        id: "test-assignment",
        employee_id: employeeId,
        payroll_role: "sushi",
        category: "intern",
        is_primary: true,
        effective_from: "2026-09-04",
        effective_to: null,
        created_at: createdAt,
        updated_at: createdAt,
      },
    ],
  };
}

function transactions() {
  return [
    {
      id: "dismissal",
      employee_id: employeeId,
      run_id: null,
      transaction_type: "dismissal_payout",
      amount: "2000.00",
      created_at: createdAt,
      happened_on: "2026-09-08",
      effective_date: "2026-09-08",
    },
    {
      id: "late-record",
      employee_id: employeeId,
      run_id: null,
      transaction_type: "dismissal_writeoff",
      amount: "1000.00",
      created_at: "2026-09-09T10:00:00Z",
      happened_on: "2026-09-07",
      effective_date: "2026-09-07",
    },
    {
      id: "unknown",
      employee_id: employeeId,
      run_id: null,
      transaction_type: "unrecognized",
      amount: "500.00",
      created_at: createdAt,
      effective_date: "2026-09-06",
    },
    {
      id: "legacy",
      employee_id: employeeId,
      run_id: null,
      transaction_type: "accrual",
      amount: "2000.00",
      comment: "старый API",
      created_at: "2026-09-07T22:30:00Z",
    },
  ];
}
