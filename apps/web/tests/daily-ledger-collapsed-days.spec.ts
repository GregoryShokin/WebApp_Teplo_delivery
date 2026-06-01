import { expect, test, type Page, type Route } from "@playwright/test";

const collapsedTimesStorageKey = "daily-ledger-times-collapsed-days";
const firstDay = "2026-05-24";
const days = [
  "2026-05-24",
  "2026-05-25",
  "2026-05-26",
  "2026-05-27",
  "2026-05-28",
  "2026-05-29",
  "2026-05-30",
];

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

  await page.route("**/api/v1/shifts/ledger/matrix**", (route) =>
    fulfillJson(route, ledgerMatrix()),
  );
});

test("keeps role visible while toggling time columns with localStorage persistence", async ({
  page,
}) => {
  await page.goto("/payroll/daily-ledger");

  await expect(page.getByTestId(`daily-ledger-role-header-${firstDay}`)).toBeVisible();
  await expect(page.getByTestId(`daily-ledger-open-header-${firstDay}`)).toHaveCount(0);
  await expect(page.getByTestId(`daily-ledger-close-header-${firstDay}`)).toHaveCount(0);
  await expect.poll(() => storedCollapsedTimes(page)).toEqual(days);

  await expect(page.getByTestId(`daily-ledger-day-toggle-${firstDay}`)).toBeVisible();
  await page.getByTestId(`daily-ledger-day-toggle-${firstDay}`).click();

  await expect(page.getByTestId(`daily-ledger-role-header-${firstDay}`)).toBeVisible();
  await expect(page.getByTestId(`daily-ledger-open-header-${firstDay}`)).toBeVisible();
  await expect(page.getByTestId(`daily-ledger-close-header-${firstDay}`)).toBeVisible();
  await expect
    .poll(() => storedCollapsedTimes(page))
    .toEqual(days.filter((date) => date !== firstDay));

  await page.reload();
  await expect(page.getByTestId(`daily-ledger-role-header-${firstDay}`)).toBeVisible();
  await expect(page.getByTestId(`daily-ledger-open-header-${firstDay}`)).toBeVisible();
  await expect(page.getByTestId(`daily-ledger-close-header-${firstDay}`)).toBeVisible();

  await page.getByTestId(`daily-ledger-day-toggle-${firstDay}`).click();
  await expect(page.getByTestId(`daily-ledger-role-header-${firstDay}`)).toBeVisible();
  await expect(page.getByTestId(`daily-ledger-open-header-${firstDay}`)).toHaveCount(0);
  await expect(page.getByTestId(`daily-ledger-close-header-${firstDay}`)).toHaveCount(0);
  await expect.poll(() => storedCollapsedTimes(page)).toEqual(days);
});

function storedCollapsedTimes(page: Page) {
  return page.evaluate((key) => {
    const value = window.localStorage.getItem(key);
    return value ? (JSON.parse(value) as string[]) : null;
  }, collapsedTimesStorageKey);
}

function fulfillJson(route: Route, body: unknown) {
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
    status: 200,
  });
}

function corsHeaders() {
  return {
    "access-control-allow-credentials": "true",
    "access-control-allow-headers": "authorization, content-type",
    "access-control-allow-methods": "GET, POST, PATCH, OPTIONS",
    "access-control-allow-origin": "http://127.0.0.1:5174",
  };
}

function ledgerMatrix() {
  return {
    selected_date: "2026-05-30",
    start_date: firstDay,
    end_date: "2026-05-30",
    days: days.map((date) => ({ date, is_today: date === "2026-05-30" })),
    employees: [
      {
        id: "employee-1",
        full_name: "Иван Петров",
        iiko_id: "iiko-1",
        days: days.map((date, index) => {
          const hasShift = index < 2;
          return {
            date,
            available_roles: [
              {
                payroll_role: "sushi",
                category: "category_1",
              },
            ],
            summary: {
              earliest_open: hasShift ? `${date}T09:00:00+03:00` : null,
              latest_close: hasShift ? `${date}T18:00:00+03:00` : null,
              shift_count: hasShift ? 1 : 0,
            },
            shifts: hasShift
              ? [
                  {
                    ledger_entry_id: `entry-${date}`,
                    opened_at: `${date}T09:00:00+03:00`,
                    closed_at: `${date}T18:00:00+03:00`,
                    payroll_role: "sushi",
                    category: "category_1",
                    is_resolved: true,
                    status: "resolved",
                  },
                ]
              : [],
          };
        }),
      },
    ],
  };
}
