import { expect, test, type Page, type Route } from "@playwright/test";
import type { NewPaymentArticle, NewPaymentContext } from "../src/lib/api";

// Собственник без платёжного профиля всё равно должен быть получателем займа.
// Проверяем выбор в реальном окне и counterparty_id, который уходит в каждый канал.
const LOAN_ID = "11111111-1111-1111-1111-111111111111";
const RETURN_ID = "22222222-2222-2222-2222-222222222222";
const EXPENSE_ID = "33333333-3333-3333-3333-333333333333";
const INCOME_ID = "44444444-4444-4444-4444-444444444444";
const PAVEL_ID = "55555555-5555-5555-5555-555555555555";
const SUPPLIER_ID = "66666666-6666-6666-6666-666666666666";
const SERVICES_ID = "77777777-7777-7777-7777-777777777777";
const LOAN_NAME = "Выдача кредитов и займов";
const RETURN_NAME = "Возврат кредитов и займов";

const PAVEL = {
  counterparty_id: PAVEL_ID,
  name: "Павел",
  inn: null,
  relationship: "informal" as const,
  has_requisites: false,
  requisites_verified: false,
  service_period_required: false,
  default_service_period_offset_months: null,
};
const SUPPLIER = {
  ...PAVEL,
  counterparty_id: SUPPLIER_ID,
  name: "Поставщик",
  default_dds_article_id: EXPENSE_ID,
  confirm_no_dds_article: false,
};

function article(
  id: string,
  code: string,
  name: string,
  flow: "expense" | "income",
  ownerRequired = false,
): NewPaymentArticle {
  return {
    id,
    code,
    name,
    flow,
    activity: ownerRequired ? "financing" : "operating",
    owner_required: ownerRequired,
    // Даже единственный закреплённый собственник не выбирается за пользователя.
    counterparties: ownerRequired ? [PAVEL] : [],
    location_required: false,
    lease_bound: false,
    asset_link_kind: null,
  };
}

function paymentContext(): NewPaymentContext {
  return {
    articles: [
      article(LOAN_ID, "vydacha_kreditov_i_zaimov", LOAN_NAME, "expense", true),
      article(RETURN_ID, "vozvrat_kreditov_i_zaimov", RETURN_NAME, "income", true),
      article(EXPENSE_ID, "other_expense", "Прочий расход", "expense"),
      article(INCOME_ID, "other_income", "Прочее поступление", "income"),
    ],
    counterparties: [SUPPLIER],
    owners: [PAVEL],
    wallets: [
      {
        id: "wallet-tbank",
        code: "tbank",
        name: "Т-Банк",
        bank_code: "tbank",
        kind: "bank",
        location: null,
      },
      {
        id: "wallet-safe",
        code: "safe",
        name: "Сейф",
        bank_code: null,
        kind: "cash",
        location: "safe",
      },
      {
        id: "wallet-kassa",
        code: "kassa",
        name: "Касса ТК",
        bank_code: null,
        kind: "cash",
        location: "kassa",
      },
    ],
    employees: [],
  };
}

function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}
async function mockContext(page: Page, context: NewPaymentContext) {
  await page.route("**/api/v1/dds/new-payment/context**", (route) => fulfillJson(route, context));
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
  await page.route("**/api/v1/finance/payments**", (route) =>
    fulfillJson(route, { scope: "active", buckets: [], items: [] }),
  );
  await page.route("**/api/v1/counterparties/registry**", (route) =>
    fulfillJson(route, [SUPPLIER]),
  );
  await mockContext(page, paymentContext());
});

async function openDialog(page: Page, name = LOAN_NAME, financing = true) {
  await page.goto("/");
  await page.getByRole("button", { name: "Активные платежи" }).click();
  await page
    .getByRole("dialog")
    .filter({ hasText: "Активные платежи" })
    .getByRole("button", { name: "Создать", exact: true })
    .click();
  const dialog = page.getByRole("dialog").filter({ hasText: "Новый платёж" });
  await expect(dialog).toBeVisible();
  if (financing) await dialog.getByRole("button", { name: "Фин.", exact: true }).click();
  await dialog.getByRole("button", { name, exact: true }).first().click();
  return dialog;
}

