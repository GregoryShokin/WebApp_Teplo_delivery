import { expect, test, type Page, type Route } from "@playwright/test";

// НДС платежа в окне «Новый платёж».
//
// Назначение платежа обязано называть налог или прямо говорить «Без НДС»: это читают банк и
// налоговая. У платёжки по СЧЁТУ налог берётся с накладной, а свободный расход и предоплата
// счёта не имеют — ставку называет человек прямо в окне.
//
// Тестами закреплено ровно то, что не поймает ни один тест сервиса:
//   1. форма показывает СТРОКУ, которую увидит банк, а не только выбранный процент — сверять
//      надо результат; строка обязана совпасть с той, что соберёт бэк;
//   2. налог ВЫДЕЛЯЕТСЯ из итога («в том числе»), а не начисляется сверху: итог платежа при
//      выборе ставки не меняется;
//   3. у наличного счёта блока нет вовсе — такой платёж в банк не уходит;
//   4. ставка доезжает до запроса (`vat_rate`), а не остаётся украшением формы.

const ARTICLE_ID = "11111111-1111-1111-1111-111111111111";

function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
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
        {
          id: ARTICLE_ID,
          code: "seo_optimizaciya",
          name: "SEO-оптимизация",
          flow: "expense",
          activity: "operating",
          counterparties: [],
          location_required: false,
          lease_bound: false,
          asset_link_kind: null,
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
        {
          id: "wallet-safe",
          code: "cash_safe",
          name: "Сейф",
          bank_code: null,
          kind: "cash",
          location: "safe",
        },
      ],
      employees: [],
    }),
  );
});

/** Открыть «Новый платёж», выбрать свободную статью и вписать сумму. */
async function openExpense(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: "Активные платежи" }).click();

  const payments = page.getByRole("dialog").filter({ hasText: "Активные платежи" });
  await payments.getByRole("button", { name: "Создать", exact: true }).click();

  const dialog = page.getByRole("dialog").filter({ hasText: "Новый платёж" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "SEO-оптимизация" }).first().click();
  await dialog.getByLabel("Сумма").fill("7984,90");
  return dialog;
}

test("форма показывает строку назначения, которую соберёт бэк", async ({ page }) => {
  const dialog = await openExpense(page);

  // По умолчанию — «Без НДС»: утверждение о налоге делается осознанно.
  await expect(dialog.getByText("SEO-оптимизация. Без НДС.")).toBeVisible();

  await dialog.getByRole("button", { name: "22%", exact: true }).click();
  // Формат — эталонный, тот же, что у платёжки по счёту: без разделителя тысяч.
  await expect(dialog.getByText("В т.ч. НДС: 22% - 1439,90 руб.")).toBeVisible();

  // Итог платежа от выбора ставки НЕ меняется: налог выделяется из суммы, а не добавляется
  // к ней. Иначе в банк ушла бы не та сумма, которую согласовали с получателем.
  await expect(dialog.getByText(/Итого\s*7\s*984,90/)).toBeVisible();
});

test("ровные половинки копейки округляются как на бэке, а не как в double", async ({ page }) => {
  // 3,33 ₽ по ставке 20 % — это ровно 0,555 ₽ налога. Бэк считает на Decimal с ROUND_HALF_UP
  // и пишет 0,56; наивная формула в double даёт 0,5549999999999999 и показала бы 0,55.
  // Поле, которое существует ради сверки результата, обязано совпадать с банком до копейки.
  const dialog = await openExpense(page);
  await dialog.getByLabel("Сумма").fill("3,33");
  await dialog.getByRole("button", { name: "20%", exact: true }).click();
  await expect(dialog.getByText("В т.ч. НДС: 20% - 0,56 руб.")).toBeVisible();

  // Вторая такая же половинка на другом порядке — расхождение не зависит от масштаба суммы.
  await dialog.getByLabel("Сумма").fill("6,15");
  await expect(dialog.getByText("В т.ч. НДС: 20% - 1,03 руб.")).toBeVisible();
});

test("длинное назначение ужимается, налог в предпросмотре остаётся целым", async ({ page }) => {
  // Лимит платёжки (210 символов) съедает ОПИСАНИЕ, а не налог, и предпросмотр обязан
  // показывать именно то, что поместится: обещать строку, которая в банк не влезет, —
  // то же самое, что молчать про обрезку.
  const dialog = await openExpense(page);
  await dialog.getByPlaceholder("Назначение (необязательно)").fill("Услуги доставки ".repeat(20));
  await dialog.getByRole("button", { name: "22%", exact: true }).click();

  const preview = dialog.getByText(/^В назначение платежа уйдёт:/);
  await expect(preview).toContainText("В т.ч. НДС: 22% - 1439,90 руб.");
  const text = ((await preview.textContent()) ?? "").replace("В назначение платежа уйдёт: ", "");
  // 210 минус место под техметку [TPL-…], которую добавит бэк.
  expect(text.length).toBeLessThanOrEqual(210 - " [TPL-000000000000]".length);
});

test("ставка доезжает до запроса, а не остаётся украшением формы", async ({ page }) => {
  const dialog = await openExpense(page);
  await dialog.getByRole("button", { name: "10%", exact: true }).click();

  let sent: Record<string, unknown> | null = null;
  await page.route("**/api/v1/dds/new-payment/expense-draft", (route) => {
    sent = route.request().postDataJSON();
    return fulfillJson(route, {
      id: "draft-1",
      amount: 7984.9,
      status: "created",
      provider_ref: "mock",
      last_error: null,
      created_at: "2026-08-19T09:00:00Z",
    });
  });

  await dialog.getByRole("button", { name: "Отправить в банк" }).click();
  await expect.poll(() => sent).not.toBeNull();
  expect(sent!.vat_rate).toBe("10");
});

test("у наличного счёта блока НДС нет — платёж в банк не уходит", async ({ page }) => {
  const dialog = await openExpense(page);
  await expect(dialog.getByText("SEO-оптимизация. Без НДС.")).toBeVisible();

  await dialog.getByRole("button", { name: "Сейф", exact: true }).click();
  await expect(dialog.getByText("В назначение платежа уйдёт:")).toHaveCount(0);
  await expect(dialog.getByRole("button", { name: "22%", exact: true })).toHaveCount(0);
});
