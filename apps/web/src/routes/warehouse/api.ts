import { api } from "@/lib/api";

const BASE = "/warehouse";

export type WarehouseProduct = {
  id: string;
  iiko_id: string;
  name: string;
  code: string | null;
  unit: string | null;
  type: string;
};

export type WarehouseInvoiceSummary = {
  id: string;
  counterparty_id: string;
  counterparty_name: string;
  source: string;
  direction: string;
  number: string | null;
  issued_at: string | null;
  invoice_date: string | null;
  amount: number;
  staff_amount: number;
  production_amount: number;
  allocated: number;
  remaining: number;
  payment_status: string;
  iiko_push_status: string;
};

export type WarehouseInvoiceLine = {
  id: string;
  name: string;
  article: string | null;
  unit: string | null;
  quantity: number;
  price: number;
  sum: number;
  vat_percent: number | null;
  is_staff: boolean;
};

export type InvoiceAllocation = {
  id: string;
  source_kind: string;
  bank_operation_id: string | null;
  cashflow_transaction_id: string | null;
  amount: number;
};

export type WarehouseInvoiceDetail = WarehouseInvoiceSummary & {
  due_date: string | null;
  iiko_push_error: string | null;
  lines: WarehouseInvoiceLine[];
  allocations: InvoiceAllocation[];
};

export type LinePayload = {
  name: string;
  quantity: number;
  price: number;
  iiko_product_id?: string | null;
  vat_percent?: number | null;
  is_staff: boolean;
  dds_article_id?: string | null;
};

export type CreateInvoicePayload = {
  counterparty_id: string;
  issued_at: string;
  mode?: "normal" | "loan";
  we_lend?: boolean;
  number?: string | null;
  due_date?: string | null;
  lines: LinePayload[];
  // Касса: создать сразу оплаченной (списание с ТК Черникова + проводка в iiko).
  mark_paid?: boolean;
  paid_amount?: number | null;
};

export async function getProducts(params?: {
  q?: string;
  type?: string;
  include_deleted?: boolean;
  limit?: number;
}): Promise<WarehouseProduct[]> {
  const response = await api.get<WarehouseProduct[]>(`${BASE}/products`, { params });
  return response.data;
}

export async function syncProducts(): Promise<{
  seen: number;
  created: number;
  updated: number;
  goods_count: number;
  needs_unit_mapping: string[];
}> {
  const response = await api.post(`${BASE}/products/sync`);
  return response.data;
}

export async function getWarehouseInvoices(params?: {
  status?: string;
  has_staff?: boolean;
}): Promise<WarehouseInvoiceSummary[]> {
  const response = await api.get<WarehouseInvoiceSummary[]>(`${BASE}/invoices`, { params });
  return response.data;
}

export async function getWarehouseInvoice(id: string): Promise<WarehouseInvoiceDetail> {
  const response = await api.get<WarehouseInvoiceDetail>(`${BASE}/invoices/${id}`);
  return response.data;
}

export async function createWarehouseInvoice(
  payload: CreateInvoicePayload,
): Promise<WarehouseInvoiceDetail> {
  const response = await api.post<WarehouseInvoiceDetail>(`${BASE}/invoices`, payload);
  return response.data;
}

export async function getNextInvoiceNumber(): Promise<string> {
  const response = await api.get<{ number: string }>(`${BASE}/invoices/next-number`);
  return response.data.number;
}

export type StaffArticle = { id: string; name: string };

export async function getStaffArticles(): Promise<StaffArticle[]> {
  const response = await api.get<StaffArticle[]>(`${BASE}/staff-articles`);
  return response.data;
}

export async function pushInvoiceToIiko(id: string): Promise<WarehouseInvoiceDetail> {
  const response = await api.post<WarehouseInvoiceDetail>(`${BASE}/invoices/${id}/push`);
  return response.data;
}

