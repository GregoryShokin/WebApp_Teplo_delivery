import { expect, test, type Route } from "@playwright/test";

// Хвост дефекта 30.07 (аренда Виталия): закрывающий документ с будущей датой — ещё не долг
// (правило 4 канона ДЗ/КЗ). Бэкенд такой документ в банк не пускает, а UI не должен предлагать
// его к оплате. При этом ПРЯТАТЬ строку нельзя — иначе накладная молча исчезает из «к оплате»
// и менеджер ищет, куда она делась. Проверяем оба требования сразу.

const CP_ID = "22222222-2222-2222-2222-222222222222";
const IN_FORCE_ID = "33333333-3333-3333-3333-333333333333";
const PENDING_ID = "44444444-4444-4444-4444-444444444444";

function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function invoice(overrides: Record<string, unknown>) {
  return {
    id: IN_FORCE_ID,
    counterparty_id: CP_ID,
    counterparty_name: "ООО «ЭкоЦентр»",
    ledger_category_id: null,
    source: "manual",
    operational_scope: "warehouse",
    direction: "payable",
    number: "УТ-100",
    invoice_date: "2026-07-10",
    due_date: null,
    service_period_start: null,
    service_period_end: null,
    service_period_status: "ready",
    service_period_required: false,
    bank_payments_create_prepayment: false,
    amount: 1000,
    vat_total: 0,
    vat_breakdown: {},
    allocated: 0,
    remaining: 1000,
    payment_status: "unpaid",
    draft_id: null,
    barter_settlement_id: null,
    barter_role: null,
    iiko_push_status: "not_pushed",
    iiko_push_error: null,
    external_id: null,
    draft_status: null,
    draft_pays_via_safe: false,
    price_control_status: "clean",
    doc_kind: "closing",
    activation_status: "active",
    ...overrides,
  };
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
  await page.route("**/api/v1/settings**", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/counterparties/categories**", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/counterparties/registry**", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/warehouse/**", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/counterparties/invoices**", (route) =>
    fulfillJson(route, [
      invoice({}),
      invoice({
        id: PENDING_ID,
        number: "УТ-777",
        invoice_date: "2026-08-31",
        amount: 2000,
        remaining: 2000,
        activation_status: "pending",
      }),
    ]),
  );
});

test("будущий закрывающий документ виден в списке, но со статусом «Вступает в силу»", async ({
  page,
}) => {
  await page.goto("/warehouse/invoices");

  // Строка на месте — накладная не спрятана.
  await expect(page.getByText("УТ-777")).toBeVisible();
  // И сразу видно, почему её нельзя оплатить.
  await expect(page.getByText("Вступает в силу 31.08.2026")).toBeVisible();
  // Действующая накладная статус не меняет.
  await expect(page.getByText("УТ-100")).toBeVisible();
});

test("кнопка «Оплатить» не считает будущий документ оплачиваемым", async ({ page }) => {
  await page.goto("/warehouse/invoices");
  await expect(page.getByText("УТ-777")).toBeVisible();

  const checkboxes = page.getByRole("row").getByRole("checkbox");
  const pendingRow = page.getByRole("row").filter({ hasText: "УТ-777" });
  await pendingRow.getByRole("checkbox").click();

  // Выбран только будущий документ → платить нечего, кнопка выключена.
  const payButton = page.getByRole("button", { name: /^Оплатить/ });
  await expect(payButton).toBeDisabled();

  // Добавляем действующую → в счётчике ровно одна, будущий документ не попал.
  await page.getByRole("row").filter({ hasText: "УТ-100" }).getByRole("checkbox").click();
  await expect(payButton).toBeEnabled();
  await expect(payButton).toHaveText(/Оплатить \(1\)/);
  expect(await checkboxes.count()).toBeGreaterThan(0);
});
