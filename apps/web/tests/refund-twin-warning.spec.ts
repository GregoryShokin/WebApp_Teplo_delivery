import { expect, test, type Page, type Route } from "@playwright/test";

// Сторож задвоенного возврата (25.09.2026).
//
// Возврат переплаты гасит аванс поставщика без аллокации: пересборка берёт ВСЕ его приходы с
// возвратной статьёй. Провести один возврат дважды — «Новым платежом» в Сейф и разбором
// выписки — значит погасить аванс вдвое (1 000 − 300 − 300). Код расчёта прав, ошибка — в
// задвоенном приходе, поэтому окна не запрещают, а предупреждают ДО проведения.
//
// Проверяем, что предупреждение доезжает во все три окна (разбор операции, «Новый платёж»,
// разбор кейса собственником), спрашивает бэкенд правильным каналом (операция выписки — своей
// операцией, наличный приход — кошельком) и не блокирует кнопку.

const REFUND_ARTICLE_ID = "11111111-1111-1111-1111-111111111111";
const CP_ID = "22222222-2222-2222-2222-222222222222";
const OP_ID = "33333333-3333-3333-3333-333333333333";
const BANK_WALLET_ID = "44444444-4444-4444-4444-444444444444";
const SAFE_WALLET_ID = "55555555-5555-5555-5555-555555555555";
const REFUND_CODE = "vozvrat_pereplaty_ot_postavschikov";

function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockAuth(page: Page) {
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
}

async function mockTwins(page: Page, twin: Record<string, unknown>) {
  const asked: URLSearchParams[] = [];
  await page.route("**/api/v1/dds/refund-twins**", (route) => {
    asked.push(new URL(route.request().url()).searchParams);
    return fulfillJson(route, { items: [twin], window_days: 7 });
  });
  return asked;
}

test("разбор выписки возвратом предупреждает о том же возврате наличными", async ({ page }) => {
  await mockAuth(page);
  await page.route("**/api/v1/dds/articles**", (route) =>
    fulfillJson(route, [
      {
        id: REFUND_ARTICLE_ID,
        code: REFUND_CODE,
        name: "Возврат переплаты от поставщиков",
        movement_type: "inflow",
        activity_type: "operating",
        parent_id: null,
        is_active: true,
        kassa_enabled: false,
        location_required: false,
        lease_bound: false,
        asset_link_kind: null,
        description: null,
        aliases: [],
      },
    ]),
  );
  await page.route("**/api/v1/dds/wallets**", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/counterparties/registry**", (route) =>
    fulfillJson(route, [
      {
        counterparty_id: CP_ID,
        name: "ИП Скачкова",
        inn: "610000000001",
        status: "active",
        relationship: "official",
      },
    ]),
  );
  await page.route("**/api/v1/dds/journal**", (route) =>
    fulfillJson(route, {
      items: [
        {
          kind: "operation",
          id: OP_ID,
          bank_operation_id: OP_ID,
          status: "classified",
          operation_date: "2026-09-22",
          occurred_at: "2026-09-22T12:00:00+03:00",
          direction: "in",
          amount: "300.00",
          article_id: null,
          counterparty_id: null,
          wallet_id: BANK_WALLET_ID,
          provider: "tbank",
          payment_purpose: "Возврат переплаты по счёту 17",
          counterparty_name_raw: "ИП Скачкова",
          counterparty_inn_raw: "610000000001",
          is_card: false,
        },
      ],
    }),
  );
  await page.route(`**/api/v1/dds/operations/${OP_ID}/split`, (route) =>
    fulfillJson(route, {
      bank_operation_id: OP_ID,
      amount: "300.00",
      classification_status: "classified",
      lines: [
        {
          cashflow_transaction_id: "66666666-6666-6666-6666-666666666666",
          article_id: REFUND_ARTICLE_ID,
          amount: "300.00",
          counterparty_id: CP_ID,
          invoice_id: null,
          employee_id: null,
          location_id: null,
          lease_id: null,
          asset_id: null,
        },
      ],
    }),
  );
  const asked = await mockTwins(page, {
    transaction_id: "77777777-7777-7777-7777-777777777777",
    operation_date: "2026-09-20",
    amount: "300.00",
    wallet_name: "Сейф",
    channel: "cash",
    source_kind: "new_payment_income",
  });

  await page.goto("/dds");
  await page.getByRole("tab", { name: /Журнал ДДС/ }).click();
  await page.getByRole("row").filter({ hasText: "Возврат переплаты по счёту 17" }).first().click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();

  const warning = dialog.getByRole("alert").filter({ hasText: "Похоже на задвоение" });
  await expect(warning).toBeVisible();
  await expect(warning).toContainText("от 20.09.2026 наличными на «Сейф» («Новый платёж»)");
  await expect(warning).toContainText("аванс погасится дважды");
  // Предупреждение, не запрет: два возврата одной суммы бывают.
  await expect(dialog.getByRole("button", { name: "Разнести" })).toBeEnabled();

  // Операция выписки спрашивает своей операцией — канал и дату бэкенд берёт из неё.
  const params = asked.at(-1)!;
  expect(params.get("bank_operation_id")).toBe(OP_ID);
  expect(params.get("counterparty_id")).toBe(CP_ID);
  expect(params.get("amount")).toBe("300.00");
  expect(params.get("wallet_id")).toBeNull();

  await dialog.screenshot({ path: "test-results/refund-twin-operation.png" });
});

