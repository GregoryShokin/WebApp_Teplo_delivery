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
//
// И решение владельца 25.09: возврат переплаты не запоминается правилом — авторазметка гасила
// бы аванс фоном, мимо сторожа. Галочка «Запомнить» при возвратной статье заблокирована в обоих
// окнах разбора, и в запрос уходит remember_as_rule: false.

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

const REFUND_ARTICLE = {
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
};

const SKACHKOVA = {
  counterparty_id: CP_ID,
  name: "ИП Скачкова",
  inn: "610000000001",
  status: "active",
  relationship: "official",
};

const OTHER_CP_ID = "22222222-2222-2222-2222-222222222223";
const LEMA = {
  counterparty_id: OTHER_CP_ID,
  name: "ООО Лема",
  inn: "610000000002",
  status: "active",
  relationship: "official",
};

// Операция выписки 300 ₽, уже разнесённая возвратными долями (суммы долей — `shares`, их
// контрагенты — `counterparties`, по умолчанию все доли Скачковой).
async function mockClassifiedOperation(
  page: Page,
  shares: string[],
  counterparties: string[] = shares.map(() => CP_ID),
) {
  await mockAuth(page);
  await page.route("**/api/v1/dds/articles**", (route) => fulfillJson(route, [REFUND_ARTICLE]));
  await page.route("**/api/v1/dds/wallets**", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/counterparties/registry**", (route) =>
    fulfillJson(route, [SKACHKOVA, LEMA]),
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
      lines: shares.map((amount, index) => ({
        cashflow_transaction_id: `66666666-6666-6666-6666-66666666666${index}`,
        article_id: REFUND_ARTICLE_ID,
        amount,
        counterparty_id: counterparties[index],
        invoice_id: null,
        employee_id: null,
        location_id: null,
        lease_id: null,
        asset_id: null,
      })),
    }),
  );
}

async function openOperationDialog(page: Page) {
  await page.goto("/dds");
  await page.getByRole("tab", { name: /Журнал ДДС/ }).click();
  await page.getByRole("row").filter({ hasText: "Возврат переплаты по счёту 17" }).first().click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  return dialog;
}

test("разбор выписки возвратом предупреждает о том же возврате наличными", async ({ page }) => {
  await mockClassifiedOperation(page, ["300.00"]);
  const asked = await mockTwins(page, {
    transaction_id: "77777777-7777-7777-7777-777777777777",
    operation_date: "2026-09-20",
    amount: "300.00",
    wallet_name: "Сейф",
    channel: "cash",
    source_kind: "new_payment_income",
  });

  const dialog = await openOperationDialog(page);

  const warning = dialog.getByRole("alert").filter({ hasText: "Похоже на задвоение" });
  await expect(warning).toBeVisible();
  await expect(warning).toContainText("у контрагента «ИП Скачкова»");
  await expect(warning).toContainText("от 20.09.2026 наличными на «Сейф» («Новый платёж»)");
  await expect(warning).toContainText("аванс погасится дважды");
  // Предупреждение, не запрет: два возврата одной суммы бывают.
  await expect(dialog.getByRole("button", { name: "Разнести" })).toBeEnabled();
  // А вот правилом возврат не запоминается: фоном он прошёл бы мимо этого предупреждения.
  await expect(dialog.getByRole("checkbox", { name: /Запомнить/ })).toBeDisabled();
  await expect(dialog.getByText(/Возврат переплаты правилом не запоминаем/)).toBeVisible();

  // Операция выписки спрашивает своей операцией — канал и дату бэкенд берёт из неё.
  const params = asked.at(-1)!;
  expect(params.get("bank_operation_id")).toBe(OP_ID);
  expect(params.get("counterparty_id")).toBe(CP_ID);
  expect(params.get("amount")).toBe("300.00");
  expect(params.get("wallet_id")).toBeNull();

  await dialog.screenshot({ path: "test-results/refund-twin-operation.png" });
});

test("доли 150 + 150 спрашивают одним возвратом на 300 — и видят два наличных прихода", async ({
  page,
}) => {
  await mockClassifiedOperation(page, ["150.00", "150.00"]);
  const asked: URLSearchParams[] = [];
  await page.route("**/api/v1/dds/refund-twins**", (route) => {
    asked.push(new URL(route.request().url()).searchParams);
    return fulfillJson(route, {
      combined: true,
      window_days: 7,
      items: [
        {
          transaction_id: "77777777-7777-7777-7777-777777777771",
          operation_date: "2026-09-20",
          amount: "150.00",
          wallet_name: "Сейф",
          channel: "cash",
          source_kind: "new_payment_income",
        },
        {
          transaction_id: "77777777-7777-7777-7777-777777777772",
          operation_date: "2026-09-21",
          amount: "150.00",
          wallet_name: "Сейф",
          channel: "cash",
          source_kind: "new_payment_income",
        },
      ],
    });
  });

  const dialog = await openOperationDialog(page);
  const warning = dialog.getByRole("alert").filter({ hasText: "Похоже на задвоение" });
  await expect(warning).toBeVisible();
  await expect(warning).toContainText("вместе дают ту же сумму");
  await expect(warning).toContainText("от 21.09.2026 наличными");

  // Пересборка гасит аванс всеми долями сразу — спрашиваем их суммой, а не строкой по 150.
  expect(new Set(asked.map((params) => params.get("amount")))).toEqual(new Set(["300.00"]));
});

