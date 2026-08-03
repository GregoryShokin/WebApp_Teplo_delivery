import { expect, test, type Route } from "@playwright/test";

const employeeId = "employee-1";
const createdAt = "2026-05-28T10:00:00+03:00";

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

  await page.route("**/api/v1/settings**", (route) => {
    if (route.request().method() === "PUT") {
      return fulfillJson(route, { key: "updated", value: route.request().postDataJSON().value });
    }
    return fulfillJson(route, settings());
  });
  await page.route("**/api/v1/deposits", (route) => fulfillJson(route, deposits()));
  await page.route(`**/api/v1/deposits/${employeeId}/transactions`, (route) =>
    fulfillJson(route, [
      {
        id: "tx-1",
        employee_id: employeeId,
        run_id: null,
        transaction_type: "accrual",
        amount: "5000.00",
        created_at: createdAt,
      },
    ]),
  );
  await page.route("**/api/v1/payroll/config/rates**", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/payroll/config/revenue-tiers**", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/payroll/config/category-coefficients**", (route) =>
    fulfillJson(route, []),
  );
  await page.route("**/api/v1/payroll/config/deductions**", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/payroll/config/seniority-premium**", (route) =>
    fulfillJson(route, []),
  );
  await page.route("**/api/v1/employees/changes**", (route) => fulfillJson(route, []));
  await page.route(/\/api\/v1\/employees\/?(\?.*)?$/, (route) => fulfillJson(route, employees()));
});

test("renders deposits tab with category defaults", async ({ page }) => {
  await page.goto("/payroll/configuration");
  await page.getByRole("tab", { name: "Депозиты" }).click();

  await expect(page.getByRole("heading", { name: "Общие правила удержания" })).toBeVisible();
  await expect(
    page
      .getByRole("row", { name: /category_1/ })
      .locator("input")
      .first(),
  ).toHaveValue("20000");
  await expect(
    page
      .getByRole("row", { name: /category_2/ })
      .locator("input")
      .first(),
  ).toHaveValue("15000");
});

test("opens individual deposit dialog from staff card", async ({ page }) => {
  await page.goto("/staff");
  await page.getByRole("button", { name: /Иван Петров/ }).click();
  await page.getByRole("button", { name: "Индивидуальный депозит" }).click();

  const dialog = page.getByRole("dialog", { name: "Индивидуальный депозит" });
  await expect(dialog).toHaveAttribute("data-state", "open");
  await expect(dialog).toHaveClass(/stage-manager-dialog/);
});

test("sends target override patch from individual deposit dialog", async ({ page }) => {
  const patches: unknown[] = [];
  await page.route(`**/api/v1/deposits/${employeeId}/config`, (route) => {
    patches.push(route.request().postDataJSON());
    return fulfillJson(route, {
      employee_id: employeeId,
      deposit_target_override: "22000.00",
      deposit_withholding_override: null,
      deposit_excluded: false,
      deposit_excluded_until: null,
      deposit_excluded_reason: null,
    });
  });

  await page.goto("/staff");
  await page.getByRole("button", { name: /Иван Петров/ }).click();
  await page.getByRole("button", { name: "Индивидуальный депозит" }).click();

  const dialog = page.getByRole("dialog", { name: "Индивидуальный депозит" });
  await dialog.getByLabel("Индивидуальная цель депозита").fill("22000");
  await dialog.getByRole("button", { name: "Сохранить" }).click();

  await expect.poll(() => patches).toEqual([{ deposit_target_override: "22000" }]);
});

function fulfillJson(route: Route, body: unknown) {
  if (route.request().method() === "OPTIONS") {
    return route.fulfill({
      headers: corsHeaders(route),
      status: 204,
    });
  }

  return route.fulfill({
    body: JSON.stringify(body),
    contentType: "application/json",
    headers: corsHeaders(route),
    status: 200,
  });
}

function corsHeaders(route: Route) {
  // Запросы идут с credentials на другой origin (localhost:8000), поэтому allow-origin
  // должен совпадать с origin страницы. Порт задаётся слотом агента (WEB_E2E_PORT) —
  // прошитый 5174 ронял весь файл в чужом слоте на экран логина.
  return {
    "access-control-allow-credentials": "true",
    "access-control-allow-headers": "authorization, content-type",
    "access-control-allow-methods": "GET, POST, PUT, PATCH, OPTIONS",
    "access-control-allow-origin": route.request().headers()["origin"] ?? "http://127.0.0.1:5174",
  };
}

function settings() {
  return [
    {
      id: "setting-auto",
      key: "payroll.deposit_auto_withholding_enabled",
      value: true,
    },
    {
      id: "setting-rules",
      key: "payroll.category_rules",
      value: {
        "1": { coeff: 10, deposit_target: 20000, deposit_withholding: 1000 },
        "2": { coeff: 7.5, deposit_target: 15000, deposit_withholding: 1000 },
        "3": { coeff: 5, deposit_target: 10000, deposit_withholding: 1000 },
        "4": { coeff: 0, deposit_target: 7000, deposit_withholding: 1000 },
      },
    },
  ];
}

function deposits() {
  return [
    {
      id: employeeId,
      full_name: "Иван Петров",
      position: "Повар",
      category: "category_1",
      balance: "5000.00",
      initial_balance: "2000.00",
      target: "20000.00",
      withholding: "1000.00",
      is_excluded: false,
      excluded_until: null,
      progress_pct: "25.00",
    },
  ];
}

function employees() {
  return [
    {
      id: employeeId,
      full_name: "Иван Петров",
      iiko_id: "iiko-1",
      position: "Повар",
      category: "category_1",
      default_cooking_station: "sushi",
      is_senior: false,
      is_deputy_senior: false,
      status: "active",
      hire_date: null,
      fire_date: null,
      fire_reason: null,
      pin_assumed_from_iiko: false,
      pin_set_at: null,
      iiko_sync_at: createdAt,
      created_at: createdAt,
      updated_at: createdAt,
      assignments: [
        {
          id: "assignment-1",
          employee_id: employeeId,
          payroll_role: "sushi",
          category: "category_1",
          is_primary: true,
          effective_from: "2026-01-01",
          effective_to: null,
          created_at: createdAt,
          updated_at: createdAt,
        },
      ],
    },
  ];
}
