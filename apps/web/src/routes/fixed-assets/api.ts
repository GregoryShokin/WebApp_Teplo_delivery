/** Контракт страницы «Учёт ОС» — запросы к `/api/v1/fixed-assets/*`.
 *
 * Типы зеркалят схемы из `app/api/v1/routes/fixed_assets.py` один в один.
 *
 * Деньги приходят из pydantic-`Decimal`, а он сериализуется СТРОКОЙ. Поэтому денежный тип —
 * `Money = number | string`, а приведение живёт в одном месте (`toNumber` в index.tsx):
 * арифметика по нетипизированному ответу молча склеила бы строки.
 *
 * Считать остаточную стоимость, накопленную амортизацию и «идёт ли амортизация» на фронте
 * НЕЛЬЗЯ — бэкенд отдаёт `residual`, `accumulated`, `monthly_amount`, `depreciating` и
 * `status_title` готовыми, чтобы список, карточка и свод судили одинаково.
 */
import { api } from "@/lib/api";

const BASE = "/fixed-assets";

export type Money = number | string;

export type AssetStatus = "in_use" | "in_storage" | "not_working" | "disposed" | "sold";
export type ValuationBasis = "market" | "payment";
export type ReviewStatus = "ok" | "requires_owner_review";

/** Откуда объект взялся у бизнеса — правая сторона баланса.
 *
 * `purchase` встречной записи не создаёт: деньги ушли, объект пришёл, итог тот же. Остальные
 * три означают, что актив вырос, а деньги не тратились, — и в пассиве обязана появиться
 * запись на ту же сумму. `null` — «не указано»: так стоят карточки описи 2026. */
export type AcquisitionSource = "purchase" | "owner_contribution" | "owner_loan" | "donation";

export type AssetCategory = {
  id: string;
  name: string;
  useful_life_months: number;
  /** Какие поля спрашивать при заведении: техника — марка и модель, мебель — материал и
   *  размеры. Здесь не используется, но объявлено — этот файл зеркалит схему один в один. */
  spec_profile: "equipment" | "furniture" | "other";
  note: string | null;
};

export type FixedAsset = {
  id: string;
  name: string;
  inventory_number: string | null;
  brand_model: string | null;
  category_id: string | null;
  category_name: string | null;
  initial_cost: Money;
  accumulated: Money;
  residual: Money;
  monthly_amount: Money;
  useful_life_months: number | null;
  valuation_basis: ValuationBasis;
  valued_on: string | null;
  commissioned_on: string | null;
  status: AssetStatus;
  status_title: string;
  location: string | null;
  location_id: string | null;
  location_name: string | null;
  source_ref: string | null;
  review_status: ReviewStatus;
  review_reason: string | null;
  note: string | null;
  acquisition_source: AcquisitionSource | null;
  depreciating: boolean;
};

export type DepreciationEntry = {
  period_month: string;
  amount: Money;
  residual_after: Money;
  is_manual: boolean;
  corrected_at: string | null;
  note: string | null;
};

export type ConditionStatus = "pending" | "proposed" | "applied" | "dismissed" | "failed";

/** О чём обращение: поломка у работающего объекта или покупка б/у. */
export type ConditionReportKind = "purchase" | "incident";

/** Сообщение о состоянии объекта и предложение модели по нему.
 *
 * У поломки (`incident`) предмет разговора — СТОИМОСТЬ, у покупки б/у (`purchase`) — остаток
 * СРОКА службы: цена б/у объекта износ уже содержит, а срок из категории считает его новым.
 *
 * Оба предложения бывают пустыми: модель не смогла связать сообщение со стоимостью или не
 * поняла из описания, сколько объект отработал. Запись всё равно доходит до владельца —
 * свидетельство ценно само по себе.
 */
export type ConditionReport = {
  id: string;
  message: string;
  kind: ConditionReportKind;
  status: ConditionStatus;
  cost_before: Money;
  proposed_cost: Money | null;
  proposed_useful_life_months: number | null;
  proposed_reason: string | null;
  confidence: Money | null;
  model: string | null;
  error: string | null;
  created_at: string;
};

export type FixedAssetDetail = FixedAsset & {
  entries: DepreciationEntry[];
  condition_reports: ConditionReport[];
};

export type CategoryTotal = {
  category_id: string | null;
  category_name: string;
  count: number;
  initial_cost: Money;
  accumulated: Money;
  residual: Money;
  monthly_amount: Money;
};

export type LocationTotal = {
  location_id: string | null;
  location_name: string;
  /** 'point' — торговая точка, 'warehouse' — склад, 'office' — офис. */
  kind: string | null;
  count: number;
  initial_cost: Money;
  residual: Money;
  monthly_amount: Money;
  /** Числятся «в работе», хотя стоят не на торговой точке — деньги, не делающие выручку. */
  idle_count: number;
  idle_residual: Money;
};

export type FixedAssetsSummary = {
  count: number;
  initial_cost: Money;
  accumulated: Money;
  residual: Money;
  monthly_amount: Money;
  last_closed_month: string | null;
  by_category: CategoryTotal[];
  by_location: LocationTotal[];
};

export type AssetFilters = {
  search?: string;
  status?: AssetStatus;
  category_id?: string;
  location_id?: string;
};

export type CreateAssetPayload = {
  name: string;
  initial_cost: string;
  category_id?: string | null;
  brand_model?: string | null;
  inventory_number?: string | null;
  valuation_basis?: ValuationBasis;
  valued_on?: string | null;
  commissioned_on?: string | null;
  useful_life_months?: number | null;
  status?: AssetStatus;
  location_id?: string | null;
  source_ref?: string | null;
  note?: string | null;
  acquisition_source?: AcquisitionSource | null;
};