test("заём требует явного выбора собственника и отправляет его ID в банковский черновик", async ({
  page,
}) => {
  await page.route("**/api/v1/dds/new-payment/expense-draft", (route) =>
    fulfillJson(route, { id: "draft", amount: 12000, status: "draft" }),
  );
  const dialog = await openDialog(page);
  await dialog.getByLabel("Сумма", { exact: true }).fill("12000");
  await expect(dialog.getByLabel("Собственник", { exact: true })).toHaveText("Собственник");
  await expect(dialog.getByText(/Выберите собственника — движение/)).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Отправить в банк" })).toBeDisabled();
  await dialog.getByLabel("Собственник", { exact: true }).click();
  await expect(dialog.getByRole("button", { name: "Поставщик", exact: true })).toBeHidden();
  await dialog.getByRole("button", { name: "Павел", exact: true }).click();
  await expect(dialog.getByText(/Собственник: «Павел»/)).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Отправить в банк" })).toBeEnabled();
  await dialog.screenshot({ path: "/private/tmp/teplo-owner-loan-preview.png" });
  const request = page.waitForRequest(
    (req) => req.url().endsWith("/dds/new-payment/expense-draft") && req.method() === "POST",
  );
  await dialog.getByRole("button", { name: "Отправить в банк" }).click();
  expect((await request).postDataJSON()).toMatchObject({
    channel: "bank_draft",
    lines: [{ article_id: LOAN_ID, amount: 12000, counterparty_id: PAVEL_ID }],
  });
});

test("наличный резерв займа сохраняет выбранного собственника", async ({ page }) => {
  await page.route("**/api/v1/dds/new-payment/expense-cash", (route) =>
    fulfillJson(route, { created: 1, total: 5000, location: "safe" }),
  );
  const dialog = await openDialog(page);
  await dialog.getByRole("button", { name: "Сейф", exact: true }).click();
  await dialog.getByLabel("Сумма", { exact: true }).fill("5000");
  await expect(dialog.getByRole("button", { name: "Создать резерв", exact: true })).toBeDisabled();
  await dialog.getByLabel("Собственник", { exact: true }).click();
  await dialog.getByRole("button", { name: "Павел", exact: true }).click();
  await expect(dialog.getByText(/Резерв на Сейфе.*Собственник: «Павел»/)).toBeVisible();
  const request = page.waitForRequest(
    (req) => req.url().endsWith("/dds/new-payment/expense-cash") && req.method() === "POST",
  );
  await dialog.getByRole("button", { name: "Создать резерв", exact: true }).click();
  expect((await request).postDataJSON()).toMatchObject({
    wallet_id: "wallet-safe",
    pay_now: false,
    lines: [{ article_id: LOAN_ID, amount: 5000, counterparty_id: PAVEL_ID }],
  });
});

test("реквизиты собственника берутся из реестра собственников для банковского маршрута", async ({
  page,
}) => {
  const context = paymentContext();
  context.owners = [
    { ...PAVEL, relationship: "official", has_requisites: true, requisites_verified: true },
  ];
  await mockContext(page, context);
  const dialog = await openDialog(page);
  await dialog.getByLabel("Сумма", { exact: true }).fill("8000");
  await dialog.getByLabel("Собственник", { exact: true }).click();
  await dialog.getByRole("button", { name: "Павел", exact: true }).click();
  await expect(dialog.getByText(/Павел по реквизитам/)).toBeVisible();
  await expect(dialog.getByRole("button", { name: /Период услуги/ })).toBeHidden();
  await expect(dialog.getByRole("button", { name: "Отправить в банк" })).toBeEnabled();
  await expect(dialog.getByRole("button", { name: "Сейф", exact: true })).toBeDisabled();
});

