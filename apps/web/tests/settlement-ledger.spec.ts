import { expect, test, type Route } from "@playwright/test";

// Сверка расчётов и просрочка закрывающих документов. Экран отвечает на вопрос «заплатили —
// закрыли ли документом?»: раньше это выяснялось случайно (УПД Микроэля за май нашли через два
// месяца), а всего таких денег на проде набралось 311 969 ₽ у десяти контрагентов.
//
// Отдельной вкладки «Разрывы» нет с 01.08.2026 (eb65edfb): срок и просрочка живут в состоянии
// «ждём документ» на «Признании расходов», а сверка открывается из «Остатков» кликом по имени.

const CP_ID = "22222222-2222-2222-2222-222222222222";
const NAUMCHENKO_ID = "33333333-3333-3333-3333-333333333333";
const MANGO_ID = "44444444-4444-4444-4444-444444444444";

function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function waitingItem(overrides: Record<string, unknown>) {
  return {
    source_kind: "legacy_prepayment",
    stage: "waiting_document",
    article_id: null,
    article_name: "Связь и интернет",
    invoice_id: null,
    invoice_number: null,
    document_kind: null,
    balance_type: "receivable",
    period_status: "confirmed",
    recognition_month: null,
    recognized: false,
    period_assumed: false,
    opening: false,
    note: null,
    settled: false,
    auto_recognition_on: null,
    document_amount: null,
    amount_mismatch: 0,
    can_recognize: false,
    recognize_blocked_reason: null,
    ...overrides,
  };
}

// Два платежа с прошедшим сроком документа и один, чей срок ещё не наступил.
const WAITING = [
  waitingItem({
    id: "pp-microel",
    counterparty_id: CP_ID,
    counterparty_name: 'ООО "МИКРОЭЛ"',
    amount: 3230,
    paid_amount: 3230,
    balance_amount: 3230,
    service_period_start: "2026-05-01",
    service_period_end: "2026-05-31",
    payment_date: "2026-05-20",
    expected_by: "2026-05-31",
    days_overdue: 62,
  }),
  waitingItem({
    id: "pp-naumchenko",
    counterparty_id: NAUMCHENKO_ID,
    counterparty_name: "ИП Наумченко Наталья Васильевна",
    amount: 9000,
    paid_amount: 9000,
    balance_amount: 9000,
    service_period_start: "2026-04-01",
    service_period_end: "2026-06-30",
    payment_date: "2026-07-30",
    expected_by: "2026-06-30",
    days_overdue: 32,
  }),
  waitingItem({
    id: "pp-mango",
    counterparty_id: MANGO_ID,
    counterparty_name: "ООО «Манго Телеком»",
    amount: 5000,
    paid_amount: 5000,
    balance_amount: 5000,
    service_period_start: "2026-07-01",
    service_period_end: "2026-07-31",
    payment_date: "2026-07-26",
    expected_by: "2026-08-15",
    days_overdue: 0,
  }),
];

/** Как /accounting/suppliers на бэкенде: плитки считаются по всем строкам, список — по stage. */
function accountingList(stage: string | null) {
  return {
    items: stage === null || stage === "waiting_document" ? WAITING : [],
    receivable_total: 17230,
    payable_total: 0,
    scheduled_total: 0,
    needs_review_total: 0,
    in_expense: { count: 0, amount: 0 },
    period_running: { count: 0, amount: 0 },
    waiting_document: { count: 3, amount: 17230 },
    needs_period: { count: 0, amount: 0 },
    in_expense_month: null,
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
    open_prepayments: net > 0 ? 1 : 0,
    unpaid_invoices: net < 0 ? 1 : 0,
    last_activity: "2026-07-30",
  };
}

