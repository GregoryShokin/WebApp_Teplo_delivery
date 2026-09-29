import { expect, test, type Page, type Route } from "@playwright/test";

const DEPOSIT_ARTICLE = "11111111-1111-1111-1111-111111111111";
const PLAIN_ARTICLE = "22222222-2222-2222-2222-222222222222";
const EMPLOYEE = "33333333-3333-3333-3333-333333333333";
const TXN = "44444444-4444-4444-4444-444444444444";
const DOMAIN_HINT = "Выдача депозита обязательно связана с сотрудником. Оформите или скорректируйте её в разделе «Зарплата → Депозиты».";

const fulfillJson = (route: Route, body: unknown) => route.fulfill({
  status: 200, contentType: "application/json", body: JSON.stringify(body),
});

function row(overrides: Record<string, unknown> = {}) {
  return {
    kind: "cashflow", id: TXN, bank_operation_id: null, status: "classified",
    operation_date: "2026-09-08", occurred_at: "2026-09-08T21:08:00+03:00",
    direction: "out", amount: "2000.00", article_id: DEPOSIT_ARTICLE,
    counterparty_id: null, wallet_id: null, provider: null,
    payment_purpose: "Выдача депозита сотруднику (операция UUID)",
    counterparty_name_raw: null, counterparty_inn_raw: null, is_card: false,
    source_kind: "production_deposit_payout", employee_id: EMPLOYEE,
    employee_name: "Абдурахманов Сергей", classification_blocked_reason: DOMAIN_HINT,
    ...overrides,
  };
}

async function mockCommon(page: Page, item: Record<string, unknown>) {
  await page.route("**/api/v1/auth/refresh", (route) => fulfillJson(route, {
    access_token: "test-token", refresh_token: "test-refresh-token", token_type: "bearer",
    user: { id: "user-owner", email: "owner@example.com", full_name: "Владелец", roles: ["owner"] },
  }));
  await page.route("**/api/v1/settings**", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/dds/articles**", (route) => fulfillJson(route, [
    { id: DEPOSIT_ARTICLE, code: "vydacha_depozita_sotrudniku", name: "Выдача депозита", is_active: true, movement_type: "outflow", activity_type: "operating", aliases: [] },
    { id: PLAIN_ARTICLE, code: "prochie_rashody", name: "Прочие расходы", is_active: true, movement_type: "outflow", activity_type: "operating", aliases: [] },
  ]));
  await page.route("**/api/v1/dds/wallets**", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/counterparties/directory**", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/dds/journal**", (route) => fulfillJson(route, { items: [item], total: 1, marked_total: 1, unmarked_total: 0, transfer_total: 0 }));
}

async function openJournal(page: Page) {
  await page.goto("/dds");
  await page.getByRole("tab", { name: /Журнал ДДС/ }).click();
}

test("депозит показывает сотрудника и не разрешает независимый разбор или исключение", async ({ page }) => {
  await mockCommon(page, row());
  await openJournal(page);
  const journalRow = page.getByRole("row").filter({ hasText: "Абдурахманов Сергей" });
  await expect(journalRow).toBeVisible();
  await journalRow.click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("Сотрудник-получатель (обязательно)")).toBeVisible();
  await expect(dialog.getByText("Абдурахманов Сергей", { exact: true })).toBeVisible();
  await expect(dialog.getByText(DOMAIN_HINT)).toBeVisible();
  await expect(dialog.getByText("контрагент (необязательно)")).toHaveCount(0);
  await expect(dialog.getByRole("button", { name: "Разнести", exact: true })).toHaveCount(0);
  await expect(dialog.getByRole("button", { name: "Исключить", exact: true })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await journalRow.click();
  await expect(page.getByRole("dialog").getByText("Абдурахманов Сергей", { exact: true })).toBeVisible();
});

test("историческая выдача без источника показывает отсутствие обязательного сотрудника", async ({ page }) => {
  await mockCommon(page, row({ source_kind: "manual", employee_id: null, employee_name: null }));
  await openJournal(page);
  await page.getByRole("row").filter({ hasText: "Выдача депозита" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText(/Не удалось определить сотрудника/)).toBeVisible();
  await expect(dialog.getByText(DOMAIN_HINT)).toBeVisible();
  await expect(dialog.getByText("контрагент (необязательно)")).toHaveCount(0);
  await expect(dialog.getByRole("button", { name: "Исключить", exact: true })).toHaveCount(0);
});

test("статья выдачи депозита требует сотрудника через депозитный контур, не зарплатную атрибуцию", async ({ page }) => {
  await mockCommon(page, row({ article_id: PLAIN_ARTICLE, source_kind: "manual", employee_id: null, employee_name: null, classification_blocked_reason: null }));
  await openJournal(page);
  await page.getByRole("row").filter({ hasText: "Прочие расходы" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("button", { name: "Разнести", exact: true })).toBeEnabled();
  await dialog.getByRole("button", { name: "Прочие расходы", exact: true }).click();
  await dialog.getByRole("button", { name: "Выдача депозита", exact: true }).click();
  await expect(dialog.getByText("нужен сотрудник (выдача депозита)")).toBeVisible();
  await expect(dialog.getByText(DOMAIN_HINT)).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Разнести", exact: true })).toBeDisabled();
  await expect(dialog.getByText("контрагент (необязательно)")).toHaveCount(0);
});
