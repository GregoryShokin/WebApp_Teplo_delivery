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
  // Черновик в банке (оплата уехала) — повторную оплату не предлагаем.
  draft_id: string | null;
  // Статус черновика: created/updated → «Отправлено в банк»; paid+pays_via_safe → «Деньги в Сейфе».
  draft_status: string | null;
  draft_pays_via_safe: boolean;
  iiko_push_status: string;
  // iiko documentId (есть → накладная существует в iiko; удаление будет двусторонним).
  external_id: string | null;
  barter_role: string | null;
  // Контроль цен: clean — норма; flagged — есть подозрительные цены (оплата/банк заблокированы);
  // confirmed — цены подтверждены человеком. undefined у старых ответов.
  price_control_status?: "clean" | "flagged" | "confirmed";
};

// Аномальная позиция: снимок среднего/отклонения на момент расчёта.
export type PriceAnomaly = {
  name: string;
  product_guid: string | null;
  unit: string | null;
  price: number;
  avg_price: number;
  sample_count: number;
  deviation_pct: number;
  direction: "high" | "low";
};

// Статистика цены товара для живой подсветки при вводе строки.
export type ProductPriceStats = {
  avg_price: number | null;
  sample_count: number;
  unit: string | null;
  upper_pct: number;
  lower_pct: number;
  lookback_days: number;
  min_samples: number;
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
  // Расходная строка (не товар → не уходит в iiko): по статье ДДС, единообразно для
  // чеков и накладных. dds_article_name — название статьи для бейджа.
  is_expense: boolean;
  // Позиция возвращена в магазин (чек Кассы): в чеке остаётся для gross-следа,
  // но не проведена — показывается красной строкой.
  is_return?: boolean;
  dds_article_name: string | null;
  iiko_product_id: string | null;
  dds_article_id: string | null;
  // Контроль цен: если цена аномальна — направление (high=дорого/low=дёшево), среднее и
  // отклонение для подсветки ячейки цены. null у нормальных строк.
  price_flag?: "high" | "low" | null;
  price_avg?: number | null;
  price_deviation_pct?: number | null;
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
  // Сумма возвращённых позиций чека (gross = amount + returned_total).
  returned_total?: number;
  // Отражение коррекции оплаченной накладной в iiko (возврат старого прихода + новая приходная):
  // none/pending/booked/failed/skipped.
  iiko_return_status?: string;
  iiko_return_external_id?: string | null;
  iiko_return_error?: string | null;
  // Контроль цен: снимок аномалий + кто/когда подтвердил.
  price_anomalies?: PriceAnomaly[];
  price_confirmed_at?: string | null;
  price_confirmed_by?: string | null;
  // Гард правки оплаченной: отражена ли оплата оригинала в iiko (иначе правку не пускаем —
  // возврат уйдёт не на ту сумму) и можно ли отправить оплату автоматически (одиночный банк).
  iiko_payment_settled?: boolean;
  iiko_payment_auto_sendable?: boolean;
  // Заполняется ответом send-iiko-payment, если автоотправка не прошла (iiko отклонил / сеть).
  iiko_payment_push_error?: string | null;
};

