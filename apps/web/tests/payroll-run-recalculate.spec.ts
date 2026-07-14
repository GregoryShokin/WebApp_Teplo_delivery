import { expect, test, type Route } from "@playwright/test";

const periodId = "period-1";
const runId = "run-1";
const initialStartedAt = "2026-05-20T10:00:00+03:00";
const recalculatedStartedAt = "2026-05-26T09:45:00+03:00";

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

  await page.route(/\/api\/v1\/employees\/?(\?.*)?$/, (route) => fulfillJson(route, []));
  await page.route("**/api/v1/settings**", (route) => fulfillJson(route, []));
  await page.route(`**/api/v1/payroll/runs/${runId}/lines`, (route) => fulfillJson(route, []));
});

test("recalculates a blocked payroll run from detail header", async ({ page }) => {
  let hasRecalculated = false;
  let runRefetchesAfterRecalculate = 0;

  await page.route(`**/api/v1/payroll/runs/${runId}`, (route) => {
    if (hasRecalculated) {
      runRefetchesAfterRecalculate += 1;
    }
    return fulfillJson(
      route,
      payrollRun(hasRecalculated ? recalculatedStartedAt : initialStartedAt),
    );
  });
  await page.route("**/api/v1/payroll/runs", async (route) => {
    if (route.request().method() === "POST") {
      expect(route.request().postDataJSON()).toEqual({
        period_id: periodId,
        force_refresh: true,
      });
      hasRecalculated = true;
      return fulfillJson(route, payrollRun(recalculatedStartedAt));
    }
    return fulfillJson(route, [
      payrollRun(hasRecalculated ? recalculatedStartedAt : initialStartedAt),
    ]);
  });

  await page.goto(`/payroll/runs/${runId}`);

  await expect(page.getByText(/Невозможно финализировать: 1 блокер/)).toBeVisible();
  await page.getByRole("button", { name: "Пересчитать" }).click();

  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("Текущие линии расчёта будут пересозданы")).toBeVisible();

  const postRequest = page.waitForRequest(
    (request) => request.method() === "POST" && request.url().endsWith("/api/v1/payroll/runs"),
  );
  await dialog.getByRole("button", { name: "Пересчитать" }).click();
  await postRequest;

  await expect.poll(() => runRefetchesAfterRecalculate).toBeGreaterThan(0);
  await expect(page.getByText("Создан 26.05.2026, 09:45")).toBeVisible();
});

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
    "access-control-allow-methods": "GET, POST, OPTIONS",
    "access-control-allow-origin": "http://127.0.0.1:5174",
  };
}

function payrollRun(startedAt: string) {
  return {
    id: runId,
    period_id: periodId,
    started_at: startedAt,
    finished_at: null,
    status: "blocked",
    blocking_issues: [
      {
        type: "needs_setup",
        employee_name: "Иван Петров",
        work_date: "2026-05-20",
      },
    ],
    summary: {
      line_count: 1,
      total_payable: 0,
    },
    period: {
      id: periodId,
      period_type: "week",
      start_date: "2026-05-19",
      end_date: "2026-05-25",
      payroll_date: "2026-05-26",
      status: "open",
      finalized_at: null,
      finalized_by_user_id: null,
    },
  };
}
