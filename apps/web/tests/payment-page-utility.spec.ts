import { expect, test, type Route } from "@playwright/test";

/**
 * Коммунальная платёжка на «Странице на оплату».
 *
 * Проверяем то, что человек читает глазами перед тем, как отправить деньги: сумма к оплате и
 * расход за период — РАЗНЫЕ числа у акта за факт, и показать надо оба. Покажи одно — июнь
 * недосчитается 65 000 ₽ зачтённого аванса, и недостача будет выглядеть экономией.
 *
 * И вторая половина: два акта одного визита выбираются вместе и уходят одним переводом —
 * ровно так их и платят.
 */

function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

const BASE_INTAKE = {
  mailbox: "photo",
  from_addr: null,
  subject: null,
  received_at: null,
  attachment_filename: "IMG_0001.jpg",
  status: "linked",
  engine: "utility:vision",
  confidence: 0.9,
  counterparty_id: "landlord-1",
  counterparty_name: "Гордеев Виталий Анатольевич",
  companion_amount: null,
  recipient_name: "Гордеев Виталий Анатольевич",
  inn: "614314309921",
  invoice_date: "2026-07-17",
  service_period_source: "utility",
  service_period_status: "ready",
  service_period_confidence: null,
  service_period_required: false,
  service_billing_mode: null,
  requisites: {},
  reviewed_requisites: {},
  requisites_verified: false,
  invoice_payment_status: "unpaid",
  invoice_in_draft: false,
  invoice_dds_article_id: null,
  default_dds_article_id: null,
  has_pdf: true,
  attachment_mime: "image/jpeg",
  utility_account_id: "flow-power",
  utility_kind: "electricity",
  utility_kind_label: "Электричество",
  utility_period_label: null,
  utility_blocking: [],
  scheduled_send_date: null,
  created_at: "2026-07-17T10:00:00Z",
};

const ACTUAL = {
  ...BASE_INTAKE,
  id: "intake-actual",
  invoice_id: "invoice-actual",
  companion_invoice_id: "invoice-closing",
  companion_amount: "95402.00",
  amount: "30402.00",
  invoice_number: "Возмещение: электричество, 06.2026",
  service_period_start: "2026-06-01",
  service_period_end: "2026-06-30",
  utility_act_kind: "actual",
  utility_expense_amount: "95402.00",
  utility_payable_amount: "30402.00",
  utility_hints: ["Есть только акт за факт — ждём авансовый"],
};

const ADVANCE = {
  ...BASE_INTAKE,
  id: "intake-advance",
  invoice_id: "invoice-advance",
  companion_invoice_id: null,
  amount: "65000.00",
  invoice_number: "Возмещение: электричество, 07.2026",
  service_period_start: "2026-07-01",
  service_period_end: "2026-07-31",
  utility_act_kind: "advance",
  utility_expense_amount: null,
  utility_payable_amount: "65000.00",
  utility_hints: [],
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
  await page.route("**/api/v1/payment-page/intakes", (route) =>
    fulfillJson(route, [ACTUAL, ADVANCE]),
  );
  await page.route(/\/api\/v1\/counterparties(\?.*)?$/, (route) => fulfillJson(route, []));
});

test("акт за факт показывает и платёж, и расход периода", async ({ page }) => {
  await page.goto("/finance/payments");

  // Различаем строки по НОМЕРУ документа, а не по дате: обе бумаги подписаны 17.07.2026,
  // и по дате в строку попадали бы обе.
  const row = page.getByRole("row").filter({ hasText: "Возмещение: электричество, 06.2026" });
  await expect(row).toContainText("30 402");
  // Расход месяца больше платежа на зачтённый аванс — и это видно, не открывая документ.
  await expect(row).toContainText("расход");
  await expect(row).toContainText("95 402");
  await expect(row).toContainText("Электричество");
  // Подсказка не мешает платить, но объясняет, чего ждать дальше.
  await expect(row).toContainText("ждём авансовый");
});

test("аванс помечен как аванс и расхода не показывает", async ({ page }) => {
  await page.goto("/finance/payments");

  const row = page.getByRole("row").filter({ hasText: "Возмещение: электричество, 07.2026" });
  await expect(row).toContainText("65 000");
  await expect(row).toContainText("аванс");
  await expect(row).not.toContainText("расход");
});

