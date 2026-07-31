import { expect, test, type Route } from "@playwright/test";

// Заведение карточки ОС из разбора платежа. Ответы модели подменены: проверяется не её
// сообразительность, а поведение окна вокруг неё — то, что ломается молча.
//
// Почему это важно проверять отдельно: категория задаёт срок амортизации и решает, какие поля
// вообще показывать. Если окно потеряет категорию, спрячет обязательное поле или пропустит б/у
// без описания, объект будет амортизироваться не те годы, и увидят это через годы.

const ASSET_ARTICLE_ID = "11111111-1111-1111-1111-111111111111";
const HEAT_ID = "99999999-9999-9999-9999-999999999999";
const FURNITURE_ID = "88888888-8888-8888-8888-888888888888";
const WALLET_ID = "44444444-4444-4444-4444-444444444444";
const TXN_ID = "55555555-5555-5555-5555-555555555555";

function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

/** Ответ модели: поля те же, что у ``IntakeRead``, — молчаливый пропуск ловится типами. */
function intakeReply(overrides: Record<string, unknown>) {
  return {
    status: "ready",
    question: null,
    why: null,
    suggestions: [],
    name: null,
    brand: null,
    model: null,
    category_id: null,
    category_name: null,
    material: null,
    dimensions: null,
    specs: null,
    condition: null,
    condition_note: null,
    reason: null,
    ...overrides,
  };
}

async function mockBase(page: import("@playwright/test").Page) {
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
      {
        id: ASSET_ARTICLE_ID,
        code: "pokupka_os",
        name: "Покупка ОС",
        movement_type: "outflow",
        activity_type: "investing",
        parent_id: null,
        is_active: true,
        kassa_enabled: false,
        location_required: false,
        lease_bound: false,
        asset_link_kind: "purchase",
        description: null,
        aliases: [],
      },
    ]),
  );
  await page.route("**/api/v1/fixed-assets/options**", (route) =>
    fulfillJson(route, { items: [] }),
  );
  await page.route("**/api/v1/fixed-assets/categories**", (route) =>
    fulfillJson(route, {
      items: [
        {
          id: HEAT_ID,
          name: "Тепловое оборудование",
          useful_life_months: 84,
          spec_profile: "equipment",
          note: null,
        },
        {
          id: FURNITURE_ID,
          name: "Вспомогательное оборудование",
          useful_life_months: 120,
          spec_profile: "furniture",
          note: null,
        },
      ],
    }),
  );
  await page.route("**/api/v1/dds/wallets**", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/counterparties/directory**", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/dds/journal**", (route) =>
    fulfillJson(route, {
      items: [
        {
          kind: "cashflow",
          id: TXN_ID,
          bank_operation_id: null,
          status: "classified",
          operation_date: "2026-07-31",
          occurred_at: "2026-07-31T12:00:00+03:00",
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
        },
      ],
    }),
  );
}

async function openCreate(page: import("@playwright/test").Page) {
  await page.goto("/dds");
  await page.getByRole("tab", { name: /Журнал ДДС/ }).click();
  await page.getByRole("row").filter({ hasText: "Покупка ОС" }).first().click();
  await page.getByText("нужен объект основных средств").click();
  await page.getByRole("button", { name: /Завести новый объект/ }).click();
}

test("по умолчанию открывается форма, и категория решает, какие поля спрашивать", async ({
  page,
}) => {
  await mockBase(page);
  await openCreate(page);
  const dialog = page.getByRole("dialog").last();

  // Первый вопрос — категория. Диалог с моделью здесь ОПЦИЯ, а не единственный вход: без ключа
  // или при лежащем релее покупку всё равно надо записать.
  await expect(dialog.getByText("Что за оборудование")).toBeVisible();
  await expect(dialog.getByRole("button", { name: /Внести с помощью ИИ/ })).toBeVisible();
  await expect(dialog.getByText("Наименование")).toBeHidden();

  // Техника — марка и модель: без них объект не опознать при следующей инвентаризации.
  await dialog.getByRole("button", { name: /Тепловое оборудование · 7 лет/ }).click();
  await expect(dialog.getByText("Марка")).toBeVisible();
  await expect(dialog.getByText("Модель")).toBeVisible();
  await expect(dialog.getByText("Материал")).toBeHidden();

  // Мебель — материал и размеры, марки у производственного стола обычно нет вовсе.
  await dialog.getByRole("button", { name: /Вспомогательное оборудование · 10 лет/ }).click();
  await expect(dialog.getByText("Материал")).toBeVisible();
  await expect(dialog.getByText("Размеры")).toBeVisible();
  await expect(dialog.getByText("Марка")).toBeHidden();
});

