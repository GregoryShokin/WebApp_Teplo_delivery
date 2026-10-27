import { expect, test } from "@playwright/test";

test("подтверждение платёжки сразу показывает план и переносит воскресный срок", async ({ page }) => {
  const id = "bb7ed71c-c819-4d54-b674-73a7f1a7fd93";
  let document = {
    id, filename: "0,2 %.xls", document_type: "payment_order", status: "needs_review",
    recognition: { tax_kind: "contrib_injury", amount: 100, period_hint: "2026-10", due_date: "2026-11-15", review_reasons: ["проверить месяц"] },
    received_at: "2026-10-22T10:26:04Z", created_at: "2026-10-22T10:45:25Z",
    from_addr: "Бухгалтер", has_file: false, error: null, tax_payment_bundle_id: null as string | null,
  };
  let reviewBody: Record<string, unknown> | undefined;
  let batchPromotions = 0;
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path.endsWith("/auth/refresh")) return json({ access_token: "test-token", refresh_token: "test-refresh-token", token_type: "bearer", user: { id: "owner", email: "owner@example.com", full_name: "Владелец", roles: ["owner"] } });
    if (path.endsWith(`/taxes/documents/${id}/review`)) {
      reviewBody = route.request().postDataJSON();
      document = { ...document, status: "promoted", tax_payment_bundle_id: "plan-bundle", recognition: { ...document.recognition, due_date: String(reviewBody?.due_date), review_reasons: [] } };
      return json(document);
    }
    if (path.endsWith("/taxes/documents/promote")) { batchPromotions += 1; return json({}); }
    if (path.endsWith("/taxes/documents")) return json({ items: [document], total: 1, status_counts: { [document.status]: 1 } });
    if (path.endsWith("/taxes/overview")) return json({ detail: "В тесте проверяются документы" }, 422);
    if (path.endsWith("/taxes/calendar")) return json({ items: [], overdue_count: 0 });
    return json([]);
  });
  await page.goto("/taxes");
  await page.getByRole("tab", { name: /Документы/ }).click();
  await page.getByRole("button", { name: "Проверить…", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.locator("#rev-period").selectOption("2026-09");
  await dialog.locator("#rev-period").selectOption("2026-10");
  await expect(dialog.locator("#rev-due")).toHaveValue("2026-11-16");
  await dialog.getByRole("button", { name: "Подтвердить и поставить в план", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect(page.getByText("В плане", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Поставить в план", exact: true })).toBeDisabled();
  expect(reviewBody).toMatchObject({ status: "parsed", tax_kind: "contrib_injury", amount: 100, period_hint: "2026-10", due_date: "2026-11-16" });
  expect(batchPromotions).toBe(0);
});
