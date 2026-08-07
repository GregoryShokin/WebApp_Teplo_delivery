import { expect, test, type Page, type Route } from "@playwright/test";

// НДС в окнах разбора и отправки: цифра отсюда уходит в ТЕКСТ назначения платежа, который
// читают банк и налоговая. Поэтому окно показывает не только поля, но и готовую строку
// платёжки, а нераспознанный налог подсвечивает — молчание здесь означало бы «Без НДС.» по
// счёту, где налог есть.

const CP_ID = "22222222-2222-2222-2222-222222222222";
const INTAKE_ID = "33333333-3333-3333-3333-333333333333";

const CARD_REQUISITES = {
  recipientName: 'ООО "СДЭК-СЛАВЯНСК"',
  inn: "2370006152",
  kpp: "237001001",
  bankAcnt: "40702810400000012349",
  bankBik: "044525225",
  recipientCorrAccountNumber: "30101810400000000225",
};

function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function intake(overrides: Record<string, unknown> = {}) {
  return {
    id: INTAKE_ID,
    mailbox: "corporate",
    from_addr: "buh@cdek.ru",
    subject: "Счёт на оплату № СКБ-0437096",
    received_at: "2026-07-19T09:00:00Z",
    attachment_filename: "schet-skb-0437096.pdf",
    status: "needs_review",
    engine: "deterministic",
    confidence: 0.85,
    counterparty_id: CP_ID,
    counterparty_name: 'ООО "СДЭК-СЛАВЯНСК"',
    invoice_id: null,
    companion_invoice_id: null,
    companion_amount: null,
    recipient_name: 'ООО "СДЭК-СЛАВЯНСК"',
    inn: "2370006152",
    amount: "7984.90",
    invoice_number: "СКБ-0437096",
    invoice_date: "2026-07-19",
    service_period_start: null,
    service_period_end: null,
    service_period_source: null,
    service_period_status: "not_required",
    service_period_confidence: null,
    service_period_required: false,
    vat_mode: "included",
    vat_rate: "22",
    vat_amount: "1439.90",
    service_billing_mode: null,
    requisites: CARD_REQUISITES,
    reviewed_requisites: {},
    requisites_verified: true,
    invoice_payment_status: null,
    invoice_in_draft: false,
    invoice_dds_article_id: null,
    default_dds_article_id: null,
    has_pdf: false,
    attachment_mime: null,
    utility_account_id: null,
    utility_kind: null,
    utility_kind_label: null,
    utility_act_kind: null,
    utility_expense_amount: null,
    utility_payable_amount: null,
    utility_period_label: null,
    utility_hints: [],
    utility_blocking: [],
    scheduled_send_date: null,
    scheduled_pays_via_safe: false,
    created_at: "2026-07-19T09:00:00Z",
    ...overrides,
  };
}

async function mockCommon(page: Page) {
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
  await page.route("**/api/v1/counterparties/registry**", (route) =>
    fulfillJson(route, [{ counterparty_id: CP_ID, name: 'ООО "СДЭК-СЛАВЯНСК"', inn: "2370006152" }]),
  );
  await page.route(`**/api/v1/payment-page/counterparties/${CP_ID}/requisites`, (route) =>
    fulfillJson(route, {
      counterparty_id: CP_ID,
      name: 'ООО "СДЭК-СЛАВЯНСК"',
      inn: "2370006152",
      requisites: CARD_REQUISITES,
      requisites_verified: true,
    }),
  );
}

test("распознанный НДС показан со ставкой и собран в строку назначения", async ({ page }) => {
  await mockCommon(page);
  await page.route("**/api/v1/payment-page/intakes**", (route) => fulfillJson(route, [intake()]));

  await page.goto("/finance/payments");
  await page.getByRole("button", { name: "Разобрать" }).first().click();

  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("Разбор счёта на оплату")).toBeVisible();

  await expect(dialog.getByLabel("Сумма НДС")).toHaveValue("1439.90");
  await expect(dialog.getByLabel("Ставка, %")).toHaveValue("22");
  await expect(dialog.getByText("из счёта").last()).toBeVisible();
  // Человек сверяет с бумагой РЕЗУЛЬТАТ, а не исходники: в платёжку уйдёт именно эта строка.
  await expect(dialog.getByText("В т.ч. НДС: 22% - 1439,90 руб.")).toBeVisible();
});

test("нераспознанный НДС подсвечен: иначе банк молча получит «Без НДС.»", async ({ page }) => {
  await mockCommon(page);
  await page.route("**/api/v1/payment-page/intakes**", (route) =>
    fulfillJson(route, [intake({ vat_mode: "", vat_rate: null, vat_amount: null })]),
  );

  await page.goto("/finance/payments");
  await page.getByRole("button", { name: "Разобрать" }).first().click();

  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("не распознан", { exact: true })).toBeVisible();
  await expect(dialog.getByText("В назначение платежа уйдёт:")).toBeVisible();
  await expect(dialog.getByText("Без НДС.", { exact: true })).toBeVisible();
  await expect(
    dialog.getByText(/Налог из счёта не распознан\. Если он там есть/),
  ).toBeVisible();

  // Оператор вписывает налог с бумаги — предпросмотр платёжки пересобирается на глазах.
  await dialog.getByLabel("Сумма НДС").fill("1439.90");
  await dialog.getByLabel("Ставка, %").fill("22");
  await expect(dialog.getByText("В т.ч. НДС: 22% - 1439,90 руб.")).toBeVisible();
});

test("«в счёте без НДС» не путается с «не распознан»", async ({ page }) => {
  await mockCommon(page);
  await page.route("**/api/v1/payment-page/intakes**", (route) =>
    fulfillJson(route, [intake({ vat_mode: "none", vat_rate: null, vat_amount: null })]),
  );

  await page.goto("/finance/payments");
  await page.getByRole("button", { name: "Разобрать" }).first().click();

  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("в счёте «без НДС»")).toBeVisible();
  // Претензий к человеку нет: документ сам так сказал, предупреждение не показываем.
  await expect(dialog.getByText(/Налог из счёта не распознан/)).toHaveCount(0);
  await expect(dialog.getByText("Без НДС.", { exact: true })).toBeVisible();
});

test("НДС правится и в окне отправки в банк — последняя точка перед деньгами", async ({ page }) => {
  await mockCommon(page);
  await page.route("**/api/v1/payment-page/intakes**", (route) =>
    fulfillJson(route, [
      intake({ status: "linked", invoice_id: "44444444-4444-4444-4444-444444444444" }),
    ]),
  );
  await page.route("**/api/v1/dds/articles**", (route) => fulfillJson(route, []));

  await page.goto("/finance/payments");
  await page.getByRole("button", { name: "В банк" }).first().click();

  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("Отправка счёта в банк")).toBeVisible();
  await expect(dialog.getByLabel("Сумма НДС")).toHaveValue("1439.90");

  // Опечатка «налог больше платежа» блокирует отправку: цифра уехала бы в текст платёжки.
  await dialog.getByLabel("Сумма НДС").fill("9000.00");
  await expect(dialog.getByText(/НДС не может быть больше суммы счёта/)).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Отправить в банк" })).toBeDisabled();

  await dialog.getByLabel("Сумма НДС").fill("1440.00");
  await expect(dialog.getByText("В т.ч. НДС: 22% - 1440,00 руб.")).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Отправить в банк" })).toBeEnabled();
});