export type LinePayload = {
  name: string;
  quantity: number;
  price: number;
  iiko_product_id?: string | null;
  vat_percent?: number | null;
  is_staff: boolean;
  dds_article_id?: string | null;
  // Сумма строки как её видит пользователь — эталон (кол-во×округлённая цена не всегда точна).
  sum?: number;
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
  // Создано через страницу Касса → source="kassa_invoice" (вкладка «Накладные» Кассы).
  via_kassa?: boolean;
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

// Скользящее среднее цены товара + пороги — для живой подсветки отклонения при вводе строки.
export async function getProductPriceStats(productId: string): Promise<ProductPriceStats> {
  const response = await api.get<ProductPriceStats>(`${BASE}/products/${productId}/price-stats`);
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

export type UpdateInvoicePayload = {
  lines: LinePayload[];
  issued_at?: string;
  number?: string | null;
};

export async function updateWarehouseInvoice(
  id: string,
  payload: UpdateInvoicePayload,
): Promise<WarehouseInvoiceDetail> {
  const response = await api.put<WarehouseInvoiceDetail>(`${BASE}/invoices/${id}`, payload);
  return response.data;
}

// Правка УЖЕ ОПЛАЧЕННОЙ накладной (право invoices.normal.edit_paid): излишек оплаты уходит в
// дебиторку поставщику. `moved_to_receivable` — сколько именно ушло (для тоста).
export type AdjustPaidInvoiceResult = WarehouseInvoiceDetail & { moved_to_receivable?: number };

export async function adjustPaidInvoice(
  id: string,
  payload: UpdateInvoicePayload,
): Promise<AdjustPaidInvoiceResult> {
  const response = await api.post<AdjustPaidInvoiceResult>(
    `${BASE}/invoices/${id}/adjust-paid`,
    payload,
  );
  return response.data;
}

// Повторить отражение коррекции в iiko после сбоя (сага возврат→новая приходная продолжится
// с последнего успешного шага; чек-пойнты хранятся на накладной).
export async function retryIikoReturn(id: string): Promise<WarehouseInvoiceDetail> {
  const response = await api.post<WarehouseInvoiceDetail>(
    `${BASE}/invoices/${id}/retry-iiko-return`,
  );
  return response.data;
}

// Гард правки оплаченной: «Отправить оплату в iiko сейчас» — провести add_payment немедленно
// (не дожидаясь сверочного джоба). Идемпотентно; «already paid» = успех. Ответ содержит
// iiko_payment_push_error, если автоотправка не прошла (тогда остаётся ручное подтверждение).
export async function sendIikoPayment(id: string): Promise<AdjustPaidInvoiceResult> {
  const response = await api.post<AdjustPaidInvoiceResult>(
    `${BASE}/invoices/${id}/send-iiko-payment`,
  );
  return response.data;
}

// Гард правки оплаченной: «Оплата подтверждена вручную» — человек утверждает, что платёж уже
// проведён в бэк-офисе iiko; пишем ok-маркер (джоб больше не шлёт add_payment) и снимаем гард.
export async function confirmIikoPaymentManual(id: string): Promise<WarehouseInvoiceDetail> {
  const response = await api.post<WarehouseInvoiceDetail>(
    `${BASE}/invoices/${id}/confirm-iiko-payment`,
  );
  return response.data;
}

export async function payInvoiceFromKassa(
  id: string,
  amount?: number | null,
): Promise<WarehouseInvoiceDetail> {
  const response = await api.post<WarehouseInvoiceDetail>(`${BASE}/invoices/${id}/pay-kassa`, {
    amount: amount ?? null,
  });
  return response.data;
}

// «ОК, всё верно» — подтвердить подозрительные цены и разблокировать оплату/отправку в банк
// (право invoices.confirm_price).
export async function confirmInvoicePrices(id: string): Promise<WarehouseInvoiceDetail> {
  const response = await api.post<WarehouseInvoiceDetail>(`${BASE}/invoices/${id}/confirm-prices`);
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

export async function deleteWarehouseInvoice(id: string): Promise<void> {
  await api.delete(`${BASE}/invoices/${id}`);
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

export async function createBarterReturn(payload: ReturnPayload): Promise<WarehouseInvoiceDetail> {
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
  // Статью ДДС не передаём — бэк разносит каждую наличную строку по позициям накладной.
  split_by_lines?: boolean;
};

/** Кандидаты банк-операций по дате+времени чека. Возвращает null, если накладная
 *  не поддерживает сверку (бартер/доходная/оплачена) — бэк отвечает 404. */
export async function getInvoiceMatchSuggestions(
  id: string,
  params?: { window_hours?: number; tolerance_pct?: number; tight_minutes?: number },
): Promise<MatchSuggestion | null> {
  try {
    const response = await api.get<MatchSuggestion>(`${BASE}/invoices/${id}/match-suggestions`, {
      params,
    });
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
