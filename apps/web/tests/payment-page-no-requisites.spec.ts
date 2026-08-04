import { expect, test, type Route } from "@playwright/test";

// Счёт от получателя, у которого банковских реквизитов нет и не будет: арендодатель с
// возмещением коммуналки, физлицо со счётом на бумаге. Окно отправки требовало пять полей
// платёжки — заполнить их было нечем, и кнопка «Отправить в банк» оставалась мёртвой.
// Галочка «у получателя нет реквизитов» переводит платёж на карту ИП → Сейф.

const CP_ID = "22222222-2222-2222-2222-222222222222";
const INTAKE_ID = "33333333-3333-3333-3333-333333333333";
const INVOICE_ID = "44444444-4444-4444-4444-444444444444";

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
    from_addr: null,
    subject: "Возмещение за воду, 07.2026",
    received_at: "2026-08-01T09:00:00Z",
    attachment_filename: "voda-07-2026.jpg",
    status: "linked",
    engine: "vision",
    confidence: 0.71,
    counterparty_id: CP_ID,
    counterparty_name: "Станислав Юрьевич",
    invoice_id: INVOICE_ID,
    companion_invoice_id: null,
    companion_amount: null,
    recipient_name: "Станислав Юрьевич",
    inn: null,
    amount: "9654.25",
    invoice_number: "Возмещение",
    invoice_date: "2026-07-31",
    service_period_start: "2026-07-01",
    service_period_end: "2026-07-31",
    service_period_source: "document",
    service_period_status: "ready",
    service_period_confidence: 0.9,
    service_period_required: false,
    service_billing_mode: null,
    // В бумаге стоят реквизиты Водоканала, а платим мы арендодателю — своих у него нет.
    requisites: {},
    reviewed_requisites: {},
    requisites_verified: false,
    invoice_payment_status: "unpaid",
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
    created_at: "2026-08-01T09:00:00Z",
    ...overrides,
  };
}

async function mockCommon(
  page: import("@playwright/test").Page,
  card: Record<string, string> = {},
) {
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
  await page.route("**/api/v1/dds/articles**", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/counterparties/registry**", (route) =>
    fulfillJson(route, [{ counterparty_id: CP_ID, name: "Станислав Юрьевич", inn: null }]),
  );
  await page.route(`**/api/v1/payment-page/counterparties/${CP_ID}/requisites`, (route) =>
    fulfillJson(route, {
      counterparty_id: CP_ID,
      name: "Станислав Юрьевич",
      inn: null,
      requisites: card,
      requisites_verified: Object.keys(card).length > 0,
    }),
  );
  await page.route("**/api/v1/payment-page/intakes**", (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    return fulfillJson(route, [intake()]);
  });
}

test("без реквизитов кнопка мертва, галочка выводит платёж на карту ИП", async ({ page }) => {
  await mockCommon(page);
  const calls: { url: string; body: unknown }[] = [];
  await page.route(`**/api/v1/payment-page/intakes/${INTAKE_ID}/confirm`, (route) => {
    calls.push({ url: "confirm", body: route.request().postDataJSON() });
    return fulfillJson(route, intake());
  });
  await page.route(`**/api/v1/payment-page/intakes/${INTAKE_ID}/send-to-bank`, (route) => {
    calls.push({ url: "send-to-bank", body: route.request().postDataJSON() });
    return fulfillJson(route, intake({ invoice_in_draft: true }));
  });

  await page.goto("/finance/payments");
  await page.getByRole("button", { name: "В банк" }).first().click();

  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("Отправка счёта в банк")).toBeVisible();
  // Платить некуда: реквизитов нет ни в бумаге, ни в карточке.
  await expect(dialog.getByRole("button", { name: "Отправить в банк" })).toBeDisabled();
  await expect(
    dialog.getByText("Заполните название, ИНН, БИК, расчётный и корреспондентский счета."),
  ).toBeVisible();

  const consent = dialog.locator("label", { hasText: "нет реквизитов" }).locator("input");
  await consent.check();

  // Маршрут назван прямо: деньги пойдут на карту ИП и осядут на Сейфе до выдачи.
  await expect(dialog.getByText(/деньги придут на Сейф/)).toBeVisible();
  await dialog.getByRole("button", { name: "Отправить в банк" }).click();

  await expect.poll(() => calls.map((call) => call.url)).toEqual(["confirm", "send-to-bank"]);
  const confirm = calls[0].body as Record<string, unknown>;
  // Реквизиты не переносим в карточку: в бумаге стоит ресурсник, а платим арендодателю —
  // занеси мы их сейчас, следующий платёж ушёл бы по ним молча и мимо получателя.
  expect(confirm.apply_requisites).toBe(false);
  expect(confirm.requisites).toBeUndefined();
  expect((calls[1].body as Record<string, unknown>).pays_via_safe).toBe(true);
});

test("у контрагента есть реквизиты — выбора «на карту ИП» не предлагаем", async ({ page }) => {
  await mockCommon(page, {
    recipientName: "Станислав Юрьевич",
    inn: "614301695606",
    bankAcnt: "40702810400000012349",
    bankBik: "044525225",
    recipientCorrAccountNumber: "30101810400000000225",
  });

  await page.goto("/finance/payments");
  await page.getByRole("button", { name: "В банк" }).first().click();

  const dialog = page.getByRole("dialog");
  await expect(dialog.getByLabel("Расчётный счёт")).toHaveValue("40702810400000012349");
  // Счёт получателя известен — платим по нему, и обойти это галочкой нельзя.
  await expect(dialog.locator("label", { hasText: "нет реквизитов" })).toHaveCount(0);
  await expect(dialog.getByRole("button", { name: "Отправить в банк" })).toBeEnabled();
});
