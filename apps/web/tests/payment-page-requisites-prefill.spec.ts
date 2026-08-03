import { expect, test, type Route } from "@playwright/test";

// Окно «Разбор счёта на оплату» узнавало контрагента (по ИНН, по адресу отправителя или по
// точному имени), но реквизиты подставляло только из распознанного PDF. Счёт-скан или письмо,
// разобранное наполовину, оставляли пять полей платёжки пустыми — притом что в карточке
// контрагента они есть и именно по ним платёж и уходит в банк.

const CP_ID = "22222222-2222-2222-2222-222222222222";
const INTAKE_ID = "33333333-3333-3333-3333-333333333333";

const CARD_REQUISITES = {
  recipientName: 'ООО "АЛЬЯНС ЮГ"',
  inn: "6143059250",
  kpp: "614301001",
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
    from_addr: "buh@alliance-yug.ru",
    subject: "Счёт на оплату № 512",
    received_at: "2026-07-30T09:00:00Z",
    attachment_filename: "schet-512.pdf",
    status: "needs_review",
    engine: "deterministic",
    confidence: 0.62,
    counterparty_id: CP_ID,
    counterparty_name: 'ООО "АЛЬЯНС ЮГ"',
    invoice_id: null,
    companion_invoice_id: null,
    companion_amount: null,
    recipient_name: 'ООО "АЛЬЯНС ЮГ"',
    inn: "6143059250",
    amount: "12000.00",
    invoice_number: "512",
    invoice_date: "2026-07-30",
    service_period_start: null,
    service_period_end: null,
    service_period_source: null,
    service_period_status: "not_required",
    service_period_confidence: null,
    service_period_required: false,
    // Парсер вытащил только имя и ИНН: банковский блок в скане не читается.
    requisites: { recipientName: 'ООО "АЛЬЯНС ЮГ"', inn: "6143059250" },
    reviewed_requisites: {},
    requisites_verified: true,
    invoice_payment_status: null,
    invoice_in_draft: false,
    invoice_dds_article_id: null,
    default_dds_article_id: null,
    has_pdf: false,
    scheduled_send_date: null,
    created_at: "2026-07-30T09:00:00Z",
    ...overrides,
  };
}

async function mockCommon(page: import("@playwright/test").Page, card = CARD_REQUISITES) {
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
    fulfillJson(route, [
      { counterparty_id: CP_ID, name: 'ООО "АЛЬЯНС ЮГ"', inn: "6143059250" },
    ]),
  );
  await page.route(`**/api/v1/payment-page/counterparties/${CP_ID}/requisites`, (route) =>
    fulfillJson(route, {
      counterparty_id: CP_ID,
      name: 'ООО "АЛЬЯНС ЮГ"',
      inn: "6143059250",
      requisites: card,
      requisites_verified: true,
    }),
  );
}

test("реквизиты, которых нет в счёте, подставляются из карточки — с подписью источника", async ({
  page,
}) => {
  await mockCommon(page);
  await page.route("**/api/v1/payment-page/intakes**", (route) => fulfillJson(route, [intake()]));

  await page.goto("/finance/payments");
  await page.getByRole("button", { name: "Разобрать" }).first().click();

  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("Разбор счёта на оплату")).toBeVisible();

  // Банковский блок пришёл из карточки — раньше эти поля были пустые.
  await expect(dialog.getByLabel("Расчётный счёт")).toHaveValue("40702810400000012349");
  await expect(dialog.getByLabel("БИК")).toHaveValue("044525225");
  await expect(dialog.getByLabel("Корр. счёт")).toHaveValue("30101810400000000225");
  // …и видно, что это карточка, а не счёт.
  await expect(dialog.getByText("из карточки").first()).toBeVisible();
  // ИНН распознан из самого PDF — источник другой.
  await expect(dialog.getByLabel("ИНН")).toHaveValue("6143059250");
  await expect(dialog.getByText("из счёта").first()).toBeVisible();

  // Переносить в карточку нечего: в форме ровно то, что в ней уже лежит.
  await expect(
    dialog.getByText("В карточке контрагента уже эти реквизиты — переносить нечего."),
  ).toBeVisible();
});

test("расчётный счёт в счёте разошёлся с карточкой — окно просит сверить", async ({ page }) => {
  await mockCommon(page);
  await page.route("**/api/v1/payment-page/intakes**", (route) =>
    fulfillJson(route, [
      intake({
        requisites: {
          recipientName: 'ООО "АЛЬЯНС ЮГ"',
          inn: "6143059250",
          bankAcnt: "40702810900000055555",
          bankBik: "046015602",
        },
      }),
    ]),
  );

  await page.goto("/finance/payments");
  await page.getByRole("button", { name: "Разобрать" }).first().click();

  const dialog = page.getByRole("dialog");
  // Банковский блок целиком из счёта: смешивать свежий счёт со старым БИК карточки нельзя.
  await expect(dialog.getByLabel("Расчётный счёт")).toHaveValue("40702810900000055555");
  await expect(dialog.getByLabel("БИК")).toHaveValue("046015602");
  await expect(dialog.getByLabel("Корр. счёт")).toHaveValue("");
  // И человека предупредили: так выглядит и смена банка, и подмена реквизитов в письме.
  await expect(dialog.getByText(/В счёте расчётный счёт/)).toBeVisible();
  await expect(dialog.getByText(/40702810400000012349/)).toBeVisible();
});

test("правки прошлого разбора важнее и счёта, и карточки", async ({ page }) => {
  await mockCommon(page);
  await page.route("**/api/v1/payment-page/intakes**", (route) =>
    fulfillJson(route, [
      intake({
        status: "linked",
        invoice_id: "44444444-4444-4444-4444-444444444444",
        reviewed_requisites: { ...CARD_REQUISITES, kpp: "614301002" },
      }),
    ]),
  );

  await page.goto("/finance/payments");
  await page.getByRole("button", { name: "Разобрать" }).first().click();

  const dialog = page.getByRole("dialog");
  await expect(dialog.getByLabel("КПП")).toHaveValue("614301002");
  await expect(dialog.getByText("сохранено при разборе").first()).toBeVisible();
});
