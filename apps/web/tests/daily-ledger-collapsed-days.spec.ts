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

  await page.route("**/api/v1/shifts/ledger/bonuses**", (route) =>
    fulfillJson(route, ledgerBonuses()),
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
    "access-control-allow-origin": `http://127.0.0.1:${process.env.WEB_E2E_PORT ?? "5174"}`,
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

function ledgerBonuses(percent = "3150", revenue = "140000") {
  return {
    calculated_at: "2026-05-30T15:00:00Z",
    days: days.map((date) => ({
      date,
      daily_revenue: revenue,
      percent_pool: "6300",
      rate_percent: "4.5",
      status: "ready",
      has_open_shifts: date === firstDay,
      employees: [
        {
          employee_id: "employee-1",
          percent,
          shifts: [
            {
              role: "sushi",
              category: "category_1",
              hours: "9",
              coefficient: "10",
              weight: "7.5",
              percent,
            },
          ],
        },
      ],
    })),
  };
}

test("shows daily revenue and bonus with collapsed times and formula details", async ({ page }) => {
  await page.goto("/schedule/shifts-ledger");
  await page.locator("#shift-ledger-date").fill("2026-05-30");
  const bonus = page.getByTestId(`daily-ledger-bonus-employee-1-${firstDay}`);
  await expect(bonus).toContainText(/3\s?150 ₽/);
  await expect(bonus).toContainText("≈");
  await expect(bonus).toHaveAttribute("title", /коэффициент 10/);
  await expect(page.getByTestId(`daily-ledger-revenue-${firstDay}`)).toContainText(/140\s?000 ₽/);
  await expect(page.getByTestId(`daily-ledger-revenue-${firstDay}`)).toContainText("4,5%");
  await expect(page.getByTestId(`daily-ledger-open-header-${firstDay}`)).toHaveCount(0);
  await page.screenshot({ path: "test-results/shift-bonuses-preview.png", fullPage: true });
});

test("refreshes bonuses every minute without closing or rebuilding shifts", async ({ page }) => {
  await page.clock.install();
  let count = 0;
  await page.route("**/api/v1/shifts/ledger/bonuses**", (route) => {
    count += 1;
    return fulfillJson(route, count === 1 ? ledgerBonuses() : ledgerBonuses("5225", "190000"));
  });
  await page.goto("/payroll/daily-ledger");
  const bonus = page.getByTestId(`daily-ledger-bonus-employee-1-${firstDay}`);
  await expect(bonus).toContainText(/3\s?150 ₽/);
  await page.clock.fastForward(60_000);
  await expect(bonus).toContainText(/5\s?225 ₽/);
  await expect(page.getByTestId(`daily-ledger-revenue-${firstDay}`)).toContainText(/190\s?000 ₽/);
});

test("retains previous result but labels it stale when revenue refresh fails", async ({ page }) => {
  await page.goto("/payroll/daily-ledger");
  const bonus = page.getByTestId(`daily-ledger-bonus-employee-1-${firstDay}`);
  await expect(bonus).toContainText(/3\s?150 ₽/);
  await page.route("**/api/v1/shifts/ledger/bonuses**", (route) =>
    route.fulfill({
      status: 503,
      headers: corsHeaders(),
      contentType: "application/json",
      body: JSON.stringify({ detail: "iiko недоступен" }),
    }),
  );
  await page.getByRole("button", { name: "Обновить премии", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Показан предыдущий расчёт");
  await expect(bonus).toContainText(/3\s?150 ₽/);
  await expect(bonus).toHaveAttribute("title", /устаревшими/);
  await expect(page.getByText("Иван Петров", { exact: true })).toBeVisible();
});

test("does not show zero bonuses on an initial revenue failure", async ({ page }) => {
  await page.route("**/api/v1/shifts/ledger/bonuses**", (route) =>
    route.fulfill({
      status: 503,
      headers: corsHeaders(),
      contentType: "application/json",
      body: "{}",
    }),
  );
  await page.goto("/payroll/daily-ledger");
  await expect(page.getByRole("alert")).toContainText("Не удалось обновить выручку и премии");
  await expect(page.getByText("Премия: —", { exact: true }).first()).toBeVisible();
  await expect(page.getByTestId(`daily-ledger-bonus-employee-1-${firstDay}`)).toHaveCount(0);
});

test("marks the whole day for review when a participant has unresolved inputs", async ({
  page,
}) => {
  const data = ledgerBonuses();
  data.days[0].status = "needs_review";
  data.days[0].employees = [];
  await page.route("**/api/v1/shifts/ledger/bonuses**", (route) => fulfillJson(route, data));
  await page.goto("/payroll/daily-ledger");
  await expect(page.getByText("Премия: уточните смены дня", { exact: true })).toBeVisible();
  await expect(page.getByTestId(`daily-ledger-bonus-employee-1-${firstDay}`)).toHaveCount(0);
  await expect(page.getByTestId(`daily-ledger-bonus-employee-1-${days[1]}`)).toContainText(
    /3\s?150 ₽/,
  );
});
