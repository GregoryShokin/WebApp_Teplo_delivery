import { expect, test, type Page, type Route } from "@playwright/test";

// Сверка обязана сходиться с «Остатками», и то, что баланс видит без документа, стоит в ней
// своими строками: выплата дивидендов (остаток не двигает), возврат денег, закрытие предоплаты
// решением человека, оплата документа другого контрагента. Найдено 25.09.2026 на копии прода:
// без этих строк 6 карточек расходились с «Остатками» на 154 974,73 ₽.

const OWNER_ID = "44444444-4444-4444-4444-444444444444";
const SUPPLIER_ID = "55555555-5555-5555-5555-555555555555";

function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function row(overrides: Record<string, unknown>) {
  return {
    subtitle: null,
    period_start: null,
    period_end: null,
    period_assumed: false,
    uncovered: 0,
    status: "ok",
    expected_by: null,
    days_overdue: 0,
    prepayment_id: null,
    self_billed: false,
    owner_settlement: false,
    closed_by: null,
    ...overrides,
  };
}

function ledger(id: string, name: string, closing: number, rows: unknown[], months: unknown[]) {
  return {
    counterparty_id: id,
    counterparty_name: name,
    contour: "goods",
    contour_manual: false,
    closing_doc_expected_day: null,
    opening_balance: 0,
    closing_balance: closing,
    total_paid: 0,
    total_documented: 0,
    overdue_amount: 0,
    self_billed_amount: 0,
    has_barter: false,
    rows,
    months,
  };
}

// Григорий: входящий остаток — долг собственника, дивиденды — выплата, а не долг.
const OWNER_LEDGER = ledger(
  OWNER_ID,
  "Григорий",
  1020000,
  [
    row({
      kind: "payout",
      id: "div",
      row_date: "2026-08-19",
      amount: 50000,
      title: "Выплата дивидендов",
      subtitle: "Сейф",
      balance_after: 1020000,
      owner_settlement: true,
    }),
    row({
      kind: "payment",
      id: "opening",
      row_date: "2026-08-02",
      amount: 1020000,
      title: "Входящий остаток",
      subtitle: "Входящий остаток на 01.07.2026",
      balance_after: 1020000,
      owner_settlement: true,
    }),
  ],
  [{ month: "2026-08", paid: 1020000, documented: 0, gap: 0, has_overdue: false }],
);

// Поставщик: закрытие решением, возврат денег и платёж, закрывший чужой документ.
const SUPPLIER_LEDGER = ledger(
  SUPPLIER_ID,
  "Поставка овощей",
  -17398,
  [
    row({
      kind: "document",
      id: "doc",
      row_date: "2026-09-18",
      amount: 17398,
      title: "УПД № 515328",
      subtitle: "не оплачен",
      uncovered: 17398,
      balance_after: -17398,
    }),
    row({
      kind: "refund",
      id: "refund",
      row_date: "2026-09-10",
      amount: 2822,
      title: "Возврат денег",
      subtitle: "Возврат переплаты",
      balance_after: 0,
    }),
    row({
      kind: "transfer",
      id: "transfer",
      row_date: "2026-09-05",
      amount: 3561.6,
      title: "Оплачен документ «ИП Скачкова»",
      subtitle: "УПД № DX001312A: долг гасится в сверке с ним — проверьте разметку в ДДС",
      balance_after: 2822,
    }),
    row({
      kind: "payment",
      id: "pay-transfer",
      row_date: "2026-09-05",
      amount: 3561.6,
      title: "Т-Банк",
      subtitle: "Оплата поставщикам",
      balance_after: 6383.6,
    }),
    row({
      kind: "payment",
      id: "pay-refund",
      row_date: "2026-09-01",
      amount: 2822,
      title: "Т-Банк",
      subtitle: "Оплата поставщикам",
      balance_after: 2822,
      closed_by: "refund",
    }),
    row({
      kind: "closure",
      id: "closure",
      row_date: "2026-07-20",
      amount: 38479,
      title: "Закрыто без документа",
      subtitle: "Исторический расчёт за поставки до текущего контура ДЗ/КЗ",
      balance_after: 0,
    }),
    row({
      kind: "payment",
      id: "pay-closed",
      row_date: "2026-06-23",
      amount: 38479,
      title: "Т-Банк",
      subtitle: "Оплата поставщикам",
      balance_after: 38479,
      closed_by: "decision",
    }),
  ],
  [
    { month: "2026-09", paid: 6383.6, documented: 17398, gap: 0, has_overdue: false },
    { month: "2026-07", paid: 0, documented: 0, gap: 0, has_overdue: false },
    { month: "2026-06", paid: 38479, documented: 0, gap: 0, has_overdue: false },
  ],
);

