import { expect, test, type Route } from "@playwright/test";

// Сверка расчётов и сводка разрывов. Экран отвечает на вопрос «заплатили — закрыли ли
// документом?»: раньше это выяснялось случайно (УПД Микроэля за май нашли через два месяца),
// а всего таких денег на проде набралось 311 969 ₽ у десяти контрагентов.

const CP_ID = "22222222-2222-2222-2222-222222222222";

function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

const GAPS = {
  as_of: "2026-08-01",
  total_amount: 12230,
  items: [
    {
      counterparty_id: CP_ID,
      counterparty_name: 'ООО "МИКРОЭЛ"',
      period_start: "2026-05-01",
      period_end: "2026-05-31",
      amount: 3230,
      expected_by: "2026-05-31",
      days_overdue: 62,
      payments: 1,
      last_payment_date: "2026-05-20",
    },
    {
      counterparty_id: "33333333-3333-3333-3333-333333333333",
      counterparty_name: "ИП Наумченко Наталья Васильевна",
      period_start: "2026-04-01",
      period_end: "2026-06-30",
      amount: 9000,
      expected_by: "2026-06-30",
      days_overdue: 32,
      payments: 1,
      last_payment_date: "2026-07-30",
    },
  ],
};

const LEDGER = {
  counterparty_id: CP_ID,
  counterparty_name: 'ООО "МИКРОЭЛ"',
  contour: "service",
  contour_manual: false,
  closing_doc_expected_day: null,
  opening_balance: 0,
  closing_balance: 3230,
  total_paid: 6460,
  total_documented: 3230,
  overdue_amount: 3230,
  has_barter: false,
  rows: [
    {
      kind: "document",
      id: "d1",
      row_date: "2026-06-30",
      amount: 3230,
      title: "УПД № 5541",
      subtitle: null,
      period_start: "2026-06-01",
      period_end: "2026-06-30",
      uncovered: 0,
      status: "ok",
      expected_by: null,
      days_overdue: 0,
      balance_after: 3230,
      prepayment_id: null,
    },
    {
      kind: "payment",
      id: "p1",
      row_date: "2026-06-09",
      amount: 3230,
      title: "Т-Банк",
      subtitle: "Оплата интернета",
      period_start: "2026-06-01",
      period_end: "2026-06-30",
      uncovered: 0,
      status: "ok",
      expected_by: "2026-06-30",
      days_overdue: 0,
      balance_after: 6460,
      prepayment_id: null,
    },
    {
      kind: "payment",
      id: "p2",
      row_date: "2026-05-20",
      amount: 3230,
      title: "Т-Банк",
      subtitle: "Оплата интернета",
      period_start: "2026-05-01",
      period_end: "2026-05-31",
      uncovered: 3230,
      status: "overdue",
      expected_by: "2026-05-31",
      days_overdue: 62,
      balance_after: 3230,
      prepayment_id: null,
    },
  ],
  months: [
    { month: "2026-06", paid: 3230, documented: 3230, gap: 0, has_overdue: false },
    { month: "2026-05", paid: 3230, documented: 0, gap: 3230, has_overdue: true },
  ],
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
  await page.route("**/api/v1/settings**", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/accounting/suppliers/gaps**", (route) => fulfillJson(route, GAPS));
  await page.route(`**/api/v1/accounting/suppliers/${CP_ID}/ledger**`, (route) =>
    fulfillJson(route, LEDGER),
  );
  await page.route("**/api/v1/accounting/suppliers/balances**", (route) =>
    fulfillJson(route, { items: [], receivable_total: 0, payable_total: 0 }),
  );
  await page.route("**/api/v1/accounting/suppliers/staff-payable**", (route) =>
    fulfillJson(route, { as_of: "2026-08-01", total: 0, receivable_total: 0, items: [] }),
  );
  await page.route("**/api/v1/accounting/suppliers?**", (route) =>
    fulfillJson(route, {
      items: [],
      receivable_total: 0,
      payable_total: 0,
      scheduled_total: 0,
      needs_review_total: 0,
    }),
  );
  await page.route("**/api/v1/taxes/debt**", (route) =>
    fulfillJson(route, {
      as_of: "2026-08-01",
      // Decimal приезжает СТРОКОЙ — на этом плитка кредиторки и ломалась в «не число ₽».
      payable_total: "57390.00",
      items: [],
      wallet: { as_of: "2026-08-01", inflow: "0", recognized: "0.00", balance: "0", shortfall: "0" },
    }),
  );
  await page.route(`**/api/v1/counterparties/${CP_ID}`, (route) =>
    fulfillJson(route, {
      counterparty_id: CP_ID,
      name: 'ООО "МИКРОЭЛ"',
      inn: "6143049372",
      type: "legal_entity",
      status: "active",
      relationship: "official",
      barter_balance: 0,
      profile: {
        ledger_category_id: null,
        relationship: "official",
        relationship_manual: false,
        brand_group: null,
        internal_name: null,
        payment_delay_days: null,
        payment_due_day_of_month: null,
        manager_name: null,
        manager_phone: null,
        default_dds_article_id: null,
        confirm_no_dds_article: true,
        service_period_required: true,
        default_service_period_offset_months: null,
        bank_payments_create_prepayment: false,
        closing_doc_expected_day: null,
        settlement_contour: null,
        requisites: {},
        requisites_verified: false,
        kassa_enabled: false,
        status: "active",
      },
      aliases: [],
      collection_sources: [],
      routing_rules: [],
      invoices: [],
      drafts: [],
    }),
  );
});

test("сводка разрывов показывает, у кого нет закрывающего документа", async ({ page }) => {
  await page.goto("/dz-kz");
  await page.getByRole("tab", { name: /Разрывы/ }).click();

  await expect(page.getByRole("row").filter({ hasText: 'ООО "МИКРОЭЛ"' })).toContainText("62 дн");
  await expect(page.getByRole("row").filter({ hasText: "Наумченко" })).toContainText("32 дн");
  // Итог виден и в бейдже вкладки, и под таблицей — это цифра, ради которой экран открывают.
  await expect(page.getByRole("tab", { name: /Разрывы/ })).toContainText("12 230");
});

test("клик по разрыву открывает сверку с бегущим остатком", async ({ page }) => {
  await page.goto("/dz-kz");
  await page.getByRole("tab", { name: /Разрывы/ }).click();
  await page.getByRole("row").filter({ hasText: 'ООО "МИКРОЭЛ"' }).click();

  const card = page.getByRole("dialog");
  await expect(card.getByText("Остаток расчётов")).toBeVisible();
  // Май подсвечен как месяц без документов…
  await expect(card.getByText("без документов 3 230,00 ₽")).toBeVisible();
  // …а сам платёж — красным статусом с числом дней.
  await expect(card.getByText("документа нет · 62 дн")).toBeVisible();
  // Июнь закрыт полностью — по нему претензий нет.
  await expect(card.getByText("закрыт полностью")).toBeVisible();
});

test("кредиторка не превращается в «не число», когда есть налоговый долг", async ({ page }) => {
  await page.goto("/dz-kz");

  // /taxes/debt отдаёт Decimal строкой: без приведения к числу «+» склеивал строки и
  // главная плитка показывала «не число ₽».
  const payableCard = page.locator("div").filter({ hasText: /^Кредиторская задолженность/ }).first();
  await expect(payableCard).not.toContainText("не число");
  await expect(payableCard).toContainText("57 390");
});