test("возвраты двум контрагентам — каждое предупреждение называет своего", async ({ page }) => {
  await mockClassifiedOperation(page, ["150.00", "150.00"], [CP_ID, OTHER_CP_ID]);
  const asked = await mockTwins(page, {
    transaction_id: "77777777-7777-7777-7777-777777777777",
    operation_date: "2026-09-20",
    amount: "150.00",
    wallet_name: "Сейф",
    channel: "cash",
    source_kind: "new_payment_income",
  });
  const duplicateKeys: string[] = [];
  page.on("console", (message) => {
    if (message.text().includes("same key")) duplicateKeys.push(message.text());
  });

  const dialog = await openOperationDialog(page);
  const warnings = dialog.getByRole("alert").filter({ hasText: "Похоже на задвоение" });
  // Совпадения у обоих одинаковые по сумме и дате — без имени это два неотличимых текста.
  await expect(warnings).toHaveCount(2);
  await expect(warnings.filter({ hasText: "у контрагента «ИП Скачкова»" })).toHaveCount(1);
  await expect(warnings.filter({ hasText: "у контрагента «ООО Лема»" })).toHaveCount(1);
  expect(new Set(asked.map((params) => params.get("counterparty_id")))).toEqual(
    new Set([CP_ID, OTHER_CP_ID]),
  );
  expect(duplicateKeys).toEqual([]);
});

async function mockNewPaymentIncome(page: Page) {
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
}

async function openNewPaymentRefund(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: "Активные платежи" }).click();
  const payments = page.getByRole("dialog").filter({ hasText: "Активные платежи" });
  await payments.getByRole("button", { name: "Создать", exact: true }).click();
  const dialog = page.getByRole("dialog").filter({ hasText: "Новый платёж" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Возврат переплаты от поставщиков" }).first().click();
  return dialog;
}

const BANK_TWIN = {
  transaction_id: "88888888-8888-8888-8888-888888888888",
  operation_date: "2026-09-22",
  amount: "300.00",
  wallet_name: "Т-Банк",
  channel: "bank",
  source_kind: "bank_operation",
};

test("«Новый платёж» — возврат наличными предупреждает о той же выписке", async ({ page }) => {
  await mockNewPaymentIncome(page);
  const asked = await mockTwins(page, BANK_TWIN);

  const dialog = await openNewPaymentRefund(page);
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

test("«Новый платёж» спрашивает сторожа суммой после паузы, а не на каждую цифру", async ({
  page,
}) => {
  await mockNewPaymentIncome(page);
  const asked = await mockTwins(page, { ...BANK_TWIN, amount: "35017.95" });

  const dialog = await openNewPaymentRefund(page);
  await dialog.getByRole("button", { name: /ИП Скачкова/ }).click();
  // Печатаем как человек: восемь нажатий быстрее паузы в 300 мс.
  await dialog.getByLabel("Сумма, ₽").pressSequentially("35017.95", { delay: 20 });

  await expect(dialog.getByText(/Похоже на задвоение/)).toBeVisible();
  expect(asked.map((params) => params.get("amount"))).toEqual(["35017.95"]);
});

test("разбор кейса собственником тоже предупреждает — так пришёл возврат Скачковой", async ({
  page,
}) => {
  await mockAuth(page);
  await page.route("**/api/v1/dds/articles**", (route) => fulfillJson(route, [REFUND_ARTICLE]));
  await page.route("**/api/v1/counterparties/registry**", (route) =>
    fulfillJson(route, [SKACHKOVA]),
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

  const classified: Record<string, unknown>[] = [];
  await page.route("**/api/v1/dds/owner-review/*/classify", (route) => {
    classified.push(route.request().postDataJSON());
    return fulfillJson(route, {
      case_id: "99999999-9999-9999-9999-999999999999",
      status: "resolved",
      bank_operation_id: OP_ID,
      classification_status: "classified",
      rule_id: null,
      rule_warning: null,
    });
  });

  // Галочку поставили раньше, чем выбрали статью: возвратная статья её всё равно снимает.
  const remember = page.getByRole("checkbox", { name: /Запомнить как правило/ });
  await remember.check();

  const selects = page.getByRole("combobox");
  await selects.nth(1).click();
  await page.getByRole("option", { name: /Возврат переплаты от поставщиков/ }).click();
  await selects.nth(2).click();
  await page.getByRole("option", { name: /ИП Скачкова/ }).click();

  const warning = page.getByRole("alert").filter({ hasText: "Похоже на задвоение" });
  await expect(warning).toBeVisible();
  await expect(warning).toContainText("наличными на «Сейф» («Новый платёж»)");
  expect(asked.at(-1)!.get("bank_operation_id")).toBe(OP_ID);

  await expect(remember).toBeDisabled();
  await expect(remember).not.toBeChecked();
  await expect(page.getByText(/Возврат переплаты правилом не запоминаем/)).toBeVisible();
  await page.getByRole("button", { name: "Классифицировать" }).click();
  await expect.poll(() => classified.length).toBe(1);
  expect(classified[0]).toMatchObject({
    action: "set_article",
    article_id: REFUND_ARTICLE_ID,
    remember_as_rule: false,
  });

  await page.screenshot({ path: "test-results/refund-twin-owner-review.png", fullPage: true });
});
