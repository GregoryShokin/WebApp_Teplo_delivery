import { expect, test, type Route } from "@playwright/test";

// Заведение карточки ОС диалогом. Ответы модели подменены: проверяется не её сообразительность,
// а поведение окна вокруг неё — то, что ломается молча.
//
// Почему это важно проверять отдельно: категория задаёт срок амортизации. Если окно потеряет
// предложенную категорию или не даст её поправить, объект будет амортизироваться не те годы, и
// увидят это через годы.

const ASSET_ARTICLE_ID = "11111111-1111-1111-1111-111111111111";
const CATEGORY_ID = "99999999-9999-9999-9999-999999999999";
const WALLET_ID = "44444444-4444-4444-4444-444444444444";
const TXN_ID = "55555555-5555-5555-5555-555555555555";

function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
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
        { id: CATEGORY_ID, name: "Тепловое оборудование", useful_life_months: 84, note: null },
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

async function openIntake(page: import("@playwright/test").Page) {
  await page.goto("/dds");
  await page.getByRole("tab", { name: /Журнал ДДС/ }).click();
  await page.getByRole("row").filter({ hasText: "Покупка ОС" }).first().click();
  await page.getByText("нужен объект основных средств").click();
  await page.getByRole("button", { name: /Завести новый объект/ }).click();
}

test("модель уточняет, ответ уходит в следующий запрос вместе со всей перепиской", async ({
  page,
}) => {
  await mockBase(page);
  const asked: unknown[] = [];
  await page.route("**/api/v1/fixed-assets/intake", async (route) => {
    const body = route.request().postDataJSON();
    asked.push(body);
    if (body.history.length === 0) {
      return fulfillJson(route, {
        status: "need_more",
        question: "Что за стол?",
        why: "От материала зависит категория и срок службы.",
        suggestions: ["из нержавеющей стали", "деревянный"],
        name: null,
        brand: null,
        model: null,
        category_id: null,
        category_name: null,
        specs: null,
        reason: null,
      });
    }
    return fulfillJson(route, {
      status: "ready",
      question: null,
      why: null,
      suggestions: [],
      name: "Стол производственный из нержавеющей стали",
      brand: null,
      model: null,
      category_id: CATEGORY_ID,
      category_name: "Тепловое оборудование",
      specs: "нержавеющая сталь",
      reason: "Кухонное оборудование, а не офисная мебель.",
    });
  });

  await openIntake(page);
  const dialog = page.getByRole("dialog").last();

  // Одно поле вместо формы — сотрудник пишет своими словами.
  await dialog.getByPlaceholder(/купили рисоварку/).fill("купили стол");
  await dialog.getByRole("button", { name: "Дальше" }).click();

  // Вопрос виден, и рядом — зачем он задан.
  await expect(dialog.getByText("Что за стол?")).toBeVisible();
  await expect(dialog.getByText(/От материала зависит категория/)).toBeVisible();

  // Быстрый ответ одним тапом — окно заводят с планшета.
  await dialog.getByRole("button", { name: "из нержавеющей стали" }).click();

  // Карточка-предложение показана целиком и с объяснением выбора категории.
  await expect(dialog.getByText(/Кухонное оборудование, а не офисная мебель/)).toBeVisible();
  await expect(
    dialog.locator(`input[value="Стол производственный из нержавеющей стали"]`),
  ).toBeVisible();
  await expect(dialog.locator(`input[value="нержавеющая сталь"]`)).toBeVisible();

  // Вся переписка ушла во второй запрос: без неё модель спросила бы то же самое заново.
  expect(asked).toHaveLength(2);
  expect(asked[1]).toMatchObject({
    purchase: "купили стол",
    history: [{ question: "Что за стол?", answer: "из нержавеющей стали" }],
  });
});

test("модель недоступна — окно переходит к ручной форме, а не встаёт", async ({ page }) => {
  await mockBase(page);
  await page.route("**/api/v1/fixed-assets/intake", (route) =>
    route.fulfill({
      status: 422,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Ключ ANTHROPIC_API_KEY не принят (401)" }),
    }),
  );

  await openIntake(page);
  const dialog = page.getByRole("dialog").last();
  await dialog.getByPlaceholder(/купили рисоварку/).fill("рисоварка");
  await dialog.getByRole("button", { name: "Дальше" }).click();

  // Платёж должен провестись в любом случае: недоступность модели не повод не записать
  // покупку. Поэтому вместо тупика — обычные поля, уже с тем, что человек успел написать.
  await expect(dialog.getByText("Наименование")).toBeVisible();
  await expect(dialog.locator(`input[value="рисоварка"]`)).toBeVisible();
  await expect(dialog.getByText("Категория")).toBeVisible();
});

test("категорию, предложенную моделью, можно поправить до записи", async ({ page }) => {
  await mockBase(page);
  await page.route("**/api/v1/fixed-assets/intake", (route) =>
    fulfillJson(route, {
      status: "ready",
      question: null,
      why: null,
      suggestions: [],
      name: "Рисоварка промышленная",
      brand: "Gastrorag",
      model: "DH-RC-2",
      // Категорию модель предложить не смогла (её не было в справочнике) — поле пустое, и
      // «Завести» обязано быть заблокировано: объект без срока не амортизируется вовсе.
      category_id: null,
      category_name: null,
      specs: null,
      reason: null,
    }),
  );

  await openIntake(page);
  const dialog = page.getByRole("dialog").last();
  await dialog.getByPlaceholder(/купили рисоварку/).fill("рисоварка");
  await dialog.getByRole("button", { name: "Дальше" }).click();

  await expect(dialog.locator(`input[value="Gastrorag"]`)).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Завести и выбрать" })).toBeDisabled();

  await dialog.getByText(/Тепловое оборудование · 7 лет/).click();
  await expect(dialog.getByRole("button", { name: "Завести и выбрать" })).toBeEnabled();
});
