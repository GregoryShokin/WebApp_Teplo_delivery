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

export type AssetCategory = {
  id: string;
  name: string;
  useful_life_months: number;
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

export type FixedAssetDetail = FixedAsset & { entries: DepreciationEntry[] };

export type CategoryTotal = {
  category_id: string | null;
  category_name: string;
  count: number;
  initial_cost: Money;
  accumulated: Money;
  residual: Money;
  monthly_amount: Money;
};

export type FixedAssetsSummary = {
  count: number;
  initial_cost: Money;
  accumulated: Money;
  residual: Money;
  monthly_amount: Money;
  last_closed_month: string | null;
  by_category: CategoryTotal[];
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
};

export type UpdateAssetPayload = Partial<Omit<CreateAssetPayload, "inventory_number">> & {
  review_status?: ReviewStatus;
  review_reason?: string | null;
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