test("готовая коммунальная платёжка отправляется без повторного обычного разбора", async ({ page }) => {
  const calls: string[] = [];
  await page.route("**/api/v1/dds/articles**", (route) => fulfillJson(route, []));
  await page.route(
    "**/api/v1/payment-page/counterparties/landlord-1/requisites",
    (route) =>
      fulfillJson(route, {
        counterparty_id: "landlord-1",
        name: "Гордеев Виталий Анатольевич",
        inn: null,
        requisites: {},
        requisites_verified: false,
      }),
  );
  // Если фронтенд снова вызовет этот путь, сервер вернёт ту же ошибку, которую увидел человек.
  await page.route("**/api/v1/payment-page/intakes/intake-actual/confirm", (route) => {
    calls.push("confirm");
    return route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Коммунальную платёжку проводят через окно коммунальных услуг" }),
    });
  });
  await page.route("**/api/v1/payment-page/intakes/intake-actual/send-to-bank", (route) => {
    calls.push("send-to-bank");
    return fulfillJson(route, { ...ACTUAL, invoice_in_draft: true });
  });

  await page.goto("/finance/payments");
  const row = page.getByRole("row").filter({ hasText: "Возмещение: электричество, 06.2026" });
  await row.getByRole("button", { name: "В банк" }).click();

  const dialog = page.getByRole("dialog");
  await dialog.locator("label", { hasText: "нет реквизитов" }).locator("input").check();
  await dialog.getByRole("button", { name: "Отправить в банк" }).click();

  await expect.poll(() => calls).toEqual(["send-to-bank"]);
});

test("два акта выбираются вместе и уходят одним переводом", async ({ page }) => {
  await page.goto("/finance/payments");

  const boxes = page.getByRole("checkbox", { name: "Выбрать для общего платежа" });
  await expect(boxes).toHaveCount(2);
  await boxes.nth(0).check();
  await boxes.nth(1).check();

  // 30 402 + 65 000 = 95 402 — сумма одного перевода.
  await expect(page.getByText(/Выбрано 2 на/)).toContainText("95 402");

  let sentIds: string[] = [];
  await page.route("**/api/v1/payment-page/intakes/send-to-bank", async (route) => {
    sentIds = JSON.parse(route.request().postData() ?? "{}").intake_ids ?? [];
    await fulfillJson(route, [ACTUAL, ADVANCE]);
  });
  await page.getByRole("button", { name: "Оплатить вместе" }).click();

  await expect
    .poll(() => sentIds)
    .toEqual(["intake-actual", "intake-advance"]);
});

test("окно разбора спрашивает поток, период и две суммы", async ({ page }) => {
  await page.route("**/api/v1/payment-page/intakes", (route) =>
    fulfillJson(route, [{ ...ACTUAL, status: "needs_review", invoice_id: null }]),
  );
  await page.route("**/api/v1/accounting/utilities/accounts**", (route) =>
    fulfillJson(route, {
      items: [
        {
          id: "flow-power",
          location_id: "loc-1",
          location_name: "Черникова",
          kind: "electricity",
          kind_label: "Электричество",
          counterparty_id: "landlord-1",
          counterparty_name: "Гордеев Виталий Анатольевич",
          dds_article_id: "article-1",
          dds_article_name: "Коммунальные платежи",
          expected_day: 20,
          started_on: "2026-01-01",
          ended_on: null,
          is_active: true,
          note: null,
        },
      ],
    }),
  );

  await page.goto("/finance/payments");
  await page.getByRole("button", { name: "Разобрать" }).click();

  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("Коммунальная платёжка");
  await expect(dialog).toContainText("Поток (помещение и ресурс)");
  await expect(dialog.getByLabel("К оплате, ₽")).toHaveValue("30402.00");
  await expect(dialog.getByLabel("Расход за период, ₽")).toHaveValue("95402.00");
});

test("снимок показывается картинкой и увеличивается до читаемого", async ({ page }) => {
  await page.route("**/api/v1/payment-page/intakes", (route) =>
    fulfillJson(route, [{ ...ACTUAL, status: "needs_review", invoice_id: null }]),
  );
  await page.route("**/api/v1/accounting/utilities/accounts**", (route) =>
    fulfillJson(route, { items: [] }),
  );
  // Однопиксельный JPEG: проверяем ветку показа, а не содержимое снимка.
  await page.route("**/api/v1/payment-page/intakes/intake-actual/pdf", (route) =>
    route.fulfill({
      status: 200,
      contentType: "image/jpeg",
      body: Buffer.from(
        "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a" +
          "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA" +
          "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==",
        "base64",
      ),
    }),
  );

  await page.goto("/finance/payments");
  await page.getByRole("button", { name: "Разобрать" }).click();
  const dialog = page.getByRole("dialog");

  // Картинкой, а не фреймом: JPEG во фрейме браузер рисует пустым прямоугольником, и сверять
  // разбор человеку становится не по чему.
  const shot = dialog.getByAltText("Снимок документа");
  await expect(shot).toBeVisible();

  // Вписанная в колонку страница акта нечитаема — строку «Оплачено аванс: 65000р» надо
  // увидеть глазами, поэтому снимок разворачивается в натуральный размер.
  await expect(shot).toHaveClass(/object-contain/);
  await dialog.getByRole("button", { name: "Увеличить" }).click();
  await expect(shot).toHaveClass(/max-w-none/);
  await expect(dialog.getByRole("button", { name: "Вписать" })).toBeVisible();
});
