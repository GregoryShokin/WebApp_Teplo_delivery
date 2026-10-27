import { expect, test } from "@playwright/test";

const states = [
  ["closing", "Акт / УПД"],
  ["duplicate", "Дубликат"],
  ["ignored", "Не к оплате"],
  ["excluded", "Исключён из очереди"],
  ["cancelled", "Отменён"],
];

test("история отличает состояние документа от отмены платежа", async ({ page }) => {
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const json = (body: unknown) => route.fulfill({
      status: 200, contentType: "application/json", body: JSON.stringify(body),
    });
    if (path.endsWith("/auth/refresh")) return json({
      access_token: "test-token", refresh_token: "test-refresh-token", token_type: "bearer",
      user: { id: "owner", email: "owner@example.com", full_name: "Владелец", roles: ["owner"] },
    });
    if (path.endsWith("/finance/payments")) return json({
      scope: "all", buckets: [],
      items: states.map(([state, label], index) => ({
        id: `history:${index}`, source: state === "cancelled" ? "reserve" : "invoice",
        kind: "invoice", ref_id: `document-${index}`, title: `Документ ${state}`,
        counterparty_id: null, counterparty_name: null, amount: 9000, amount_paid: null,
        article_id: null, article_name: null, method: "bank", bank_channel: "tbank",
        state, state_label: label, bucket: null, bucket_label: null,
        created_at: "2026-10-27T09:00:00Z", can_edit: false, can_send_to_bank: false,
        can_pay: false, can_cancel: false, extra: {},
      })),
    });
    return json([]);
  });
  await page.goto("/finance/payments/history");
  await expect(page.getByRole("columnheader", { name: "Добавлено", exact: true })).toBeVisible();
  for (const [state, label] of states) {
    const row = page.getByRole("row").filter({ hasText: `Документ ${state}` });
    await expect(row.getByText(label, { exact: true })).toBeVisible();
    if (state !== "cancelled") {
      await expect(row.getByText("Отменён", { exact: true })).toHaveCount(0);
      await expect(row.getByText("Т-Банк", { exact: true })).toHaveCount(0);
    }
  }
  await expect(page.getByText("Отменён", { exact: true })).toHaveCount(1);
});
