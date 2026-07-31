import { expect, test, type Page, type Route } from "@playwright/test";

// Объект основных средств в окне «Новый платёж».
//
// Два правила владельца от 31.07.2026, и оба про то, как окно ВЫГЛЯДИТ, а значит их не поймает
// ни один тест сервиса:
//   1. выбор объекта живёт в отдельной модалке, как в разборе ДДС, а не развёрнутым блоком
//      посреди платежа;
//   2. заводить новую карточку можно только у ПОКУПКИ. Ремонт — это работы по объекту, который
//      уже на балансе; кнопка «Завести новый» там породила бы второй такой же объект стоимостью
//      в сумму ремонта.

const PURCHASE_ARTICLE_ID = "11111111-1111-1111-1111-111111111111";
const REPAIR_ARTICLE_ID = "22222222-2222-2222-2222-222222222222";
const ASSET_ID = "66666666-6666-6666-6666-666666666666";

function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function article(id: string, name: string, kind: "purchase" | "repair") {
  return {
    id,
    code: kind === "purchase" ? "pokupka_os" : "remont_os",
    name,
    flow: "expense",
    activity: kind === "purchase" ? "investing" : "operating",
    counterparties: [],
    location_required: false,
    lease_bound: false,
    asset_link_kind: kind,
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
  await page.route("**/api/v1/finance/payments**", (route) =>
    fulfillJson(route, { scope: "active", buckets: [], items: [] }),
  );
  await page.route("**/api/v1/dds/new-payment/context**", (route) =>
    fulfillJson(route, {
      articles: [
        article(PURCHASE_ARTICLE_ID, "Покупка ОС", "purchase"),
        article(REPAIR_ARTICLE_ID, "Ремонт ОС", "repair"),
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
  await page.route("**/api/v1/fixed-assets/options**", (route) =>
    fulfillJson(route, {
      items: [
        {
          asset_id: ASSET_ID,
          inventory_number: "ОС-0001",
          name: "Печь для пиццы",
          brand_model: "ItPizza ML44",
          location_name: "Черникова",
          status: "in_use",
          status_title: "В работе",
          initial_cost: "95000.00",
        },
      ],
    }),
  );
  await page.route("**/api/v1/fixed-assets/categories**", (route) =>
    fulfillJson(route, { items: [] }),
  );
});

/** Открыть «Новый платёж» и выбрать статью с суммой.
 *
 * Палитра разложена по видам деятельности и открывается на операционной: покупка ОС —
 * инвестиционная, и до неё надо переключить вкладку. */
async function openRow(page: Page, articleName: string, ledger?: string) {
  await page.goto("/");
  await page.getByRole("button", { name: "Активные платежи" }).click();

  const payments = page.getByRole("dialog").filter({ hasText: "Активные платежи" });
  await payments.getByRole("button", { name: "Создать", exact: true }).click();

  const dialog = page.getByRole("dialog").filter({ hasText: "Новый платёж" });
  await expect(dialog).toBeVisible();
  if (ledger) await dialog.getByRole("button", { name: ledger, exact: true }).click();
  await dialog.getByRole("button", { name: articleName }).first().click();
  await dialog.getByLabel("Сумма").fill("12000");
  return dialog;
}

test("объект выбирается в отдельной модалке, а не блоком посреди платежа", async ({ page }) => {
  const dialog = await openRow(page, "Покупка ОС", "Инвест.");

  // В самой форме — только строка-напоминание. Список объектов до клика не развёрнут: окно
  // платежа и без того длинное, а объект нужен считанным статьям.
  await expect(dialog.getByText("нужен объект основных средств")).toBeVisible();
  await expect(dialog.getByPlaceholder("Поиск по номеру, названию или модели…")).toBeHidden();

  await dialog.getByText("нужен объект основных средств").click();

  // Модалка та же, что в разборе ДДС: заголовок, статья с суммой и выбор внутри.
  const picker = page.getByRole("dialog").filter({ hasText: "Основное средство" });
  await expect(picker.getByText("Покупка ОС ·")).toBeVisible();
  await picker.getByRole("button", { name: /ОС-0001 · Печь для пиццы/ }).click();
  await picker.getByRole("button", { name: "Готово" }).click();

  // Выбранное вернулось в строку — по ней видно, что платёж уже полон.
  await expect(dialog.getByText("Объект: ОС-0001 · Печь для пиццы")).toBeVisible();
});

test("у ремонта нельзя завести новый объект — ремонтируют существующий", async ({ page }) => {
  const dialog = await openRow(page, "Ремонт ОС");
  await dialog.getByText(/нужен объект/).click();

  const picker = page.getByRole("dialog").filter({ hasText: "Основное средство" });
  await expect(picker.getByRole("button", { name: /ОС-0001 · Печь для пиццы/ })).toBeVisible();
  // Кнопка завела бы вторую печь стоимостью в сумму ремонта, и в балансе появился бы объект,
  // которого никто не покупал.
  await expect(picker.getByRole("button", { name: /Завести новый объект/ })).toBeHidden();
});

test("у покупки кнопка заведения на месте", async ({ page }) => {
  const dialog = await openRow(page, "Покупка ОС", "Инвест.");
  await dialog.getByText("нужен объект основных средств").click();

  const picker = page.getByRole("dialog").filter({ hasText: "Основное средство" });
  // Обратная сторона предыдущей проверки: у покупки карточки ещё нет по определению, и без
  // этой кнопки платёж стал бы неразносимым.
  await expect(picker.getByRole("button", { name: /Завести новый объект/ })).toBeVisible();
});
