import { expect, test, type Page } from "@playwright/test";

test.use({ channel: "chrome" });

async function setup(page: Page) {
  let amount = 7500;
  let deferred = 0;
  let outstanding = 7500;
  let version = "initial";
  const edits: Record<string, unknown>[] = [];
  const payouts: Record<string, unknown>[] = [];
  const plan = () => ({
    reserve_id: "reserve",
    version,
    outstanding,
    allocations: [{ employee_id: "sofia", amount, deferred }],
    transferred: 0,
  });
  await page.route("**/api/v1/**", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    let body: unknown = [];
    if (path.endsWith("/auth/refresh"))
      body = {
        access_token: "test-token",
        refresh_token: "test-refresh",
        token_type: "bearer",
        user: { id: "owner", email: "owner@example.com", full_name: "Владелец", roles: ["owner"] },
      };
    if (path.endsWith("/finance/payments"))
      body = {
        scope: "active",
        buckets: [{ key: "reserved_kassa", label: "В кассе", count: 1 }],
        items: [
          {
            id: "reserve",
            source: "reserve",
            kind: "payroll_reserve",
            ref_id: "reserve",
            title: "Выплата зарплаты",
            amount: outstanding,
            amount_paid: 0,
            method: "cash",
            state: "reserved",
            state_label: "В резерве",
            bucket: "reserved_kassa",
            bucket_label: "В кассе",
            created_at: "2026-10-06T10:00:00Z",
            can_pay: true,
            can_edit: false,
            can_send_to_bank: false,
            can_cancel: false,
            extra: { run_id: "run", location: "kassa" },
          },
        ],
      };
    if (/\/employees\/?$/.test(path)) body = [{ id: "sofia", full_name: "София Колесникова" }];
    if (path.endsWith("/payroll/runs/run/lines"))
      body = [
        {
          id: "line",
          employee_id: "sofia",
          total_payable: 7500,
          payment_status: "pending",
          paid_amount: 0,
          deposit_payout_scheduled: 0,
        },
      ];
    if (path.endsWith("/payroll/runs/run/solvency")) body = { solvent: true };
    if (path.endsWith("/payroll/reserves/reserve/plan")) {
      if (req.method() === "PUT") {
        const edit = req.postDataJSON();
        edits.push(edit);
        const remainder = amount - edit.amount;
        amount = edit.amount;
        if (edit.remainder_destination) outstanding -= remainder;
        else deferred += remainder;
        version = `saved-${edits.length}`;
        body = { ...plan(), transferred: edit.remainder_destination ? remainder : 0 };
      } else body = plan();
    }
    if (path.endsWith("/payout") || path.endsWith("/pay-employee")) {
      payouts.push(req.postDataJSON());
      body = {
        reserve_id: "reserve",
        primary_booked: amount,
        overflow_booked: 0,
        employees_paid: 1,
      };
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });
  await page.goto("/finance/payments/history");
  await page.getByRole("button", { name: "Активные платежи", exact: true }).click();
  await page.getByRole("button", { name: "Выплатить ЗП", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Выплата ЗП из кассы" })).toBeVisible();
  return { edits, payouts };
}

async function editAmount(page: Page, value: string, enter = false) {
  await page.getByRole("button", { name: "Изменить сумму", exact: true }).click();
  const input = page
    .getByRole("dialog")
    .filter({ has: page.getByRole("heading", { name: "Выплата ЗП из кассы" }) })
    .getByRole("textbox");
  await input.fill(value);
  if (enter) await input.press("Enter");
  else await page.getByRole("button", { name: "Сохранить сумму", exact: true }).click();
}

test("checkmark edits unpaid plan; remainder remains reserved and survives reopening", async ({
  page,
}) => {
  const { edits, payouts } = await setup(page);
  await editAmount(page, "7000");
  await expect(page.getByRole("heading", { name: "Откуда выплатить остаток?" })).toBeVisible();
  expect(edits).toHaveLength(0);
  expect(payouts).toHaveLength(0);
  await page.getByRole("button", { name: "Оставить на этом счёте" }).click();
  await expect(page.getByText("Ещё 500 ₽ оставлены здесь в резерве")).toBeVisible();
  await expect(page.getByRole("row", { name: /София Колесникова/ })).toContainText("7 000 ₽");
  expect(edits[0]).toMatchObject({
    amount: 7000,
    expected_version: "initial",
    remainder_destination: null,
  });
  expect(payouts).toHaveLength(0);
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Выплатить ЗП", exact: true }).click();
  await expect(page.getByText("Ещё 500 ₽ оставлены здесь в резерве")).toBeVisible();
  expect(payouts).toHaveLength(0);
});

test("Enter cannot pay; moving remainder selects only the other account", async ({ page }) => {
  const { edits, payouts } = await setup(page);
  await editAmount(page, "7000", true);
  const remainder = page
    .getByRole("dialog")
    .filter({ has: page.getByRole("heading", { name: "Откуда выплатить остаток?" }) });
  await expect(remainder).toContainText("Остаток 500 ₽");
  await expect(remainder.getByRole("button", { name: /Отменить/ })).toHaveCount(0);
  await remainder.getByRole("button", { name: "Перенести 500 ₽ на Сейф" }).click();
  await expect(page.getByRole("row", { name: /София Колесникова/ })).toContainText("7 000 ₽");
  expect(edits[0]).toMatchObject({ amount: 7000, remainder_destination: "safe" });
  expect(payouts).toHaveLength(0);
});

test("pay is a separate confirmed action using the saved plan version", async ({ page }) => {
  const { payouts } = await setup(page);
  await editAmount(page, "7000");
  await page.getByRole("button", { name: "Оставить на этом счёте" }).click();
  await expect(page.getByText("Ещё 500 ₽ оставлены здесь в резерве")).toBeVisible();
  await page.getByRole("button", { name: "Выплатить", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Подтвердить выдачу зарплаты?" })).toContainText(
    "7 000 ₽",
  );
  expect(payouts).toHaveLength(0);
  await page.getByRole("button", { name: "Подтвердить выплату" }).click();
  await expect.poll(() => payouts.length).toBe(1);
  expect(payouts[0]).toMatchObject({
    plan_version: "saved-1",
    allow_overflow: false,
    selected_ids: ["sofia"],
  });
  await expect(page.getByRole("heading", { name: "Подтвердить выдачу зарплаты?" })).toHaveCount(0);
});

test("canceling edit and remainder choice writes nothing", async ({ page }) => {
  const { edits, payouts } = await setup(page);
  await editAmount(page, "7000");
  await page.getByRole("button", { name: "Назад к сумме" }).click();
  await page.getByRole("button", { name: "Отмена", exact: true }).click();
  await expect(page.getByRole("row", { name: /София Колесникова/ })).toContainText("7 500 ₽");
  expect(edits).toHaveLength(0);
  expect(payouts).toHaveLength(0);
});
