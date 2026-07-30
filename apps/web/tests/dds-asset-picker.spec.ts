import { expect, test, type Route } from "@playwright/test";

// Выбор основного средства в разборе ДДС. Тест сквозной СОЗНАТЕЛЬНО: 30.07.2026 контур
// сломали три дефекта, и ни один не ловился по отдельности.
//
// 1. `_article_payloads` собирает ответ полем за полем и пропустил `asset_link_kind` — схема
//    подставила `null`, ответ остался валидным, тесты бэкенда прошли (гейт читает статью из
//    базы, а не из HTTP), и поле выбора не показывалось НИ НА ОДНОЙ статье.
// 2. Гейт стоял только на разборе банк-операции; ручная проводка пускала «Покупку ОС» без
//    карточки.
// 3. Строка журнала не несла `asset_id`, и переоткрытие разбора отдавало пустой объект —
//    «Разнести» снимало привязку, по которой покупка стоит на балансе.
//
// Отсюда проверки: признак статьи ДОЕХАЛ, поле появилось, кнопка заблокирована до выбора,
// и уже выбранный объект переживает переоткрытие.

const ASSET_ARTICLE_ID = "11111111-1111-1111-1111-111111111111";
const PLAIN_ARTICLE_ID = "22222222-2222-2222-2222-222222222222";
const ASSET_ID = "33333333-3333-3333-3333-333333333333";
const WALLET_ID = "44444444-4444-4444-4444-444444444444";
const TXN_ID = "55555555-5555-5555-5555-555555555555";

function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function article(overrides: Record<string, unknown>) {
  return {
    id: PLAIN_ARTICLE_ID,
    code: "prochie_rashody",
    name: "Прочие расходы",
    movement_type: "outflow",
    activity_type: "operating",
    parent_id: null,
    is_active: true,
    kassa_enabled: false,
    location_required: false,
    lease_bound: false,
    asset_link_kind: null,
    description: null,
    aliases: [],
    ...overrides,
  };
}

function journalRow(overrides: Record<string, unknown> = {}) {
  return {
    kind: "cashflow",
    id: TXN_ID,
    bank_operation_id: null,
    status: "classified",
    operation_date: "2026-07-30",
    occurred_at: "2026-07-30T12:00:00+03:00",
    direction: "out",
    amount: "40000.00",
    article_id: ASSET_ARTICLE_ID,
    counterparty_id: null,
    wallet_id: WALLET_ID,
    provider: null,
    payment_purpose: "Оплата оборудования",
    counterparty_name_raw: null,
    counterparty_inn_raw: null,
    is_card: false,
    asset_id: null,
    ...overrides,
  };
}

async function mockCommon(page: import("@playwright/test").Page, row: Record<string, unknown>) {
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
  await page.route("**/api/v1/dds/articles**", (route) =>
    fulfillJson(route, [
      article({}),
      article({
        id: ASSET_ARTICLE_ID,
        code: "pokupka_os",
        name: "Покупка ОС",
        activity_type: "investing",
        asset_link_kind: "purchase",
      }),
    ]),
  );
  await page.route("**/api/v1/fixed-assets/options**", (route) =>
    fulfillJson(route, {
      items: [
        {
          asset_id: ASSET_ID,
          inventory_number: "ОС-0042",
          name: "Витрина холодильная настольная",
          brand_model: "POLAIR",
          location_name: "Черникова",
          status: "in_use",
          status_title: "В работе",
          initial_cost: "40000.00",
        },
      ],
    }),
  );
  await page.route("**/api/v1/dds/wallets**", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/counterparties/directory**", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/dds/journal**", (route) => fulfillJson(route, { items: [row] }));
}

test("статья по основным средствам требует объект и называет причину", async ({ page }) => {
  await mockCommon(page, journalRow());
  await page.goto("/dds");
  await page.getByRole("tab", { name: /Журнал ДДС/ }).click();

  await page.getByRole("row").filter({ hasText: "Покупка ОС" }).first().click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();

  // Признак статьи доехал: строка знает, что объект обязателен.
  await expect(dialog.getByText("нужен объект основных средств")).toBeVisible();
  // Серая кнопка обязана объяснять себя — иначе читается как поломка.
  await expect(dialog.getByText(/Без карточки покупка уйдёт в расход мимо баланса/)).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Разнести" })).toBeDisabled();

  // Поле выбора живёт в строке статьи и подписывает последствие для этого вида статьи.
  await dialog.getByText("нужен объект основных средств").click();
  const detail = page.getByRole("dialog").last();
  await expect(detail.getByText("Основное средство")).toBeVisible();
  await expect(detail.getByText(/станет первоначальной стоимостью карточки/)).toBeVisible();
  await expect(detail.getByText(/ОС-0042/)).toBeVisible();
});

test("уже выбранный объект переживает переоткрытие разбора", async ({ page }) => {
  await mockCommon(page, journalRow({ asset_id: ASSET_ID }));
  await page.goto("/dds");
  await page.getByRole("tab", { name: /Журнал ДДС/ }).click();

  await page.getByRole("row").filter({ hasText: "Покупка ОС" }).first().click();
  const dialog = page.getByRole("dialog");

  // Объект восстановлен из строки журнала, а не потерян: иначе «Разнести» сняло бы привязку.
  await expect(dialog.getByText(/Объект: ОС-0042/)).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Разнести" })).toBeEnabled();
});
