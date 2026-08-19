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
const PREPAYMENT_ARTICLE_ID = "22222222-2222-2222-2222-222222222222";
const SUPPLIER_ID = "33333333-3333-3333-3333-333333333333";

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
        {
          id: PREPAYMENT_ARTICLE_ID,
          code: "advance_to_supplier",
          name: "Авансы поставщикам",
          flow: "supplier_prepayment",
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
  await page.route("**/api/v1/counterparties/registry**", (route) =>
    fulfillJson(route, [
      {
        counterparty_id: SUPPLIER_ID,
        name: "ООО Поставщик",
        inn: "7701234567",
        relationship: "official",
        has_requisites: true,
        requisites_verified: true,
      },
    ]),
  );
});

// Окно держит ВСЕ формы смонтированными и прячет неактивные классом `hidden`, поэтому любой
// локатор по тексту находит и невидимых близнецов. Отсюда `visible: true` и точные имена полей.
type Dialog = ReturnType<Page["getByRole"]>;

/** Поле суммы расходной строки (в окне рядом живёт поле суммы предоплаты). */
function amountBox(dialog: Dialog) {
  return dialog.getByRole("textbox", { name: "Сумма", exact: true });
}

/** Блок НДС видимой формы. */
function vatGroup(dialog: Dialog) {
  return dialog.getByRole("group", { name: "НДС" }).filter({ visible: true });
}

/** Живая строка «В назначение платежа уйдёт: …» видимой формы. */
function vatPreview(dialog: Dialog) {
  return dialog.locator("p[aria-live='polite']").filter({ visible: true });
}

/** Открыть «Новый платёж», выбрать свободную статью и вписать сумму. */
async function openExpense(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: "Активные платежи" }).click();

  const payments = page.getByRole("dialog").filter({ hasText: "Активные платежи" });
  await payments.getByRole("button", { name: "Создать", exact: true }).click();

  const dialog = page.getByRole("dialog").filter({ hasText: "Новый платёж" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "SEO-оптимизация" }).first().click();
  // Формы окна остаются смонтированными рядом, поэтому имя поля берём точным: у строки
  // расхода это ровно «Сумма», у предоплаты — «Сумма, ₽».
  await amountBox(dialog).fill("7984,90");
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
  await amountBox(dialog).fill("3,33");
  await dialog.getByRole("button", { name: "20%", exact: true }).click();
  await expect(dialog.getByText("В т.ч. НДС: 20% - 0,56 руб.")).toBeVisible();

  // Вторая такая же половинка на другом порядке — расхождение не зависит от масштаба суммы.
  await amountBox(dialog).fill("6,15");
  await expect(dialog.getByText("В т.ч. НДС: 20% - 1,03 руб.")).toBeVisible();
});

test("сумма округляется до копеек так же, как её округлит бэк", async ({ page }) => {
  // Питон берёт число через кратчайшее десятичное представление и округляет вверх: 1024,995
  // для него ровно 1024,995 → 1025,00. В double то же число лежит как 1024,99499…, и
  // округление по двоичному значению дало бы 1024,99 — налог посчитался бы с суммы, которой
  // не будет, а рядом в том же окне «Итого» показывало бы 1 025,00 ₽.
  const dialog = await openExpense(page);
  await amountBox(dialog).fill("1024,995");
  await dialog.getByRole("button", { name: "22%", exact: true }).click();
  await expect(dialog.getByText("В т.ч. НДС: 22% - 184,84 руб.")).toBeVisible();
  await expect(dialog.getByText(/Итого\s*1\s*025,00/)).toBeVisible();
});

test("итог транша складывается построчно в копейках, как на бэке", async ({ page }) => {
  // Бэк округляет КАЖДУЮ строку (`_money(line.amount)`) и только потом суммирует: две строки
  // по 1,005 дают 2,02, а не 2,01. Сложение сырых долей рубля показало бы не тот итог,
  // который спишет банк.
  const dialog = await openExpense(page);
  await amountBox(dialog).fill("1,005");
  await dialog.getByRole("button", { name: "Добавить строку" }).click();
  await dialog.getByRole("button", { name: "SEO-оптимизация" }).nth(1).click();
  await amountBox(dialog).nth(1).fill("1,005");
  await expect(dialog.getByText(/Итого\s*2,02/)).toBeVisible();
});

