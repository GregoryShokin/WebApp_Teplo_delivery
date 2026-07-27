import { expect, test, type Page, type Route } from "@playwright/test";

// Регрессия на дефект 27.07: в окне «Бартерная накладная» кол-во «0,5» (запятая — штатный
// разделитель русской раскладки) парсилось как NaN→0. Следствия, которые видел владелец:
// сумма не пересчитывалась из цены, единица измерения была не видна, а кнопка «Оформить
// займ» гасла молча — строка выпадала из фильтра `num(quantity) > 0`.

const PRODUCT_ID = "11111111-1111-1111-1111-111111111111";
const PARTNER_ID = "22222222-2222-2222-2222-222222222222";

function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function registryItem() {
  return {
    counterparty_id: PARTNER_ID,
    name: "Ёбидоёби",
    inn: "6100000000",
    status: "active",
    relationship: "barter",
    ledger_category_id: null,
    brand_group: null,
    internal_name: null,
    payment_delay_days: null,
    requisites_verified: false,
    kassa_enabled: false,
    has_iiko_guid: true,
    origin: "manual",
    unpaid_count: 0,
    unpaid_remaining: 0,
    receivable_remaining: 0,
    prepayment_balance: 0,
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
  await page.route("**/api/v1/counterparties/invoices**", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/counterparties/registry**", (route) =>
    fulfillJson(route, [registryItem()]),
  );
  await page.route("**/api/v1/warehouse/barter/partners**", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/warehouse/staff-articles**", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/warehouse/invoices/next-number**", (route) =>
    fulfillJson(route, { number: "515246" }),
  );
  await page.route("**/api/v1/warehouse/products**", (route) =>
    fulfillJson(route, [
      {
        id: PRODUCT_ID,
        iiko_id: "guid-eel",
        name: "Угорь",
        code: "00123",
        unit: "кг",
        type: "GOODS",
      },
    ]),
  );
  // Регистрируется ПОСЛЕ общего products: Playwright проверяет роуты в обратном порядке.
  await page.route("**/api/v1/warehouse/products/*/price-stats**", (route) =>
    fulfillJson(route, {
      avg_price: null,
      sample_count: 0,
      unit: "кг",
      upper_pct: 20,
      lower_pct: 20,
      lookback_days: 90,
      min_samples: 3,
    }),
  );
});

/** Открыть окно «Бартерная накладная» и заполнить шапку (контрагент + дата). */
async function openBarterDialog(page: Page) {
  await page.goto("/warehouse/barter");
  await page.getByRole("button", { name: "+ Оформить займ" }).click();

  const dialog = page.getByRole("dialog").filter({ hasText: "Бартерная накладная" });
  await expect(dialog).toBeVisible();

  await dialog.getByPlaceholder("Начните вводить имя").click();
  await dialog.getByRole("button", { name: "Ёбидоёби" }).click();
  await dialog.locator('input[type="datetime-local"]').fill("2026-07-27T16:01");

  await dialog.getByPlaceholder("Товар (GOODS)").click();
  await dialog.getByRole("button", { name: /Угорь/ }).click();

  return dialog;
}

test("количество с запятой считает сумму и не гасит кнопку", async ({ page }) => {
  const dialog = await openBarterDialog(page);

  // Единица из номенклатуры iiko видна в строке — ровно то, чего не хватало владельцу.
  await expect(dialog.getByText("кг", { exact: true })).toBeVisible();

  await dialog.getByLabel("Количество").fill("0,5");
  await dialog.getByLabel("Цена за 1 кг").fill("1582");

  await expect(dialog.getByLabel("Сумма строки")).toHaveValue("791");
  await expect(dialog.getByText("Итого: 791,00 ₽")).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Оформить займ" })).toBeEnabled();
});

test("сумма с запятой пересчитывает цену за единицу", async ({ page }) => {
  const dialog = await openBarterDialog(page);

  await dialog.getByLabel("Количество").fill("0,5");
  await dialog.getByLabel("Сумма строки").fill("791");

  await expect(dialog.getByLabel("Цена за 1 кг")).toHaveValue("1582");
});

test("займ уходит на бэк с дробным количеством и ценой за единицу", async ({ page }) => {
  let payload: Record<string, unknown> | null = null;
  await page.route("**/api/v1/warehouse/invoices", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    payload = route.request().postDataJSON();
    return fulfillJson(route, { id: "invoice-1" });
  });

  const dialog = await openBarterDialog(page);
  await dialog.getByLabel("Количество").fill("0,5");
  await dialog.getByLabel("Цена за 1 кг").fill("1582");
  await dialog.getByRole("button", { name: "Оформить займ" }).click();

  // Подтверждение займа: раскладка «кол-во × цена = сумма» — партнёр вернёт товар по цене выдачи.
  const confirm = page.getByRole("dialog").filter({ hasText: "Подтвердите заём" });
  await expect(confirm).toContainText("0,5 кг × 1 582,00 ₽ = 791,00 ₽");
  await confirm.getByRole("button", { name: "ОК, подтверждаю" }).click();

  await expect.poll(() => payload).not.toBeNull();
  expect(payload).toMatchObject({
    mode: "loan",
    counterparty_id: PARTNER_ID,
    lines: [{ name: "Угорь", quantity: 0.5, price: 1582, sum: 791 }],
  });
});

test("бартер без цены не даёт оформить займ и объясняет причину", async ({ page }) => {
  const dialog = await openBarterDialog(page);
  await dialog.getByLabel("Количество").fill("0,5");

  await expect(dialog.getByText(/укажите цену за единицу или сумму строки/)).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Оформить займ" })).toBeDisabled();
});
