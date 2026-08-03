import { expect, test, type Route } from "@playwright/test";

// Остаток депозита бывает дробным: удержание за смену урезается доступным заработком
// (у Молокановой на проде 719,91 ₽). Экран округлял остаток до рубля — «720 ₽», — и сумма,
// набранная по шапке диалога, упиралась в проверку баланса с сообщением
// «Сумма больше текущего баланса (720 ₽)», которое противоречило вводу.
const employeeId = "employee-1";
const balance = "719.91";

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

  await page.route("**/api/v1/deposits", (route) => fulfillJson(route, deposits()));
  await page.route("**/api/v1/deposits/scheduled-payout/settings", (route) =>
    fulfillJson(route, { enabled: false }),
  );
});

test("списание дробного остатка: точный баланс на экране и в запросе", async ({ page }) => {
  const writeoffs: unknown[] = [];
  await page.route(`**/api/v1/deposits/${employeeId}/writeoff`, (route) => {
    writeoffs.push(route.request().postDataJSON());
    return fulfillJson(route, { employee_id: employeeId, balance: "0.00" });
  });

  await page.goto("/deposits");

  const row = page.getByRole("row", { name: /Молоканова Светлана/ });
  await expect(row).toContainText("719,91");

  await row.getByRole("button", { name: "Операция" }).click();
  await page.getByRole("menuitem", { name: "Списать депозит" }).click();

  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("баланс 719,91");
  // Сумма преподставлена точным остатком — руками её набирать не нужно.
  await expect(dialog.getByLabel("Сумма ₽")).toHaveValue(balance);

  await dialog.getByLabel("Причина").fill("Уволен");
  await dialog.getByRole("button", { name: "Списать депозит" }).click();
  await page.getByRole("button", { name: "Подтвердить" }).click();

  await expect.poll(() => writeoffs).toEqual([{ amount: balance, reason: "Уволен" }]);
});

test("кнопка «Весь остаток» возвращает точную сумму после правки поля", async ({ page }) => {
  await page.goto("/deposits");

  const row = page.getByRole("row", { name: /Молоканова Светлана/ });
  await row.getByRole("button", { name: "Операция" }).click();
  await page.getByRole("menuitem", { name: "Списать депозит" }).click();

  const dialog = page.getByRole("dialog");
  const amount = dialog.getByLabel("Сумма ₽");
  await amount.fill("720");
  await dialog.getByLabel("Причина").fill("Уволен");
  await dialog.getByRole("button", { name: "Списать депозит" }).click();

  // Округлённая сумма всё ещё отклоняется, но теперь сообщение показывает точный остаток.
  await expect(dialog).toContainText("Сумма больше текущего баланса (719,91");

  await dialog.getByRole("button", { name: "Весь остаток" }).click();
  await expect(amount).toHaveValue(balance);
});

function fulfillJson(route: Route, body: unknown) {
  // API живёт на другом origin (localhost:8000), запросы идут с credentials — значит
  // allow-origin должен точно совпадать с origin страницы. Порт задаётся слотом агента
  // (WEB_E2E_PORT), поэтому берём его из самого запроса, а не прошиваем константой.
  const origin = route.request().headers()["origin"] ?? "http://127.0.0.1:5174";
  const headers = {
    "access-control-allow-credentials": "true",
    "access-control-allow-headers": "authorization, content-type",
    "access-control-allow-methods": "GET, POST, PUT, PATCH, OPTIONS",
    "access-control-allow-origin": origin,
  };

  if (route.request().method() === "OPTIONS") {
    return route.fulfill({ headers, status: 204 });
  }

  return route.fulfill({
    body: JSON.stringify(body),
    contentType: "application/json",
    headers,
    status: 200,
  });
}

function deposits() {
  return [
    {
      id: employeeId,
      full_name: "Молоканова Светлана",
      position: "Повар",
      category: "category_1",
      balance,
      initial_balance: "0.00",
      target: "20000.00",
      withholding: "2000.00",
      is_excluded: false,
      excluded_until: null,
      progress_pct: "3.60",
    },
  ];
}
