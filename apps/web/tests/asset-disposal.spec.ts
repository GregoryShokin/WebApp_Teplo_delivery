import { expect, test, type Route } from "@playwright/test";

// Списание объекта: третий исход разговора о состоянии и кнопка в карточке.
//
// Проверяется ровно то, чего не хватило владельцу 02.08.2026: модель верно поняла, что скамью
// украли, а нажать было не на что — предложение без суммы код показывал как «оценить не
// удалось» и оставлял одну кнопку «Оставить как есть». Развилка целиком визуальная: у утраты
// предмет разговора не деньги, а действие, и перепутанная ветка снова оставит владельца
// наедине с правильным выводом и без кнопки.

const ASSET_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
const REPORT_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd";

function fulfillJson(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

const ASSET = {
  id: ASSET_ID,
  name: "Скамья уличная",
  inventory_number: "ОС-0143",
  brand_model: null,
  condition: null,
  category_id: null,
  category_name: "Мебель",
  initial_cost: "24000.00",
  accumulated: "0.00",
  residual: "24000.00",
  monthly_amount: "400.00",
  useful_life_months: 60,
  valuation_basis: "market",
  valued_on: "2026-07-01",
  commissioned_on: "2026-07-01",
  status: "in_use",
  status_title: "В работе",
  location: null,
  location_id: null,
  location_name: null,
  source_ref: null,
  review_status: "ok",
  review_reason: null,
  note: null,
  acquisition_source: null,
  depreciating: true,
};

/** Обращение об УТРАТЕ: суммы в предложении нет, предмет разговора — списать или нет. */
const LOSS_REPORT = {
  id: REPORT_ID,
  message: "украли",
  kind: "incident",
  status: "proposed",
  cost_before: "24000.00",
  proposed_cost: null,
  proposed_useful_life_months: null,
  proposed_disposal: true,
  proposed_reason:
    "Менеджер сообщил, что скамью украли — объект физически отсутствует, поэтому его остаточная стоимость полностью утрачена.",
  confidence: "0.750",
  model: "claude-sonnet-5",
  error: null,
  created_at: "2026-08-02",
};

type MockOptions = {
  reports?: unknown[];
  disposal?: unknown;
  asset?: Record<string, unknown>;
  onDecide?: (body: unknown) => void;
  onDispose?: (body: unknown) => void;
  onCancel?: () => void;
};

async function openAsset(page: import("@playwright/test").Page, options: MockOptions = {}) {
  const reports = options.reports ?? [];
  const asset = { ...ASSET, ...(options.asset ?? {}) };

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
  // Один обработчик на весь модуль — маршруты различаются хвостом пути (см. соседний спек).
  await page.route("**/api/v1/fixed-assets**", (route) => {
    const url = new URL(route.request().url());
    const tail = url.pathname.split("/fixed-assets")[1] ?? "";
    const method = route.request().method();

    if (method === "POST" && tail.includes("/decision")) {
      options.onDecide?.(route.request().postDataJSON());
      return fulfillJson(route, { ...LOSS_REPORT, status: "applied" });
    }
    if (method === "POST" && tail.endsWith("/disposal")) {
      options.onDispose?.(route.request().postDataJSON());
      return fulfillJson(route, {
        occurred_on: "2026-08-02",
        loss_amount: "24000.00",
        reason: "украли",
        previous_status: "in_use",
        period_frozen: false,
      });
    }
    if (method === "DELETE" && tail.endsWith("/disposal")) {
      options.onCancel?.();
      return fulfillJson(route, { ...asset, status: "in_use", status_title: "В работе" });
    }
    if (tail.startsWith("/summary")) {
      return fulfillJson(route, {
        count: 1,
        initial_cost: "24000.00",
        accumulated: "0.00",
        residual: "24000.00",
        monthly_amount: "400.00",
        last_closed_month: null,
        by_category: [],
        by_location: [],
      });
    }
    if (tail.startsWith(`/${ASSET_ID}`)) {
      return fulfillJson(route, {
        ...asset,
        entries: [],
        condition_reports: reports,
        disposal: options.disposal ?? null,
      });
    }
    if (tail === "" || tail.startsWith("?")) {
      return fulfillJson(route, { items: [asset], total: 1 });
    }
    return fulfillJson(route, { items: [] });
  });

  await page.goto("/fixed-assets");
  await page.getByRole("row").filter({ hasText: "ОС-0143" }).first().click();
}

test("у утраты предлагается списать, а не переоценить", async ({ page }) => {
  const decisions: unknown[] = [];
  await openAsset(page, { reports: [LOSS_REPORT], onDecide: (body) => decisions.push(body) });
  await page.getByRole("tab", { name: /Состояние/ }).click();

  const sheet = page.getByRole("dialog").last();
  await expect(sheet.getByText(/объект физически отсутствует/)).toBeVisible();
  await expect(sheet.getByText(/Под списание · убыток от выбытия/)).toBeVisible();

  // Именно этой строки владелец и не должен больше видеть: суммы в предложении нет не потому,
  // что модель не справилась, а потому что разговор не про сумму.
  await expect(sheet.getByText(/Оценить в деньгах не удалось/)).toBeHidden();

  const apply = sheet.getByRole("button", { name: /Списать — убыток 24 000,00/ });
  await expect(apply).toBeVisible();
  const sent = page.waitForResponse("**/decision");
  await apply.click();
  await sent;

  expect(decisions).toEqual([{ accept: true }]);
});

test("списать объект можно из карточки — с датой и причиной", async ({ page }) => {
  const disposals: unknown[] = [];
  await openAsset(page, { onDispose: (body) => disposals.push(body) });

  const sheet = page.getByRole("dialog").last();
  await sheet.getByRole("button", { name: "Списать объект" }).click();

  const dialog = page.getByRole("dialog").last();
  // Цена решения — на экране ДО нажатия, а не в тосте после.
  await expect(dialog.getByText(/станет убытком от выбытия/)).toBeVisible();
  await expect(dialog.getByText("24 000,00 ₽")).toBeVisible();

  const confirm = dialog.getByRole("button", { name: "Списать" });
  // Причина обязательна: без неё в акте останется «объект исчез сам».
  await expect(confirm).toBeDisabled();

  await dialog.getByLabel("Дата выбытия").fill("2026-08-02");
  await dialog.getByLabel("Причина").fill("Скамью украли");
  const sent = page.waitForResponse("**/disposal");
  await confirm.click();
  await sent;

  expect(disposals).toEqual([{ reason: "Скамью украли", disposed_on: "2026-08-02" }]);
});

test("списанный объект показывает убыток и даёт отменить выбытие", async ({ page }) => {
  let cancelled = 0;
  await openAsset(page, {
    asset: { status: "disposed", status_title: "Списан", monthly_amount: "0.00" },
    disposal: {
      occurred_on: "2026-08-02",
      loss_amount: "24000.00",
      reason: "украли",
      previous_status: "in_use",
      period_frozen: false,
    },
    onCancel: () => {
      cancelled += 1;
    },
  });

  const sheet = page.getByRole("dialog").last();
  // Сумма проверяется внутри строки о списании: те же 24 000 ₽ стоят на карточке ещё дважды
  // (первоначальная и остаточная), и «просто найти сумму» ничего бы не доказало.
  await expect(sheet.getByText(/Объект списан 02\.08\.2026 · убыток от выбытия/)).toContainText(
    "24 000,00 ₽",
  );

  const sent = page.waitForResponse("**/disposal");
  await sheet.getByRole("button", { name: "Отменить списание" }).click();
  await sent;

  expect(cancelled).toBe(1);
});

test("в закрытом месяце выбытие не отменить — и это сказано на экране", async ({ page }) => {
  await openAsset(page, {
    asset: { status: "disposed", status_title: "Списан", monthly_amount: "0.00" },
    disposal: {
      occurred_on: "2026-07-15",
      loss_amount: "24000.00",
      reason: "украли",
      previous_status: "in_use",
      period_frozen: true,
    },
  });

  const sheet = page.getByRole("dialog").last();
  // Месяц уже перенесён в отчётность: объяснение стоит рядом с кнопкой, а не всплывает
  // тостом после бесполезного нажатия.
  await expect(sheet.getByRole("button", { name: "Отменить списание" })).toBeDisabled();
  await expect(sheet.getByText(/перенесён в отчётность/)).toBeVisible();
});