const LEDGER_ROW_DEFAULTS = { self_billed: false, owner_settlement: false };

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
  self_billed_amount: 0,
  has_barter: false,
  rows: [
    {
      ...LEDGER_ROW_DEFAULTS,
      kind: "document",
      id: "d1",
      row_date: "2026-06-30",
      amount: 3230,
      title: "УПД № 5541",
      subtitle: null,
      period_start: null,
      period_end: null,
      period_assumed: false,
      uncovered: 0,
      status: "ok",
      expected_by: null,
      days_overdue: 0,
      balance_after: 3230,
      prepayment_id: null,
    },
    {
      ...LEDGER_ROW_DEFAULTS,
      kind: "payment",
      id: "p1",
      row_date: "2026-06-09",
      amount: 3230,
      title: "Т-Банк",
      subtitle: "Оплата интернета",
      period_start: "2026-06-01",
      period_end: "2026-06-30",
      period_assumed: false,
      uncovered: 0,
      status: "ok",
      expected_by: "2026-06-30",
      days_overdue: 0,
      balance_after: 6460,
      prepayment_id: null,
    },
    {
      ...LEDGER_ROW_DEFAULTS,
      kind: "payment",
      id: "p2",
      row_date: "2026-05-20",
      amount: 3230,
      title: "Т-Банк",
      subtitle: "Оплата интернета",
      period_start: "2026-05-01",
      period_end: "2026-05-31",
      period_assumed: true,
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
  // Регэксп, а не glob: нужен сам список, без /balances, /staff-payable и прочих подпутей.
  await page.route(/\/api\/v1\/accounting\/suppliers(\?.*)?$/, (route) =>
    fulfillJson(route, accountingList(new URL(route.request().url()).searchParams.get("stage"))),
  );
  await page.route(`**/api/v1/accounting/suppliers/${CP_ID}/ledger**`, (route) =>
    fulfillJson(route, LEDGER),
  );
  // Без хвоста «**»: /balances/as-of — другой ответ, и этот мок не должен его подменять.
  await page.route("**/api/v1/accounting/suppliers/balances", (route) =>
    fulfillJson(route, {
      items: [
        balance(CP_ID, 'ООО "МИКРОЭЛ"', 3230),
        balance(NAUMCHENKO_ID, "ИП Наумченко Наталья Васильевна", 9000),
        balance("55555555-5555-5555-5555-555555555555", "ООО «Поставка овощей»", -1234.56),
      ],
      receivable_total: 12230,
      // С копейками нарочно: при нулевом слагаемом склейка 0 + "57390.00" даёт "057390.00",
      // Intl читает это как 57 390, и тест на «не число» проходил бы без всякого приведения.
      payable_total: 1234.56,
    }),
  );
  await page.route("**/api/v1/accounting/suppliers/staff-payable**", (route) =>
    fulfillJson(route, {
      as_of: "2026-08-01",
      total: 0,
      receivable_total: 0,
      salary_total: 0,
      vacation_total: 0,
      fund_total: 0,
      fund_current_year_total: 0,
      fund_prior_years_total: 0,
      production_deposit_total: 0,
      courier_deposit_total: 0,
      deposit_total: 0,
      items: [],
    }),
  );
  await page.route("**/api/v1/accounting/utilities/calendar**", (route) =>
    fulfillJson(route, { items: [] }),
  );
  await page.route("**/api/v1/taxes/debt**", (route) =>
    fulfillJson(route, {
      as_of: "2026-08-01",
      // Decimal приезжает СТРОКОЙ — на этом плитка кредиторки и ломалась в «не число ₽».
      payable_total: "57390.00",
      items: [],
      wallet: {
        as_of: "2026-08-01",
        inflow: "0",
        recognized: "0.00",
        balance: "0",
        shortfall: "0",
      },
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

test("«ждём документ» показывает, у кого срок документа прошёл и на сколько дней", async ({
  page,
}) => {
  await page.goto("/dz-kz");
  await page.getByRole("tab", { name: /Признание расходов/ }).click();
  const tile = page.getByRole("button", { name: /Ждём документ/ });
  await tile.click();

  await expect(page.getByRole("row").filter({ hasText: 'ООО "МИКРОЭЛ"' })).toContainText(
    "нет 62 дн",
  );
  await expect(page.getByRole("row").filter({ hasText: "Наумченко" })).toContainText("нет 32 дн");
  // Срок ещё не прошёл — не просрочка, а дата, до которой ждём.
  await expect(page.getByRole("row").filter({ hasText: "Манго" })).toContainText(
    "ждём до 15.08.2026",
  );
  // Зависшие деньги — цифра, ради которой экран открывают: сумма и число платежей на плитке.
  await expect(tile).toContainText("17 230");
  await expect(tile.getByText("3", { exact: true })).toBeVisible();
});

test("имя в «Остатках» открывает сверку с бегущим остатком", async ({ page }) => {
  await page.goto("/dz-kz");
  await page.getByRole("button", { name: 'ООО "МИКРОЭЛ"', exact: true }).click();

  const card = page.getByRole("dialog");
  // Карточка открывается сразу на «Сверке», а не на общей информации.
  await expect(card.getByRole("tab", { name: "Сверка" })).toHaveAttribute("aria-selected", "true");
  await expect(card.getByText("Остаток расчётов")).toBeVisible();
  // Май подсвечен как месяц без документов…
  await expect(card.getByText("без документов 3 230,00 ₽")).toBeVisible();
  // …а сам платёж — красным статусом с числом дней.
  await expect(card.getByText("документа нет · 62 дн")).toBeVisible();
  // Июнь закрыт полностью — по нему претензий нет.
  await expect(card.getByText("закрыт полностью")).toBeVisible();
  // Дата УПД не выдаётся за период услуги, а расчётный период платежа помечен как гипотеза.
  await expect(card.getByText("не заполнен")).toBeVisible();
  await expect(card.getByText("≈ май 2026")).toBeVisible();
});

test("кредиторка не превращается в «не число», когда есть налоговый долг", async ({ page }) => {
  await page.goto("/dz-kz");

  // /taxes/debt отдаёт Decimal строкой: без приведения к числу «+» склеивал строки
  // (1234.56 + "57390.00" → "1234.5657390.00") и главная плитка показывала «не число ₽».
  const payableCard = page
    .locator("div")
    .filter({ hasText: /^Кредиторская задолженность/ })
    .first();
  // 1 234,56 поставщикам + 57 390 налогов = 58 624,56 → «58 625 ₽» при округлении до рубля.
  await expect(payableCard).toContainText("58 625");
  await expect(payableCard).not.toContainText("не число");
});
