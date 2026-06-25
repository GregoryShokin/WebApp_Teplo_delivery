import { api } from "@/lib/api";

const BASE = "/kassa";

// --- справочники ---------------------------------------------------------------

export type KassaDdsArticle = {
  id: string;
  code: string;
  name: string;
  movement_type: string;
  activity_type: string;
};

export type KassaAccount = {
  id: string;
  code: string;
  name: string;
  type: string;
};

export type KassaCounterparty = {
  id: string;
  name: string;
  inn: string | null;
  type: string;
};

export async function getKassaExpenseArticles(): Promise<KassaDdsArticle[]> {
  const response = await api.get<KassaDdsArticle[]>(`${BASE}/dds-articles`, {
    params: { movement_type: "outflow" },
  });
  return response.data;
}

export async function getKassaCounterparties(search?: string): Promise<KassaCounterparty[]> {
  const response = await api.get<KassaCounterparty[]>(`${BASE}/counterparties`, {
    params: search ? { search } : undefined,
  });
  return response.data;
}

// Фиче-флаги Кассы для UI (доступны кассиру по kassa.refs.read).
export type KassaConfig = {
  manual_pending_cheque_enabled: boolean;
};

export async function getKassaConfig(): Promise<KassaConfig> {
  const response = await api.get<KassaConfig>(`${BASE}/config`);
  return response.data;
}

// --- чеки (оплата картой) ------------------------------------------------------

export type CardTransaction = {
  bank_operation_id: string;
  operation_date: string;
  posted_at: string | null;
  purchased_at: string | null;
  amount: number;
  counterparty_name_raw: string | null;
  purpose: string | null;
  tier: number | null;
  minutes_delta: number | null;
};

export type ChequeAllocation = {
  id: string;
  source_kind: string;
  bank_operation_id: string | null;
  cashflow_transaction_id: string | null;
  amount: number;
};

export type ChequeLine = {
  id: string;
  name: string;
  article: string | null;
  unit: string | null;
  quantity: number;
  price: number;
  sum: number;
  vat_percent: number | null;
  dds_article_id: string | null;
  dds_article_name: string | null;
};

export type Cheque = {
  id: string;
  number: string | null;
  counterparty_id: string;
  counterparty_name: string;
  issued_at: string | null;
  amount: number;
  payment_status: string;
  article_id: string | null;
  article_name: string | null;
  allocations: ChequeAllocation[];
  lines: ChequeLine[];
};

export type ChequeLinePayload = {
  name: string;
  quantity: number;
  unit?: string | null;
  price: number;
  amount?: number | null;
  dds_article_id?: string | null;
  iiko_product_id?: string | null;
  vat_percent?: number | null;
};

export type CreateChequePayload = {
  counterparty_id: string;
  article_id?: string | null;
  issued_at: string;
  bank_parts: Array<{ bank_operation_id: string; amount?: number | null }>;
  cash_amount?: number | null;
  // Ручной ввод суммы чека, когда банк ещё не передал card-операцию (взаимоисключающе с bank_parts).
  pending_card_amount?: number | null;
  track_nomenclature?: boolean;
  lines?: ChequeLinePayload[];
  comment?: string | null;
};

export async function getCardTransactions(params: {
  issued_at?: string;
  date_from?: string;
  date_to?: string;
  window_hours?: number;
}): Promise<CardTransaction[]> {
  const response = await api.get<CardTransaction[]>(`${BASE}/card-transactions`, { params });
  return response.data;
}

export async function getCheques(params?: {
  limit?: number;
  offset?: number;
}): Promise<Cheque[]> {
  const response = await api.get<Cheque[]>(`${BASE}/cheques`, { params });
  return response.data;
}

export async function createCheque(payload: CreateChequePayload): Promise<Cheque> {
  const response = await api.post<Cheque>(`${BASE}/cheques`, payload);
  return response.data;
}

// --- закрытие смены ------------------------------------------------------------

// Итог авто-штрафа смены за недостачу.
export type KassaPenaltyStatus = "none" | "applied" | "waived" | "manual_review";

export type KassaShift = {
  id: string;
  iiko_session_id: string;
  session_number: number | null;
  point_of_sale_id: string | null;
  open_date: string | null;
  close_date: string | null;
  session_status: string | null;
  session_start_cash: number | null;
  sales_cash: number | null;
  cash_sales: number | null;
  sales_card: number | null;
  total_sales: number | null;
  pay_in: number | null;
  pay_out: number | null;
  cash_remain: number | null;
  cash_diff: number | null;
  real_cash_diff: number | null;
  posted: boolean;
  penalty_status: KassaPenaltyStatus | null;
  synced_at: string;
};

export type KassaShiftPayout = {
  id: string;
  account_id_iiko: string;
  account_name: string | null;
  category: string;
  amount: number;
  comment: string | null;
};

export type KassaShiftPenalty = {
  id: string;
  employee_id: string;
  employee_full_name: string;
  amount: number;
  status: "active" | "waived";
  waived_at: string | null;
};

export type KassaShiftDetail = KassaShift & {
  payouts: KassaShiftPayout[];
  penalty_review_reason: string | null;
  shortage_threshold_pct: number | null;
  shortage_threshold_amount: number | null;
  shortage_pct_of_revenue: number | null;
  penalties: KassaShiftPenalty[];
};

export type KassaShiftSyncReport = {
  fetched: number;
  created: number;
  updated: number;
  payouts: number;
  posted: number;
  penalized: number;
  skipped: number;
};

export async function getKassaShifts(params?: {
  date_from?: string;
  date_to?: string;
}): Promise<KassaShift[]> {
  const response = await api.get<KassaShift[]>(`${BASE}/shifts`, { params });
  return response.data;
}

export async function getKassaShift(id: string): Promise<KassaShiftDetail> {
  const response = await api.get<KassaShiftDetail>(`${BASE}/shifts/${id}`);
  return response.data;
}

export async function syncKassaShifts(params?: {
  date_from?: string;
  date_to?: string;
}): Promise<KassaShiftSyncReport> {
  const response = await api.post<KassaShiftSyncReport>(`${BASE}/shifts/sync`, null, { params });
  return response.data;
}

export async function postKassaShift(id: string): Promise<KassaShiftDetail> {
  const response = await api.post<KassaShiftDetail>(`${BASE}/shifts/${id}/post`);
  return response.data;
}

export async function postKassaShiftAdjustment(id: string): Promise<KassaShiftDetail> {
  const response = await api.post<KassaShiftDetail>(`${BASE}/shifts/${id}/post-adjustment`);
  return response.data;
}

export async function waiveKassaShiftPenalty(id: string): Promise<KassaShiftDetail> {
  const response = await api.post<KassaShiftDetail>(`${BASE}/shifts/${id}/waive-penalty`);
  return response.data;
}