test("б/у без описания состояния завести нельзя", async ({ page }) => {
  await mockBase(page);
  const created: unknown[] = [];
  await page.route("**/api/v1/fixed-assets/from-payment", async (route) => {
    created.push(route.request().postDataJSON());
    return route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({
        asset_id: "77777777-7777-7777-7777-777777777777",
        inventory_number: "ОС-0150",
        name: "Шкаф холодильный",
        brand_model: "Polair CM105-S",
        location_name: null,
        status: "in_use",
        status_title: "В работе",
        initial_cost: "40000.00",
      }),
    });
  });

  await openCreate(page);
  const dialog = page.getByRole("dialog").last();
  await dialog.getByRole("button", { name: /Тепловое оборудование · 7 лет/ }).click();
  await dialog.getByPlaceholder(/Рисоварка промышленная/).fill("Шкаф холодильный");
  await dialog.getByPlaceholder("Gastrorag").fill("Polair");
  await dialog.getByPlaceholder("DH-RC-2").fill("CM105-S");

  // Состояние обязательно: карточка без него амортизируется как новая, сколько бы лет объект
  // ни отработал у прошлого хозяина.
  const submit = dialog.getByRole("button", { name: "Завести и выбрать" });
  await expect(submit).toBeDisabled();

  await dialog.getByRole("button", { name: "Б/У", exact: true }).click();
  await expect(submit).toBeDisabled();

  await dialog.getByPlaceholder(/дверь провисла/).fill("2019 года, компрессор менялся");
  await expect(submit).toBeEnabled();
  // Ждём ОТВЕТ, а не запрос: обработчик маршрута отрабатывает после события запроса, и на
  // `waitForRequest` массив ещё пуст.
  const sent = page.waitForResponse("**/api/v1/fixed-assets/from-payment");
  await submit.click();
  await sent;

  expect(created).toHaveLength(1);
  expect(created[0]).toMatchObject({
    name: "Шкаф холодильный",
    brand_model: "Polair CM105-S",
    condition: "used",
    condition_note: "2019 года, компрессор менялся",
  });
});

test("ИИ — кнопка: предложение модели приземляется в ту же форму", async ({ page }) => {
  await mockBase(page);
  const asked: unknown[] = [];
  await page.route("**/api/v1/fixed-assets/intake", async (route) => {
    const body = route.request().postDataJSON();
    asked.push(body);
    if (body.history.length === 0) {
      return fulfillJson(
        route,
        intakeReply({
          status: "need_more",
          question: "Что за стол?",
          why: "От материала зависит категория и срок службы.",
          suggestions: ["из нержавеющей стали", "деревянный"],
        }),
      );
    }
    return fulfillJson(
      route,
      intakeReply({
        name: "Стол производственный из нержавеющей стали",
        category_id: FURNITURE_ID,
        category_name: "Вспомогательное оборудование",
        material: "нержавеющая сталь",
        dimensions: "1200×600×850 мм",
        reason: "Кухонное оборудование, а не офисная мебель.",
      }),
    );
  });

  await openCreate(page);
  const dialog = page.getByRole("dialog").last();
  await dialog.getByRole("button", { name: /Внести с помощью ИИ/ }).click();

  // Одно поле вместо формы — сотрудник пишет своими словами.
  await dialog.getByPlaceholder(/купили рисоварку/).fill("купили стол");
  await dialog.getByRole("button", { name: "Дальше" }).click();

  // Вопрос виден, и рядом — зачем он задан.
  await expect(dialog.getByText("Что за стол?")).toBeVisible();
  await expect(dialog.getByText(/От материала зависит категория/)).toBeVisible();

  // Быстрый ответ одним тапом — окно заводят с планшета.
  await dialog.getByRole("button", { name: "из нержавеющей стали" }).click();

  // Готовая карточка — это ТА ЖЕ форма: человек видит поля своей категории и правит любое.
  // Материал и размеры приезжают порознь, а не строкой: иначе они не попали бы в свои поля.
  await expect(dialog.getByText(/Кухонное оборудование, а не офисная мебель/)).toBeVisible();
  await expect(
    dialog.locator(`input[value="Стол производственный из нержавеющей стали"]`),
  ).toBeVisible();
  await expect(dialog.locator(`input[value="нержавеющая сталь"]`)).toBeVisible();
  await expect(dialog.locator(`input[value="1200×600×850 мм"]`)).toBeVisible();

  // Состояние модель не угадывала — переключатель остался пустым, и «Завести» заблокировано.
  await expect(dialog.getByRole("button", { name: "Завести и выбрать" })).toBeDisabled();

  // Вся переписка ушла во второй запрос: без неё модель спросила бы то же самое заново.
  expect(asked).toHaveLength(2);
  expect(asked[1]).toMatchObject({
    purchase: "купили стол",
    history: [{ question: "Что за стол?", answer: "из нержавеющей стали" }],
  });
});

test("модель недоступна — окно возвращается к форме, а не встаёт", async ({ page }) => {
  await mockBase(page);
  await page.route("**/api/v1/fixed-assets/intake", (route) =>
    route.fulfill({
      status: 422,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Ключ ANTHROPIC_API_KEY не принят (401)" }),
    }),
  );

  await openCreate(page);
  const dialog = page.getByRole("dialog").last();
  await dialog.getByRole("button", { name: /Внести с помощью ИИ/ }).click();
  await dialog.getByPlaceholder(/купили рисоварку/).fill("рисоварка");
  await dialog.getByRole("button", { name: "Дальше" }).click();

  // Платёж должен провестись в любом случае: недоступность модели не повод не записать
  // покупку. Поэтому вместо тупика — обычная форма, уже с тем, что человек успел написать.
  await expect(dialog.getByText("Что за оборудование")).toBeVisible();
  await dialog.getByRole("button", { name: /Тепловое оборудование · 7 лет/ }).click();
  await expect(dialog.locator(`input[value="рисоварка"]`)).toBeVisible();
});
