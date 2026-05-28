import { expect, test, type Route } from "@playwright/test";

const createdAt = "2026-05-28T10:00:00+03:00";

test.beforeEach(async ({ page }) => {
  await page.route("**/api/v1/payroll/config/rates**", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/payroll/config/revenue-tiers**", (route) =>
    fulfillJson(route, [
      tier("tier-1", 50000, 140000, 0.035),
      tier("tier-2", 140000, 190000, 0.045),
      tier("tier-3", 190000, 550000, 0.055),
      tier("tier-4", 550000, null, 0.065),
    ]),
  );
  await page.route("**/api/v1/payroll/config/category-coefficients**", (route) =>
    fulfillJson(route, [
      coefficient("category_1", 3),
      coefficient("category_2", 2.25),
      coefficient("category_3", 1.5),
      coefficient("intern", 0),
      coefficient("freelancer", 0),
    ]),
  );
  await page.route("**/api/v1/payroll/config/deductions**", (route) => fulfillJson(route, []));
  await page.route("**/api/v1/payroll/config/seniority-premium**", (route) =>
    fulfillJson(route, []),
  );
});

test("shows revenue formula without the old hardcoded preview", async ({ page }) => {
  await page.goto("/payroll/configuration");
  await page.getByRole("tab", { name: "Проценты от выручки" }).click();

  await expect(page.getByRole("heading", { name: "Пример расчёта" })).toHaveCount(0);
  await expect(page.getByText("Распределение процентного пула:")).toBeVisible();
  await expect(page.getByText("Процентный пул = Дневная выручка × Tier rate")).toBeVisible();
  await expect(
    page.getByText(
      "Доля сотрудника = Процентный пул × его вес ÷ сумма весов всех сотрудников смены",
    ),
  ).toBeVisible();
  await expect(page.getByText("Сотрудник Сушист")).toHaveCount(0);
  await expect(page.getByText("2 907")).toHaveCount(0);
});

function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({
    body: JSON.stringify(body),
    contentType: "application/json",
    status: 200,
  });
}

function tier(id: string, minRevenue: number, maxRevenue: number | null, ratePercent: number) {
  return {
    id,
    min_revenue: minRevenue,
    max_revenue: maxRevenue,
    rate_percent: ratePercent,
    effective_from: "2026-01-01",
    effective_to: null,
    created_at: createdAt,
  };
}

function coefficient(category: string, value: number) {
  return {
    id: `coefficient-${category}`,
    category,
    coefficient: value,
    effective_from: category === "category_2" ? "2026-05-28" : "2026-01-01",
    effective_to: null,
    created_at: createdAt,
  };
}
