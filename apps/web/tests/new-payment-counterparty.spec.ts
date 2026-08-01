import { expect, test, type Page, type Route } from "@playwright/test";

// Вход в платёж «от получателя» (правка владельца 01.08.2026).
//
// Платёж заводят словами «надо заплатить Наумченко», а не «надо провести расход по статье ФД».
// До этой правки получателя можно было выбрать ТОЛЬКО у статьи, за которой контрагент закреплён:
// у остальных поле «Кому платим» не показывалось вовсе, и платёж уходил без атрибуции — а значит
// мимо сверки расчётов и мимо контроля закрывающих документов.
//
// Проверяем четыре вещи, которых не видит ни один тест сервиса:
//   1. переключатель «По статье / Контрагенту» — два способа завести один и тот же платёж;
//   2. статья подставляется из карточки, а если её там нет — окно прямо просит выбрать и не
//      даёт отправить;
//   3. свойства получателя (реквизиты) считаются по СПРАВОЧНИКУ, а не по списку статьи —
//      иначе свободно выбранный официальный контрагент уехал бы мимо реквизитного маршрута;
//   4. период услуги задаётся отдельным окном (правило владельца: развёрнутым блокам внутри
//      строки в окне платежа не место) и возвращается в строку подписью.

const SEO_ARTICLE_ID = "11111111-1111-1111-1111-111111111111";
const FD_ARTICLE_ID = "22222222-2222-2222-2222-222222222222";
const MIKROEL_ID = "33333333-3333-3333-3333-333333333333";
const NAUM_ID = "44444444-4444-4444-4444-444444444444";

function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function article(id: string, name: string, pinned: unknown[] = []) {
  return {
    id,
    code: id === FD_ARTICLE_ID ? "uslugi_fd_i_nk" : "seo",
    name,
    flow: "expense",
    activity: "operating",
    counterparties: pinned,
    location_required: false,
    lease_bound: false,
    asset_link_kind: null,
  };
}

const MIKROEL = {
  counterparty_id: MIKROEL_ID,
  name: 'ООО "МИКРОЭЛ"',
  inn: "6100000001",
  relationship: "official",
  has_requisites: true,
  requisites_verified: true,
  service_period_required: true,
  // «Обычно платим за прошлый месяц» — подсказка в окне периода, но НЕ автоподстановка.
  default_service_period_offset_months: -1,
  default_dds_article_id: FD_ARTICLE_ID,
  confirm_no_dds_article: false,
};

const NAUMCHENKO = {
  counterparty_id: NAUM_ID,
  name: "ИП Наумченко Наталья Васильевна",
  inn: "6100000002",
  relationship: "official",
  has_requisites: false,
  requisites_verified: false,
  service_period_required: false,
  default_service_period_offset_months: null,
  default_dds_article_id: null,
  confirm_no_dds_article: false,
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
  await page.route("**/api/v1/finance/payments**", (route) =>
    fulfillJson(route, { scope: "active", buckets: [], items: [] }),
  );
  await page.route("**/api/v1/dds/new-payment/context**", (route) =>
    fulfillJson(route, {
      articles: [
        article(SEO_ARTICLE_ID, "SEO-оптимизация"),
        article(FD_ARTICLE_ID, "Услуги ФД и НК", [MIKROEL]),
      ],
      counterparties: [MIKROEL, NAUMCHENKO],
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

async function openDialog(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: "Активные платежи" }).click();
  const payments = page.getByRole("dialog").filter({ hasText: "Активные платежи" });
  await payments.getByRole("button", { name: "Создать", exact: true }).click();
  const dialog = page.getByRole("dialog").filter({ hasText: "Новый платёж" });
  await expect(dialog).toBeVisible();
  return dialog;
}

test("режим «Контрагенту» платит получателю, статья приходит из карточки", async ({ page }) => {
  const dialog = await openDialog(page);

  // По умолчанию окно спрашивает статью — контрагентов в палитре нет.
  await expect(dialog.getByText("Кому платим", { exact: true })).toBeHidden();
  await expect(dialog.getByRole("button", { name: "SEO-оптимизация" })).toBeVisible();

  await dialog.getByRole("button", { name: "Контрагенту", exact: true }).click();

  // Переключились на получателей: статьи ушли, леджер-чипы (они про статьи) тоже.
  await expect(dialog.getByRole("button", { name: "SEO-оптимизация" })).toBeHidden();
  await expect(dialog.getByRole("button", { name: "Опер.", exact: true })).toBeHidden();
  await expect(dialog.getByRole("button", { name: /МИКРОЭЛ/ })).toBeVisible();
  await expect(dialog.getByRole("button", { name: /Наумченко/ })).toBeVisible();

  await dialog
    .getByRole("button", { name: /МИКРОЭЛ/ })
    .first()
    .click();

  // Строка расхода собралась целиком: статья из карточки + получатель.
  await expect(dialog.getByRole("button", { name: "Услуги ФД и НК" })).toBeVisible();
  await expect(dialog.getByLabel("Кому платим")).toContainText("МИКРОЭЛ");
});

test("получатель без статьи в карточке блокирует отправку до выбора статьи", async ({ page }) => {
  const dialog = await openDialog(page);
  await dialog.getByRole("button", { name: "Контрагенту", exact: true }).click();
  await dialog.getByPlaceholder("Название или ИНН…").fill("Наумченко");
  await dialog
    .getByRole("button", { name: /Наумченко/ })
    .first()
    .click();

  await expect(dialog.getByLabel("Кому платим")).toContainText("Наумченко");
  await expect(dialog.getByText(/В карточке контрагента нет статьи ДДС/)).toBeVisible();

  await dialog.getByLabel("Сумма").fill("9000");
  // Сумма есть, получатель есть — и всё равно нельзя: расход без статьи выпадает из аналитики.
  await expect(dialog.getByText(/Выберите статью ДДС для платежа/)).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Отправить в банк" })).toBeDisabled();

  await dialog.getByRole("button", { name: "Статья ДДС" }).click();
  await dialog.getByRole("button", { name: "SEO-оптимизация" }).click();
  await expect(dialog.getByRole("button", { name: "Отправить в банк" })).toBeEnabled();
});

test("свободно выбранный получатель ведёт платёж по своим реквизитам", async ({ page }) => {
  const dialog = await openDialog(page);
  // Начинаем со статьи, за которой не закреплён никто: поле «Кому платим» раньше тут вообще
  // не показывалось.
  await dialog.getByRole("button", { name: "SEO-оптимизация" }).first().click();
  await dialog.getByLabel("Сумма").fill("5000");

  await dialog.getByLabel("Кому платим").click();
  await dialog.getByRole("button", { name: /Наумченко/ }).click();

  // Официальный получатель без реквизитов: маршрут «карта ИП → Сейф» требует явного согласия.
  // Пока свойства получателя брались из списка статьи, форма считала бы, что получателя нет,
  // и отправила бы платёж без этой проверки.
  await expect(dialog.getByText(/не указаны реквизиты/)).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Вывести на карту ИП" })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Отправить в банк" })).toBeDisabled();

  // Период доступен и здесь — он свойство платежа, а не карточки.
  await expect(dialog.getByRole("button", { name: /Период услуги/ })).toBeVisible();
});

