import { expect, test, type Page, type Route } from "@playwright/test";

const employeeId = "employee-1";
const pendingAssignmentId = "assignment-pending";
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
  await page.route("**/api/v1/employees/changes**", (route) => fulfillJson(route, []));
  await page.route(/\/api\/v1\/employees\/?(\?.*)?$/, (route) =>
    fulfillJson(route, employees()),
  );
});

test("closes pending assignment delete confirmation by cancel", async ({ page }) => {
  await page.goto("/staff");
  await openPendingAssignmentDialog(page);

  await page.getByRole("button", { name: "Удалить запланированное" }).click();
  const confirm = page.getByRole("dialog", { name: "Удалить запланированное изменение?" });
  await expect(confirm).toBeVisible();

  await confirm.getByRole("button", { name: "Отмена" }).click();

  await expect(confirm).toBeHidden();
  await expect(page.getByRole("dialog", { name: "Запланированное изменение" })).toBeVisible();
});

test("closes pending assignment delete confirmation after server error", async ({ page }) => {
  await page.route(
    `**/api/v1/employees/${employeeId}/assignments/${pendingAssignmentId}`,
    (route) =>
      fulfillJson(
        route,
        {
          detail: "Эта роль — единственная основная. Сначала назначьте другую основную роль.",
        },
        409,
      ),
  );

  await page.goto("/staff");
  await openPendingAssignmentDialog(page);

  await page.getByRole("button", { name: "Удалить запланированное" }).click();
  const confirm = page.getByRole("dialog", { name: "Удалить запланированное изменение?" });
  await confirm.getByRole("button", { name: "Удалить" }).click();

  await expect(confirm).toBeHidden();
  await expect(
    page.getByText("Эта роль — единственная основная. Сначала назначьте другую основную роль."),
  ).toBeVisible();
});

async function openPendingAssignmentDialog(page: Page) {
  await page.getByLabel(/Запланировано: с .* категория сменится/).click();
  await expect(page.getByRole("dialog", { name: "Запланированное изменение" })).toBeVisible();
}

function fulfillJson(route: Route, body: unknown, status = 200) {
  if (route.request().method() === "OPTIONS") {
    return route.fulfill({
      headers: corsHeaders(),
      status: 204,
    });
  }

  return route.fulfill({
    body: JSON.stringify(body),
    contentType: "application/json",
    headers: corsHeaders(),
    status,
  });
}

function corsHeaders() {
  return {
    "access-control-allow-credentials": "true",
    "access-control-allow-headers": "authorization, content-type",
    "access-control-allow-methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
    "access-control-allow-origin": "http://127.0.0.1:5174",
  };
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
          id: "assignment-active",
          employee_id: employeeId,
          payroll_role: "sushi",
          category: "category_1",
          is_primary: true,
          effective_from: "2026-01-01",
          effective_to: "2099-01-01",
          is_pending: false,
          created_at: createdAt,
          updated_at: createdAt,
        },
        {
          id: pendingAssignmentId,
          employee_id: employeeId,
          payroll_role: "sushi",
          category: "category_2",
          is_primary: true,
          effective_from: "2099-01-01",
          effective_to: null,
          is_pending: true,
          created_at: createdAt,
          updated_at: createdAt,
        },
      ],
      active_notice: null,
    },
  ];
}