function card(id: string, name: string) {
  return {
    counterparty_id: id,
    name,
    inn: null,
    type: "individual",
    status: "active",
    relationship: "informal",
    barter_balance: 0,
    profile: {
      ledger_category_id: null,
      relationship: "informal",
      relationship_manual: false,
      brand_group: null,
      internal_name: null,
      payment_delay_days: null,
      payment_due_day_of_month: null,
      manager_name: null,
      manager_phone: null,
      default_dds_article_id: null,
      confirm_no_dds_article: true,
      service_period_required: false,
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
  };
}

function balance(id: string, name: string, net: number) {
  return {
    counterparty_id: id,
    name,
    inn: null,
    receivable: Math.max(net, 0),
    payable: Math.max(-net, 0),
    net,
    open_prepayments: 0,
    unpaid_invoices: 0,
    last_activity: "2026-09-18",
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
  await page.route("**/api/v1/accounting/suppliers/balances", (route) =>
    fulfillJson(route, {
      items: [
        balance(OWNER_ID, "Григорий", 1020000),
        balance(SUPPLIER_ID, "Поставка овощей", -17398),
      ],
      receivable_total: 1020000,
      payable_total: 17398,
    }),
  );
  await page.route(`**/api/v1/accounting/suppliers/${OWNER_ID}/ledger**`, (route) =>
    fulfillJson(route, OWNER_LEDGER),
  );
  await page.route(`**/api/v1/accounting/suppliers/${SUPPLIER_ID}/ledger**`, (route) =>
    fulfillJson(route, SUPPLIER_LEDGER),
  );
  await page.route(`**/api/v1/counterparties/${OWNER_ID}`, (route) =>
    fulfillJson(route, card(OWNER_ID, "Григорий")),
  );
  await page.route(`**/api/v1/counterparties/${SUPPLIER_ID}`, (route) =>
    fulfillJson(route, card(SUPPLIER_ID, "Поставка овощей")),
  );
});

async function openLedger(page: Page, name: string) {
  await page.goto("/dz-kz");
  await page.getByRole("button", { name, exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("Остаток расчётов")).toBeVisible();
  return dialog;
}

test("дивиденды видны в сверке, но остаток собственника не двигают", async ({ page }) => {
  const dialog = await openLedger(page, "Григорий");

  const payout = dialog.getByRole("row").filter({ hasText: "Выплата дивидендов" });
  await expect(payout).toContainText("выплата собственнику · не долг");
  // Без «+»: плюс читался бы как аванс собственнику, то есть как его долг.
  await expect(payout).not.toContainText("+");
  await expect(payout).toContainText("1 020 000");
  // Подытог месяца называет выплату — иначе непонятно, откуда в августе строка на 50 000.
  await expect(dialog.getByText(/дивиденды 50\s000,00/)).toBeVisible();
});

test("возврат, закрытие решением и чужой документ гасят остаток своими строками", async ({
  page,
}) => {
  const dialog = await openLedger(page, "Поставка овощей");

  // По подписи, а не по заголовку: «закрыто без документа» есть и в подытоге месяца.
  const closure = dialog.getByRole("row").filter({ hasText: "Исторический расчёт" });
  await expect(closure).toContainText("Закрыто без документа");
  await expect(closure).toContainText("решение человека · без документа");
  await expect(closure).toContainText("−38 479");
  // Сам платёж честно назван закрытым решением, а не документом, которого не было.
  await expect(
    dialog.getByRole("row").filter({ hasText: "38 479" }).filter({ hasText: "закрыт решением" }),
  ).toHaveCount(1);

  await expect(dialog.getByRole("row").filter({ hasText: "Возврат денег" })).toContainText(
    "деньги вернулись · аванс погашен",
  );
  await expect(dialog.getByText("деньги вернули", { exact: true })).toBeVisible();

  const transfer = dialog.getByRole("row").filter({ hasText: "Оплачен документ «ИП Скачкова»" });
  await expect(transfer).toContainText("долг гасится у получателя");
  await expect(transfer).toContainText("DX001312A");

  await expect(dialog.getByText(/возвраты 2\s822,00/)).toBeVisible();
  await expect(dialog.getByText(/оплачены чужие документы 3\s561,60/)).toBeVisible();
  await expect(dialog.getByText(/закрыто без документа 38\s479,00/)).toBeVisible();
});
