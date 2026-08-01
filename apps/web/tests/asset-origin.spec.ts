import { expect, test, type Route } from "@playwright/test";

// «Откуда объект» в форме заведения карточки.
//
// Вопрос закрывает дыру, которую видно только в балансе: объект, за который фирма не платила,
// увеличивает актив, ничего не уменьшая, и без встречной записи в пассиве баланс не сойдётся.
// Спросить можно ровно один раз — в момент заведения; через год про мангал никто не вспомнит.
//
// Поэтому проверяется не «поле есть», а три вещи: без ответа завести нельзя, ответ доезжает до
// бэкенда, и он же выбирает базу оценки (покупка — по платежу, всё остальное — рыночная).

function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

const EMPTY_SUMMARY = {
  count: 0,
  initial_cost: "0.00",
  accumulated: "0.00",
  residual: "0.00",
  monthly_amount: "0.00",
  last_closed_month: null,
  by_category: [],
  by_location: [],
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
  await page.route("**/api/v1/fixed-assets/categories**", (route) =>
    fulfillJson(route, { items: [] }),
  );
});

/** Открыть «Учёт ОС» и форму заведения карточки, перехватив создание. */
async function openCreateForm(page: import("@playwright/test").Page, created: unknown[]) {
  await page.route("**/api/v1/fixed-assets**", (route) => {
    const url = new URL(route.request().url());
    const tail = url.pathname.split("/fixed-assets")[1] ?? "";

    if (route.request().method() === "POST" && (tail === "" || tail === "/")) {
      created.push(route.request().postDataJSON());
      return route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({ id: "new-asset", inventory_number: "ОС-0150" }),
      });
    }
    if (tail.startsWith("/summary")) return fulfillJson(route, EMPTY_SUMMARY);
    if (tail === "" || tail.startsWith("?")) return fulfillJson(route, { items: [], total: 0 });
    return fulfillJson(route, { items: [] });
  });

  await page.goto("/fixed-assets");
  await page.getByRole("button", { name: /Завести карточку/ }).click();
  return page.getByRole("dialog").filter({ hasText: "Новая карточка ОС" });
}

test("без ответа «откуда» карточку завести нельзя", async ({ page }) => {
  const dialog = await openCreateForm(page, []);

  await dialog.getByLabel("Наименование").fill("Ноутбук собственника");
  await dialog.getByLabel("Стоимость, ₽").fill("50000");

  // Имя и сумма есть, а происхождения нет — и этого достаточно, чтобы кнопка не пускала.
  // Иначе объект молча встал бы в актив без встречной записи в пассиве.
  await expect(dialog.getByRole("button", { name: "Завести" })).toBeDisabled();
});

test("вклад собственника уходит на бэк вместе с рыночной оценкой", async ({ page }) => {
  const created: unknown[] = [];
  const dialog = await openCreateForm(page, created);

  await dialog.getByLabel("Наименование").fill("Ноутбук собственника");
  await dialog.getByLabel("Стоимость, ₽").fill("50000");
  await dialog.getByLabel("Откуда объект").click();
  await page.getByRole("option", { name: /Вклад собственника/ }).click();

  // Пояснение под полем меняется вместе с ответом: человек видит последствие своего выбора.
  await expect(dialog.getByText(/Актив вырастет, а деньги не тратились/)).toBeVisible();

  const sent = page.waitForResponse("**/api/v1/fixed-assets");
  await dialog.getByRole("button", { name: "Завести" }).click();
  await sent;

  // Оценка выводится из происхождения, а не спрашивается отдельным бухгалтерским вопросом.
  expect(created).toHaveLength(1);
  expect(created[0]).toMatchObject({
    name: "Ноутбук собственника",
    acquisition_source: "owner_contribution",
    valuation_basis: "market",
  });
});

test("покупка за деньги фирмы ставит оценку по платежу", async ({ page }) => {
  const created: unknown[] = [];
  const dialog = await openCreateForm(page, created);

  await dialog.getByLabel("Наименование").fill("Печь для пиццы");
  await dialog.getByLabel("Стоимость, ₽").fill("95000");
  await dialog.getByLabel("Откуда объект").click();
  await page.getByRole("option", { name: /Куплено за деньги фирмы/ }).click();

  // У покупки встречной записи в пассиве не возникает — и подсказка говорит именно это.
  await expect(dialog.getByText(/просто заменит собой потраченные деньги/)).toBeVisible();

  const sent = page.waitForResponse("**/api/v1/fixed-assets");
  await dialog.getByRole("button", { name: "Завести" }).click();
  await sent;

  expect(created[0]).toMatchObject({
    acquisition_source: "purchase",
    valuation_basis: "payment",
  });
});