test("«Новый платёж» — возврат наличными предупреждает о той же выписке", async ({ page }) => {
  await mockAuth(page);
  await page.route("**/api/v1/finance/payments**", (route) =>
    fulfillJson(route, { scope: "active", buckets: [], items: [] }),
  );
  await page.route("**/api/v1/dds/new-payment/context**", (route) =>
    fulfillJson(route, {
      articles: [
        {
          id: REFUND_ARTICLE_ID,
          code: REFUND_CODE,
          name: "Возврат переплаты от поставщиков",
          flow: "income",
          activity: "operating",
          counterparties: [],
          location_required: false,
          lease_bound: false,
          asset_link_kind: null,
        },
      ],
      counterparties: [],
      wallets: [
        {
          id: SAFE_WALLET_ID,
          code: "safe",
          name: "Сейф",
          bank_code: null,
          kind: "cash",
          location: "safe",
        },
      ],
      employees: [],
    }),
  );
  await page.route("**/api/v1/counterparties/registry**", (route) =>
    fulfillJson(route, [
      {
        counterparty_id: CP_ID,
        name: "ИП Скачкова",
        inn: "610000000001",
        status: "active",
        relationship: "official",
      },
    ]),
  );
  const asked = await mockTwins(page, {
    transaction_id: "88888888-8888-8888-8888-888888888888",
    operation_date: "2026-09-22",
    amount: "300.00",
    wallet_name: "Т-Банк",
    channel: "bank",
    source_kind: "bank_operation",
  });

  await page.goto("/");
  await page.getByRole("button", { name: "Активные платежи" }).click();
  const payments = page.getByRole("dialog").filter({ hasText: "Активные платежи" });
  await payments.getByRole("button", { name: "Создать", exact: true }).click();
  const dialog = page.getByRole("dialog").filter({ hasText: "Новый платёж" });
  await expect(dialog).toBeVisible();

  await dialog.getByRole("button", { name: "Возврат переплаты от поставщиков" }).first().click();
  await dialog.getByLabel("Сумма, ₽").fill("300");
  await dialog.getByRole("button", { name: /ИП Скачкова/ }).click();

  await expect(dialog.getByText(/Похоже на задвоение/)).toBeVisible();
  await expect(dialog.getByText(/от 22\.09\.2026 по банку на «Т-Банк» \(выписка\)/)).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Провести поступление" })).toBeEnabled();

  // Наличный приход спрашивает кошельком; дату бэкенд берёт сегодняшнюю по Москве.
  const params = asked.at(-1)!;
  expect(params.get("wallet_id")).toBe(SAFE_WALLET_ID);
  expect(params.get("amount")).toBe("300.00");
  expect(params.get("bank_operation_id")).toBeNull();

  await dialog.screenshot({ path: "test-results/refund-twin-new-payment.png" });
});

test("разбор кейса собственником тоже предупреждает — так пришёл возврат Скачковой", async ({
  page,
}) => {
  await mockAuth(page);
  await page.route("**/api/v1/dds/articles**", (route) =>
    fulfillJson(route, [
      {
        id: REFUND_ARTICLE_ID,
        code: REFUND_CODE,
        name: "Возврат переплаты от поставщиков",
        movement_type: "inflow",
        activity_type: "operating",
        parent_id: null,
        is_active: true,
        kassa_enabled: false,
        location_required: false,
        lease_bound: false,
        asset_link_kind: null,
        description: null,
        aliases: [],
      },
    ]),
  );
  await page.route("**/api/v1/counterparties/registry**", (route) =>
    fulfillJson(route, [
      {
        counterparty_id: CP_ID,
        name: "ИП Скачкова",
        inn: "610000000001",
        status: "active",
        relationship: "official",
      },
    ]),
  );
  await page.route("**/api/v1/dds/owner-review**", (route) =>
    fulfillJson(route, {
      total: 1,
      items: [
        {
          id: "99999999-9999-9999-9999-999999999999",
          kind: "unclassified_operation",
          status: "open",
          provider: "tbank",
          bank_operation_id: OP_ID,
          payload: {},
          created_at: "2026-09-22T12:00:00+03:00",
          operation: {
            id: OP_ID,
            provider: "tbank",
            provider_operation_id: "op-1",
            account_id: null,
            operation_date: "2026-09-22",
            posted_at: null,
            direction: "in",
            amount: "300.00",
            currency: "RUB",
            counterparty_name_raw: "ИП Скачкова",
            counterparty_inn_raw: "610000000001",
            counterparty_account_raw: null,
            payment_purpose: "Возврат переплаты по счёту 17",
            document_number: null,
            classification_status: "needs_review",
            cashflow_transaction_id: null,
            transfer_group_id: null,
            raw_payload: null,
            is_card: false,
          },
        },
      ],
    }),
  );
  const asked = await mockTwins(page, {
    transaction_id: "77777777-7777-7777-7777-777777777777",
    operation_date: "2026-09-20",
    amount: "300.00",
    wallet_name: "Сейф",
    channel: "cash",
    source_kind: "new_payment_income",
  });

  await page.goto("/dds/owner-review");
  await expect(page.getByText("Возврат переплаты по счёту 17")).toBeVisible();
  // Пока статья и контрагент не выбраны — спрашивать не о чем.
  await expect(page.getByText(/Похоже на задвоение/)).toBeHidden();

  const selects = page.getByRole("combobox");
  await selects.nth(1).click();
  await page.getByRole("option", { name: /Возврат переплаты от поставщиков/ }).click();
  await selects.nth(2).click();
  await page.getByRole("option", { name: /ИП Скачкова/ }).click();

  const warning = page.getByRole("alert").filter({ hasText: "Похоже на задвоение" });
  await expect(warning).toBeVisible();
  await expect(warning).toContainText("наличными на «Сейф» («Новый платёж»)");
  expect(asked.at(-1)!.get("bank_operation_id")).toBe(OP_ID);

  await page.screenshot({ path: "test-results/refund-twin-owner-review.png", fullPage: true });
});
