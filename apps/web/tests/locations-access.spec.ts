import { expect, test, type Page, type Route } from "@playwright/test";

// Регрессия на кейс 28.07.2026: у должности «Менеджер» (роль office_manager) не было права
// source.locations.read, весь роутер /locations отвечал 403, useQuery оставлял data undefined —
// и оба пикера помещения объявляли это «помещений нет». Реестр был цел, не хватало доступа;
// владелец полдня искал поломку в реестре помещений.

const RENT_ARTICLE_ID = "33333333-3333-3333-3333-333333333333";
const OPERATION_ID = "44444444-4444-4444-4444-444444444444";
const FORBIDDEN_HINT = /Нет доступа к реестру помещений/;

function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

/** Реестр помещений закрыт правом source.locations.read — ровно так отвечает бэк. */
function routeLocationsForbidden(page: Page) {
  return page.route("**/api/v1/locations/options/for-article/**", (route) =>
    route.fulfill({
      status: 403,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Insufficient permission" }),
    }),
  );
}

/** Право есть, помещений в реестре действительно нет. */
function routeLocationsEmpty(page: Page) {
  return page.route("**/api/v1/locations/options/for-article/**", (route) =>
    fulfillJson(route, { items: [] }),
  );
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
});

test.describe("Новый платёж", () => {
  test.beforeEach(async ({ page }) => {
    await page.route("**/api/v1/finance/payments**", (route) =>
      fulfillJson(route, { scope: "active", buckets: [], items: [] }),
    );
    await page.route("**/api/v1/dds/new-payment/context**", (route) =>
      fulfillJson(route, {
        articles: [
          {
            id: RENT_ARTICLE_ID,
            code: "rent",
            name: "Аренда помещения",
            flow: "expense",
            activity: "operating",
            counterparties: [],
            location_required: true,
            lease_bound: true,
          },
        ],
        wallets: [
          {
            id: "wallet-tbank",
            code: "tbank",
            name: "Т-Банк",
            bank_code: "tbank",
            kind: "bank",
            location: null,
          },
        ],
        employees: [],
      }),
    );
  });

  /** Открыть «Новый платёж» и выбрать арендную статью — строка сразу требует помещение. */
  async function openRentRow(page: Page) {
    await page.goto("/");
    await page.getByRole("button", { name: "Активные платежи" }).click();

    const payments = page.getByRole("dialog").filter({ hasText: "Активные платежи" });
    await payments.getByRole("button", { name: "Создать", exact: true }).click();

    const dialog = page.getByRole("dialog").filter({ hasText: "Новый платёж" });
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "Аренда помещения" }).first().click();
    await dialog.getByLabel("Сумма").fill("50000");

    return dialog;
  }

  test("403 на реестре помещений объясняет доступ, а не пустой реестр", async ({ page }) => {
    await routeLocationsForbidden(page);

    const dialog = await openRentRow(page);

    await expect(dialog.getByText(FORBIDDEN_HINT).first()).toBeVisible();
    await expect(dialog.getByText(/Заведите его в Настройках/)).toHaveCount(0);

    // Отправка заблокирована — и панель «Что произойдёт» называет настоящую причину, а не
    // «Укажите помещение»: заполнить поле без права всё равно нечем.
    await expect(dialog.getByRole("button", { name: "Отправить в банк" })).toBeDisabled();
    await expect(dialog.getByText(FORBIDDEN_HINT)).toHaveCount(2);
  });

  test("пустой реестр помещений остаётся «заведите в Настройках»", async ({ page }) => {
    await routeLocationsEmpty(page);

    const dialog = await openRentRow(page);

    await expect(dialog.getByText(/Нет действующих помещений/)).toBeVisible();
    await expect(dialog.getByText(FORBIDDEN_HINT)).toHaveCount(0);
    await expect(dialog.getByRole("button", { name: "Отправить в банк" })).toBeDisabled();
    await expect(dialog.getByText("Укажите помещение для арендного платежа.")).toBeVisible();
  });
});

test.describe("Разбор операции", () => {
  test.beforeEach(async ({ page }) => {
    await page.route("**/api/v1/dds/wallets**", (route) => fulfillJson(route, []));
    await page.route("**/api/v1/counterparties/registry**", (route) => fulfillJson(route, []));
    await page.route("**/api/v1/dds/articles**", (route) =>
      fulfillJson(route, [
        {
          id: RENT_ARTICLE_ID,
          code: "rent",
          name: "Аренда помещения",
          movement_type: "outflow",
          activity_type: "operating",
          parent_id: null,
          is_active: true,
          kassa_enabled: false,
          location_required: true,
          lease_bound: true,
          description: null,
          aliases: [],
        },
      ]),
    );
    await page.route("**/api/v1/dds/journal**", (route) =>
      fulfillJson(route, {
        items: [
          {
            kind: "operation",
            id: OPERATION_ID,
            bank_operation_id: OPERATION_ID,
            status: "needs_review",
            operation_date: "2026-07-27",
            occurred_at: "2026-07-27T10:00:00",
            direction: "out",
            amount: "50000",
            article_id: null,
            counterparty_id: null,
            wallet_id: null,
            provider: "tbank",
            payment_purpose: "Аренда за июль",
            counterparty_name_raw: null,
            counterparty_inn_raw: null,
            is_card: false,
          },
        ],
        total: 1,
        marked_total: 0,
        unmarked_total: 1,
        transfer_total: 0,
      }),
    );
    await page.route("**/api/v1/dds/operations/*/split**", (route) =>
      fulfillJson(route, { lines: [] }),
    );
  });

  /** Открыть разбор операции журнала и поставить строке арендную статью. */
  async function openRentSplit(page: Page) {
    await page.goto("/dds/ledger");
    await page.getByText("Аренда за июль").first().click();

    const dialog = page.getByRole("dialog").filter({ hasText: "Разбор операции" });
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "Статья ДДС" }).click();
    await dialog.getByRole("button", { name: "Аренда помещения" }).click();

    return dialog;
  }

  test("403 объясняет доступ и в строке, и над кнопкой «Разнести»", async ({ page }) => {
    await routeLocationsForbidden(page);

    const dialog = await openRentSplit(page);

    // Кнопка гаснет молча — причину теперь видно прямо над ней.
    await expect(dialog.getByText(FORBIDDEN_HINT)).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Разнести" })).toBeDisabled();

    // Та же причина в под-модалке строки — на месте списка помещений.
    await dialog.getByRole("button", { name: /контрагент/ }).click();
    const detail = page.getByRole("dialog").filter({ hasText: "Контрагент" });
    await expect(detail.getByText(FORBIDDEN_HINT)).toBeVisible();
    await expect(detail.getByText("Помещения не найдены")).toHaveCount(0);
  });

  test("пустой реестр помещений не выдаёт себя за отказ в доступе", async ({ page }) => {
    await routeLocationsEmpty(page);

    const dialog = await openRentSplit(page);

    await expect(dialog.getByText(/Для этой статьи нужно помещение/)).toBeVisible();
    await expect(dialog.getByText(FORBIDDEN_HINT)).toHaveCount(0);
    await expect(dialog.getByRole("button", { name: "Разнести" })).toBeDisabled();

    await dialog.getByRole("button", { name: /контрагент/ }).click();
    const detail = page.getByRole("dialog").filter({ hasText: "Контрагент" });
    await expect(detail.getByText("Помещения не найдены")).toBeVisible();
    await expect(detail.getByText(FORBIDDEN_HINT)).toHaveCount(0);
  });
});
