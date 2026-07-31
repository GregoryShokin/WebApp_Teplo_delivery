import { expect, test, type Route } from "@playwright/test";

// Предложение модели по б/у покупке: разговор про СРОК, а не про деньги.
//
// Развилка тонкая и целиком визуальная: одна и та же карточка обращения рисует у поломки
// стоимость, а у покупки — остаток срока службы. Перепутанная ветка предложит владельцу
// «применить» не то, что применится, и заметить это можно будет только по изменившейся не той
// цифре в балансе.

const ASSET_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
const PURCHASE_REPORT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";
const INCIDENT_REPORT_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc";

function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

const ASSET = {
  id: ASSET_ID,
  name: "Пароконвектомат Rational SCC WE 101",
  inventory_number: "ОС-0153",
  brand_model: "Rational SCC WE 101",
  condition: "used",
  category_id: null,
  category_name: "Тепловое оборудование",
  initial_cost: "180000.00",
  accumulated: "0.00",
  residual: "180000.00",
  monthly_amount: "2142.86",
  useful_life_months: 84,
  valuation_basis: "payment",
  valued_on: "2026-07-31",
  commissioned_on: "2026-07-31",
  status: "in_use",
  status_title: "В работе",
  location: null,
  location_id: null,
  location_name: null,
  source_ref: null,
  review_status: "ok",
  review_reason: null,
  note: "Куплен б/у. Состояние при покупке: 2018 года",
  depreciating: true,
};

/** Обращение о ПОКУПКЕ: предложен срок, стоимость не тронута. */
const PURCHASE_REPORT = {
  id: PURCHASE_REPORT_ID,
  message: "Куплен б/у. Состояние со слов покупателя: 2018 года, дверь не закрывается плотно",
  kind: "purchase",
  status: "proposed",
  cost_before: "180000.00",
  proposed_cost: null,
  proposed_useful_life_months: 21,
  proposed_reason: "Объект 2018 года, к моменту покупки отработал около восьми лет.",
  confidence: "0.700",
  model: "claude-sonnet-5",
  error: null,
  created_at: "2026-07-31",
};

/** Обращение о ПОЛОМКЕ у того же объекта: предметом разговора остаются деньги. */
const INCIDENT_REPORT = {
  ...PURCHASE_REPORT,
  id: INCIDENT_REPORT_ID,
  message: "Отказал компрессор, холод не держит",
  kind: "incident",
  proposed_cost: "36000.00",
  proposed_useful_life_months: null,
  proposed_reason: "Без компрессора объект стоит как железо.",
};

async function openConditionTab(
  page: import("@playwright/test").Page,
  reports: unknown[],
  onDecide?: (body: unknown) => void,
) {
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
  await page.route("**/api/v1/settings**", (route) => fulfillJson(route, []));
  // Один обработчик на весь модуль: маршруты различаются хвостом пути, и разводить их
  // отдельными масками — значит зависеть от порядка регистрации в playwright.
  await page.route("**/api/v1/fixed-assets**", (route) => {
    const url = new URL(route.request().url());
    const tail = url.pathname.split("/fixed-assets")[1] ?? "";

    if (route.request().method() === "POST" && tail.includes("/decision")) {
      onDecide?.(route.request().postDataJSON());
      return fulfillJson(route, reports[0]);
    }
    if (tail.startsWith("/summary")) {
      return fulfillJson(route, {
        count: 1,
        initial_cost: "180000.00",
        accumulated: "0.00",
        residual: "180000.00",
        monthly_amount: "2142.86",
        last_closed_month: null,
        by_category: [],
        by_location: [],
      });
    }
    if (tail.startsWith(`/${ASSET_ID}`)) {
      return fulfillJson(route, { ...ASSET, entries: [], condition_reports: reports });
    }
    if (tail === "" || tail.startsWith("?")) {
      return fulfillJson(route, { items: [ASSET], total: 1 });
    }
    return fulfillJson(route, { items: [] });
  });

  await page.goto("/fixed-assets");
  await page.getByRole("row").filter({ hasText: "ОС-0153" }).first().click();
  await page.getByRole("tab", { name: /Состояние/ }).click();
}

test("у покупки б/у предлагается срок службы, а не скидка с цены", async ({ page }) => {
  const decisions: unknown[] = [];
  await openConditionTab(page, [PURCHASE_REPORT], (body) => decisions.push(body));

  const sheet = page.getByRole("dialog").last();
  await expect(sheet.getByText(/Объект 2018 года/)).toBeVisible();

  // Срок: было 84 из категории, стало 21. Денег в предложении нет вовсе — цена б/у объекта
  // износ уже содержит, и скидка посчитала бы его дважды.
  await expect(sheet.getByText("Срок службы: 84 → 21 мес")).toBeVisible();
  await expect(sheet.getByText("(1 год 9 месяцев)")).toBeVisible();
  await expect(sheet.getByText("180 000,00 ₽ →")).toBeHidden();

  const apply = sheet.getByRole("button", { name: "Поставить срок 21 мес" });
  const sent = page.waitForResponse("**/decision");
  await apply.click();
  await sent;

  expect(decisions).toEqual([{ accept: true }]);
});

test("у поломки предмет разговора остаётся стоимостью", async ({ page }) => {
  await openConditionTab(page, [INCIDENT_REPORT]);

  const sheet = page.getByRole("dialog").last();
  await expect(sheet.getByText("180 000,00 ₽ →")).toBeVisible();
  await expect(sheet.getByRole("button", { name: /Применить 36 000,00/ })).toBeVisible();
  // Срок при поломке не обсуждается: у объекта меняется цена, а не остаток жизни.
  await expect(sheet.getByText(/Срок службы: /)).toBeHidden();
});

test("оценить нечем — предложения нет, но обращение видно", async ({ page }) => {
  await openConditionTab(page, [
    {
      ...PURCHASE_REPORT,
      proposed_useful_life_months: null,
      proposed_reason: "Из описания не понять, сколько объект уже отработал",
    },
  ]);

  const sheet = page.getByRole("dialog").last();
  // Молчание было бы хуже: владелец не узнал бы, что объект б/у, и карточка осталась бы
  // «как новая» со сроком из категории.
  await expect(sheet.getByText(/Из описания не понять/)).toBeVisible();
  await expect(sheet.getByText(/поставьте срок сами/)).toBeVisible();
  await expect(sheet.getByRole("button", { name: /Поставить срок/ })).toBeHidden();
  await expect(sheet.getByRole("button", { name: "Оставить как есть" })).toBeVisible();
});