test("период не подставляется сам — платёж без него не отправить", async ({ page }) => {
  const dialog = await openDialog(page);
  await dialog.getByRole("button", { name: "Контрагенту", exact: true }).click();
  await dialog
    .getByRole("button", { name: /МИКРОЭЛ/ })
    .first()
    .click();
  await dialog.getByLabel("Сумма").fill("9000");

  // У контрагента в карточке есть «обычный» месяц (offset −1) — и он всё равно НЕ проставлен:
  // предзаполненный период не перечитывают, а ошибка месяцем разложит расход не туда.
  await expect(dialog.getByRole("button", { name: /нужен период оказания услуги/ })).toBeVisible();
  await expect(dialog.getByText(/Укажите период оказания услуги/)).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Отправить в банк" })).toBeDisabled();

  // Настройка карточки живёт кнопкой-подсказкой: один клик, но клик человека.
  await dialog.getByRole("button", { name: /нужен период оказания услуги/ }).click();
  const period = page.getByRole("dialog").filter({ hasText: "Период оказания услуги" });
  await expect(period.getByLabel("Месяц начала периода")).toHaveValue("");
  await period.getByRole("button", { name: /Обычно у этого контрагента/ }).click();
  await expect(period.getByLabel("Месяц начала периода")).not.toHaveValue("");
  await period.getByRole("button", { name: "Готово" }).click();

  await expect(dialog.getByRole("button", { name: "Отправить в банк" })).toBeEnabled();
});

test("период задаётся отдельным окном и возвращается подписью в строку", async ({ page }) => {
  const dialog = await openDialog(page);
  await dialog.getByRole("button", { name: "Контрагенту", exact: true }).click();
  await dialog
    .getByRole("button", { name: /МИКРОЭЛ/ })
    .first()
    .click();
  await dialog.getByLabel("Сумма").fill("9000");

  // Внутри строки период не разворачивается — только ссылка, как у выбора объекта ОС.
  await expect(dialog.getByLabel("Сколько месяцев")).toBeHidden();
  await dialog.getByRole("button", { name: /нужен период оказания услуги/ }).click();

  const period = page.getByRole("dialog").filter({ hasText: "Период оказания услуги" });
  await expect(period).toBeVisible();
  await expect(period.getByText(/период обязателен/)).toBeVisible();

  await period.getByLabel("Месяц начала периода").fill("2026-04");
  await period.getByLabel("Сколько месяцев").click();
  await page.getByRole("option", { name: "3 мес." }).click();
  // Разбивка считается сразу — по ней видно, что признание разложит расход помесячно.
  await expect(period.getByText(/по 3\s?000,00\s?₽ в месяц/)).toBeVisible();
  await period.getByRole("checkbox").check();
  await period.getByRole("button", { name: "Готово" }).click();

  await expect(period).toBeHidden();
  await expect(
    dialog.getByRole("button", { name: /Период: .* · по 3\s?000,00\s?₽ в месяц · признаём сами/ }),
  ).toBeVisible();
});