export type UpdateAssetPayload = Partial<Omit<CreateAssetPayload, "inventory_number">> & {
  review_status?: ReviewStatus;
  review_reason?: string | null;
};

export type BalanceLine = {
  line_name: string;
  asset_count: number;
  initial_cost: Money;
  accumulated: Money;
  residual: Money;
  depreciation: Money;
};

/** Прошлое сдвинули после заморозки месяца: переоценка, коррекция или правка карточки. */
export type LineDrift = {
  line_name: string;
  field: string;
  snapshot_value: Money;
  current_value: Money;
};

export type Reporting = {
  period_month: string;
  lines: BalanceLine[];
  residual_total: Money;
  depreciation_total: Money;
  is_frozen: boolean;
  drift: LineDrift[];
  series: Array<{ period_month: string; amount: Money }>;
};

export async function getReporting(periodMonth?: string): Promise<Reporting> {
  const response = await api.get<Reporting>(`${BASE}/reporting`, {
    params: { period_month: periodMonth || undefined },
  });
  return response.data;
}

export type UnlinkedPayment = {
  transaction_id: string;
  operation_date: string;
  amount: Money;
  article_name: string;
  article_link_kind: string;
  payment_purpose: string | null;
};

export type MonthCloseResult = {
  period_month: string;
  entries: number;
  amount: Money;
};

/** Реестр карточек. Путь БЕЗ хвостового слэша — на бэкенде `@router.get("")`. */
export async function getFixedAssets(filters: AssetFilters): Promise<{
  items: FixedAsset[];
  total: number;
}> {
  const response = await api.get<{ items: FixedAsset[]; total: number }>(BASE, {
    // Пустое значение фильтра не должно уходить в query: axios выкидывает undefined, а
    // пустую строку отправит — и бэкенд вернёт 422.
    params: {
      search: filters.search || undefined,
      status: filters.status || undefined,
      category_id: filters.category_id || undefined,
      location_id: filters.location_id || undefined,
    },
  });
  return response.data;
}

export async function getFixedAssetsSummary(): Promise<FixedAssetsSummary> {
  const response = await api.get<FixedAssetsSummary>(`${BASE}/summary`);
  return response.data;
}

export async function getAssetCategories(): Promise<AssetCategory[]> {
  const response = await api.get<{ items: AssetCategory[] }>(`${BASE}/categories`);
  return response.data.items;
}

export async function getFixedAsset(assetId: string): Promise<FixedAssetDetail> {
  const response = await api.get<FixedAssetDetail>(`${BASE}/${assetId}`);
  return response.data;
}

export async function createFixedAsset(payload: CreateAssetPayload): Promise<FixedAsset> {
  const response = await api.post<FixedAsset>(BASE, payload);
  return response.data;
}

export async function updateFixedAsset(
  assetId: string,
  payload: UpdateAssetPayload,
): Promise<FixedAsset> {
  const response = await api.patch<FixedAsset>(`${BASE}/${assetId}`, payload);
  return response.data;
}

export async function correctDepreciation(
  assetId: string,
  payload: { period_month: string; amount: string; note?: string | null },
): Promise<DepreciationEntry> {
  const response = await api.patch<DepreciationEntry>(`${BASE}/${assetId}/depreciation`, payload);
  return response.data;
}

/** Платежи по статьям ОС, за которыми не стоит ни один объект.
 *
 * Сеть-ловушка: жёсткий гард стоит там, где человек может указать объект, а статья попадает на
 * проводку из шести десятков мест. Каждая строка списка — покупка или ремонт, ушедшие мимо
 * баланса.
 */
export async function getUnlinkedPayments(since?: string): Promise<{
  items: UnlinkedPayment[];
  total: number;
}> {
  const response = await api.get<{ items: UnlinkedPayment[]; total: number }>(
    `${BASE}/unlinked-payments`,
    { params: { since: since || undefined } },
  );
  return response.data;
}

/** Менеджер пишет, что случилось с объектом. Оценку даст модель — в фоне.
 *
 * Отвечает 202: вызов модели идёт до двух минут, держать на нём запрос нельзя. Стоимость сама
 * не изменится ни при каком ответе — предложение ждёт решения владельца.
 */
export async function reportCondition(assetId: string, message: string): Promise<ConditionReport> {
  const response = await api.post<ConditionReport>(`${BASE}/${assetId}/condition`, { message });
  return response.data;
}

/** Решение владельца по предложению модели: применить или отклонить. */
export async function decideCondition(
  assetId: string,
  reportId: string,
  accept: boolean,
): Promise<ConditionReport> {
  const response = await api.post<ConditionReport>(
    `${BASE}/${assetId}/condition/${reportId}/decision`,
    { accept },
  );
  return response.data;
}

/** Закрыть месяц вручную.
 *
 * Свой таймаут: прогон идёт по всем карточкам реестра (их 149) и пишет запись в журнал —
 * в дефолтные 15 секунд может не уложиться. Ложная ошибка при успешном закрытии опаснее
 * ожидания: владелец нажмёт кнопку второй раз.
 */
export async function closeDepreciationMonth(periodMonth: string): Promise<MonthCloseResult> {
  const response = await api.post<MonthCloseResult>(
    `${BASE}/close-month`,
    { period_month: periodMonth },
    { timeout: 120_000 },
  );
  return response.data;
}