test("возврат займа требует собственника и записывает его ID в наличное поступление", async ({
  page,
}) => {
  await page.route("**/api/v1/dds/new-payment/income-cash", (route) =>
    fulfillJson(route, { created: 1, total: 7000, location: "safe" }),
  );
  const dialog = await openDialog(page, RETURN_NAME);
  await dialog.getByLabel("Сумма, ₽", { exact: true }).fill("7000");
  await expect(dialog.getByRole("button", { name: "Провести поступление" })).toBeDisabled();
  await dialog.getByLabel("Собственник", { exact: true }).click();
  await expect(dialog.getByRole("button", { name: "Поставщик", exact: true })).toBeHidden();
  await dialog.getByRole("button", { name: "Павел", exact: true }).click();
  await expect(dialog.getByText(/Поступление от собственника «Павел»/)).toBeVisible();
  const request = page.waitForRequest(
    (req) => req.url().endsWith("/dds/new-payment/income-cash") && req.method() === "POST",
  );
  await dialog.getByRole("button", { name: "Провести поступление" }).click();
  expect((await request).postDataJSON()).toMatchObject({
    wallet_id: "wallet-safe",
    lines: [{ article_id: RETURN_ID, amount: 7000, counterparty_id: PAVEL_ID }],
  });
});

test("смена обычной статьи на заём очищает прежнего контрагента", async ({ page }) => {
  const dialog = await openDialog(page, "Прочий расход", false);
  await dialog.getByLabel("Сумма", { exact: true }).fill("3000");
  await dialog.getByLabel("Кому платим", { exact: true }).click();
  await dialog.getByRole("button", { name: "Поставщик", exact: true }).click();
  await expect(dialog.getByRole("button", { name: "Отправить в банк" })).toBeEnabled();
  await dialog.getByRole("button", { name: "Прочий расход", exact: true }).last().click();
  await dialog.getByRole("button", { name: LOAN_NAME, exact: true }).last().click();
  await expect(dialog.getByLabel("Собственник", { exact: true })).toHaveText("Собственник");
  await expect(dialog.getByRole("button", { name: "Отправить в банк" })).toBeDisabled();
});

test("смена обычного поступления на возврат займа очищает прежнего контрагента", async ({
  page,
}) => {
  const dialog = await openDialog(page, "Прочее поступление", false);
  await dialog.getByLabel("Сумма, ₽", { exact: true }).fill("4000");
  await dialog.getByRole("button", { name: "Поставщик", exact: true }).click();
  // В поступлении триггер выбора статьи получает accessible name от Label «Статья».
  await dialog.getByRole("button", { name: "Статья", exact: true }).click();
  await dialog.getByRole("button", { name: RETURN_NAME, exact: true }).click();
  await expect(dialog.getByLabel("Собственник", { exact: true })).toHaveText("Собственник");
  await expect(dialog.getByRole("button", { name: "Провести поступление" })).toBeDisabled();
});

test("оплата услуг собственнику сохраняет обязательность периода услуг", async ({ page }) => {
  const context = paymentContext();
  const serviceOwner = {
    ...PAVEL,
    service_period_required: true,
    default_dds_article_id: SERVICES_ID,
    confirm_no_dds_article: false,
  };
  context.counterparties.push(serviceOwner);
  context.articles.push({
    ...article(SERVICES_ID, "owner_services", "Услуги собственника", "expense"),
    counterparties: [serviceOwner],
  });
  await mockContext(page, context);
  const dialog = await openDialog(page, "Услуги собственника", false);
  await dialog.getByLabel("Сумма", { exact: true }).fill("6000");
  await expect(dialog.getByLabel("Кому платим", { exact: true })).toContainText("Павел");
  await expect(dialog.getByText(/Укажите период оказания услуги для Павел/)).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Отправить в банк" })).toBeDisabled();
});

test("пустой реестр собственников объясняет причину блокировки", async ({ page }) => {
  await mockContext(page, { ...paymentContext(), owners: [] });
  const dialog = await openDialog(page);
  await dialog.getByLabel("Сумма", { exact: true }).fill("1000");
  await expect(dialog.getByText(/Собственники не заведены — добавьте/)).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Отправить в банк" })).toBeDisabled();
});