test("длинное назначение ужимается, налог в предпросмотре остаётся целым", async ({ page }) => {
  // Лимит платёжки (210 символов) съедает ОПИСАНИЕ, а не налог, и предпросмотр обязан
  // показывать именно то, что поместится: обещать строку, которая в банк не влезет, —
  // то же самое, что молчать про обрезку.
  const dialog = await openExpense(page);
  await dialog.getByPlaceholder("Назначение (необязательно)").fill("Услуги доставки ".repeat(20));
  await dialog.getByRole("button", { name: "22%", exact: true }).click();

  const preview = vatPreview(dialog);
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

test("группа ставок названа, а живая строка назначения анонсируется", async ({ page }) => {
  // Поле существует ради сверки результата. Без имени группы шесть кнопок «Без НДС / 22% / …»
  // читаются вслепую, а без aria-live человек нажмёт ставку и не узнает, что уйдёт в банк.
  const dialog = await openExpense(page);
  await expect(vatGroup(dialog)).toBeVisible();
  await expect(vatPreview(dialog)).toContainText("В назначение платежа уйдёт");
});

test("ставка выбрана раньше суммы — назначение всё равно видно", async ({ page }) => {
  const dialog = await openExpense(page);
  await amountBox(dialog).fill("");
  await dialog.getByRole("button", { name: "22%", exact: true }).click();
  // Молчать нельзя: человек уже сказал «с НДС», и «Без НДС.» под этим читалось бы как отказ.
  await expect(dialog.getByText(/доля НДС 22% посчитается/)).toBeVisible();
  await expect(dialog.getByText("SEO-оптимизация.")).toBeVisible();
});

test("у наличного счёта блока НДС нет — платёж в банк не уходит", async ({ page }) => {
  const dialog = await openExpense(page);
  await expect(dialog.getByText("SEO-оптимизация. Без НДС.")).toBeVisible();

  await dialog.getByRole("button", { name: "Сейф", exact: true }).click();
  await expect(vatGroup(dialog)).toHaveCount(0);
  await expect(vatPreview(dialog)).toHaveCount(0);
});

/** Открыть форму предоплаты поставщику с выбранным получателем и суммой. */
async function openPrepayment(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: "Активные платежи" }).click();

  const payments = page.getByRole("dialog").filter({ hasText: "Активные платежи" });
  await payments.getByRole("button", { name: "Создать", exact: true }).click();

  const dialog = page.getByRole("dialog").filter({ hasText: "Новый платёж" });
  await dialog.getByRole("button", { name: "Авансы поставщикам" }).first().click();
  await dialog.getByRole("button", { name: "ООО Поставщик" }).click();
  await dialog.getByRole("textbox", { name: "Сумма, ₽" }).fill("7984,90");
  return dialog;
}

test("предоплата поставщику: ставка в форме и в запросе", async ({ page }) => {
  // Аванс уходит внешнему юрлицу по его реквизитам — маршрут, где налог в назначении
  // обязателен по-настоящему. До правки это назначение молчало о налоге вовсе.
  const dialog = await openPrepayment(page);
  await expect(dialog.getByText("Предоплата поставщику ООО Поставщик. Без НДС.")).toBeVisible();

  await dialog.getByRole("button", { name: "22%", exact: true }).click();
  await expect(dialog.getByText("В т.ч. НДС: 22% - 1439,90 руб.")).toBeVisible();

  let sent: Record<string, unknown> | null = null;
  await page.route("**/api/v1/counterparties/prepayments/bank-draft", (route) => {
    sent = route.request().postDataJSON();
    return fulfillJson(route, { id: "draft-2", amount: 7984.9, status: "created" });
  });
  await dialog.getByRole("button", { name: "Отправить в банк" }).click();
  await expect.poll(() => sent).not.toBeNull();
  expect(sent!.vat_rate).toBe("22");
});

test("предоплата наличными: блока НДС нет", async ({ page }) => {
  const dialog = await openPrepayment(page);
  await dialog.getByRole("button", { name: "Сейф", exact: true }).click();
  await expect(vatGroup(dialog)).toHaveCount(0);
});