export type OpenLoan = {
  id: string;
  counterparty_id: string;
  counterparty_name: string;
  number: string | null;
  issued_at: string | null;
  we_lend: boolean;
  amount: number;
  returned: number;
  remaining: number;
  status: string | null;
};

export type ReturnableLine = {
  id: string;
  name: string;
  unit: string | null;
  price: number;
  quantity: number;
  remaining_qty: number;
};

export type LoanReturnable = {
  id: string;
  number: string | null;
  we_lend: boolean;
  amount: number;
  remaining: number;
  lines: ReturnableLine[];
};

export type ReturnPayload = {
  loan_id: string;
  issued_at: string;
  number?: string | null;
  returns: Array<{ amount: number; loan_line_item_id?: string | null; quantity?: number | null }>;
};

export async function getOpenLoans(counterpartyId?: string): Promise<OpenLoan[]> {
  const response = await api.get<OpenLoan[]>(`${BASE}/loans`, {
    params: counterpartyId ? { counterparty_id: counterpartyId } : undefined,
  });
  return response.data;
}

export async function getLoanReturnable(loanId: string): Promise<LoanReturnable> {
  const response = await api.get<LoanReturnable>(`${BASE}/loans/${loanId}/returnable`);
  return response.data;
}

export async function createBarterReturn(
  payload: ReturnPayload,
): Promise<WarehouseInvoiceDetail> {
  const response = await api.post<WarehouseInvoiceDetail>(`${BASE}/invoices/return`, payload);
  return response.data;
}

// --- Phase 4: ДДС-мэтч по дате+времени + сплит-оплата ------------------------

export type MatchCandidate = {
  bank_operation_id: string;
  operation_date: string;
  posted_at: string | null;
  amount: number;
  official_name: string | null;
  inn: string | null;
  requisites: Record<string, string>;
  tier: number; // 1 точное время+сумма … 4 фолбэк по дате
  minutes_delta: number | null;
};

export type MatchSuggestion = {
  invoice_id: string;
  invoice_number: string | null;
  invoice_amount: number;
  remaining: number;
  issued_at: string | null;
  counterparty_id: string;
  counterparty_name: string;
  counterparty_has_inn: boolean;
  confident: boolean;
  candidates: MatchCandidate[];
};

export type BankPartInput = { bank_operation_id: string; amount?: number | null };
export type CashPartInput = {
  wallet_id: string;
  amount: number;
  operation_date: string;
  article_id?: string | null;
  comment?: string | null;
};
export type PaySplitPayload = {
  bank_parts?: BankPartInput[];
  cash_parts?: CashPartInput[];
  split_staff?: boolean;
};

/** Кандидаты банк-операций по дате+времени чека. Возвращает null, если накладная
 *  не поддерживает сверку (бартер/доходная/оплачена) — бэк отвечает 404. */
export async function getInvoiceMatchSuggestions(
  id: string,
  params?: { window_hours?: number; tolerance_pct?: number; tight_minutes?: number },
): Promise<MatchSuggestion | null> {
  try {
    const response = await api.get<MatchSuggestion>(
      `${BASE}/invoices/${id}/match-suggestions`,
      { params },
    );
    return response.data;
  } catch (error) {
    if ((error as { response?: { status?: number } })?.response?.status === 404) {
      return null;
    }
    throw error;
  }
}

export async function confirmInvoiceMatch(payload: {
  invoice_id: string;
  bank_operation_id: string;
  enrich: boolean;
}): Promise<{ invoice_id: string; payment_status: string; enriched: boolean }> {
  const response = await api.post(`${BASE}/match/confirm`, payload);
  return response.data;
}

export async function payInvoiceSplit(
  id: string,
  payload: PaySplitPayload,
): Promise<WarehouseInvoiceDetail> {
  const response = await api.post<WarehouseInvoiceDetail>(
    `${BASE}/invoices/${id}/pay-split`,
    payload,
  );
  return response.data;
}
