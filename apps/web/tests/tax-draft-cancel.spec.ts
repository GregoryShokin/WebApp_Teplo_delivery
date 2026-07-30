import { expect, test, type Route } from "@playwright/test";

/**
 * Налоговый платёж, отправленный в банк, снимается с очереди из окна «Активные платежи».
 *
 * До этого выхода из `in_bank` не было: платёжка, которую владелец не подтвердил в
 * банк-клиенте, висела активной вечно и оставалась кандидатом разбора выписки. Диалог обязан
 * предупредить, что черновик в самом банке остаётся, — иначе снятый у нас платёж всё равно
 * уйдёт по подтверждению.
 */

const draftId = "11111111-2222-3333-4444-555555555555";

function payment(state: string) {
  return {
    id: `tax_draft:${draftId}`,
    source: "tax_draft",
    kind: "tax_enp",
    ref_id: draftId,
    title: "Аванс УСН за I полугодие · 478 376 ₽",
    counterparty_id: null,
    counterparty_name: "Казначейство России (ФНС России)",
    amount: 478376,
    amount_paid: null,
    article_id: null,
    article_name: "Налоги — ЕНП",
    method: "bank",
    bank_channel: "tbank",
    state,
    // Как в payments_aggregator.BUCKET_BY_STATE: подготовленный ждёт отправки,
    // отправленный в банк — уже в корзине «к оплате».
    bucket: state === "in_bank" ? "to_pay" : "bank_ready",
    created_at: "2026-07-28T18:28:15+03:00",
    can_edit: state === "ready_to_send",
    can_send_to_bank: state === "ready_to_send",
    can_pay: false,
    can_cancel: true,
    extra: {
      tax: true,
      tax_kind: "usn_advance",
      purpose: "Единый налоговый платеж",
      requisites: { recipientName: "Казначейство России (ФНС России)" },
    },
  };
}

async function mockApp(page: import("@playwright/test").Page, state: string) {
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
  const row = payment(state);
  await page.route("**/api/v1/finance/payments**", (route) =>
    fulfillJson(route, {
      scope: "active",
      buckets: [
        {
          key: row.bucket,
          label: row.bucket === "to_pay" ? "Отправлен в банк" : "К отправке в банк",
          count: 1,
        },
      ],
      items: [row],
    }),
  );
  // Остальные запросы страницы для этого сценария не важны — отдаём пусто.
  await page.route(/\/api\/v1\/(settings|employees|dashboard).*/, (route) =>
    fulfillJson(route, []),
  );
}

async function openPayments(page: import("@playwright/test").Page) {
  await page.goto("/");
  await page.getByRole("button", { name: "Активные платежи" }).click();
  await expect(page.getByText("Аванс УСН за I полугодие")).toBeVisible();
}

test("платёж в банке можно снять с очереди, и диалог предупреждает про банк-клиент", async ({
  page,
}) => {
  await mockApp(page, "in_bank");

  let cancelled: string | null = null;
  await page.route(`**/api/v1/taxes/payment-drafts/${draftId}/cancel`, (route) => {
    cancelled = route.request().method();
    return fulfillJson(route, { id: draftId, status: "cancelled" });
  });

  await openPayments(page);
  await page.getByRole("button", { name: "Отменить", exact: true }).click();

  await expect(page.getByRole("heading", { name: "Отменить налоговый платёж?" })).toBeVisible();
  // Главное предупреждение: у нас платёж закрывается, в банке платёжка остаётся.
  await expect(page.getByText(/отмена здесь её не удаляет/i)).toBeVisible();

  await page.getByRole("button", { name: "Отменить платёж" }).click();
  await expect.poll(() => cancelled).toBe("POST");
});

test("подготовленный платёж показывает и отправку в банк, и отмену", async ({ page }) => {
  await mockApp(page, "ready_to_send");
  await openPayments(page);

  await expect(page.getByRole("button", { name: "В банк", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Отменить", exact: true })).toBeVisible();

  // Для неотправленного платежа предупреждения про банк-клиент быть не должно:
  // платёжки в банке ещё нет, и лишний алярм только путает.
  await page.getByRole("button", { name: "Отменить", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Отменить налоговый платёж?" })).toBeVisible();
  await expect(page.getByText(/отмена здесь её не удаляет/i)).toHaveCount(0);
});

function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}
