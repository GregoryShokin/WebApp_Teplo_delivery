import { api } from "@/lib/api";

export type LedgerCategory = {
  id: string;
  code: string;
  name: string;
  sort_order: number;
  is_active: boolean;
};

export type CounterpartyInvoice = {
  id: string;
  counterparty_id: string;
  counterparty_name: string;
  ledger_category_id: string | null;
  source: string;
  direction: string;
  number: string | null;
  invoice_date: string | null;
  due_date: string | null;
  service_period_start: string | null;
  service_period_end: string | null;
  service_period_status: string;
  amount: number;
  vat_total: number;
  vat_breakdown: Record<string, string>;
  allocated: number;
  remaining: number;
  payment_status: string;
  draft_id: string | null;
  barter_settlement_id: string | null;
  // Роль сведённой бартер-накладной по хронологии: "loan" | "return" | null (пока открыта).
  barter_role: string | null;
  iiko_push_status: string;
  // Текст последней ошибки пуша в iiko (для детализации красного статуса в списке).
  iiko_push_error: string | null;
  // iiko documentId: заполнен → накладная существует в iiko (у source=iiko — всегда).
  external_id: string | null;
  // Статус банковского черновика: created/updated → «Отправлено в банк»;
  // paid + pays_via_safe → «Деньги в Сейфе» (наличные ещё не выданы поставщику).
  draft_status: string | null;
  draft_pays_via_safe: boolean;
  // Контроль цен: clean — норма; flagged — подозрительные цены (строку подсвечиваем,
  // оплата/банк заблокированы); confirmed — подтверждено человеком.
  price_control_status?: "clean" | "flagged" | "confirmed";
};

export type RegistryItem = {
  counterparty_id: string;
  name: string;
  inn: string | null;
  status: string;
  relationship: string;
  ledger_category_id: string | null;
  brand_group: string | null;
  internal_name: string | null;
  payment_delay_days: number | null;
  requisites_verified: boolean;
  kassa_enabled: boolean;
  has_iiko_guid: boolean;
  unpaid_count: number;
  unpaid_remaining: number;
  receivable_remaining: number;
  prepayment_balance: number;
};

export type CollectionSource = {
  id: string;
  kind: string;
  value: string | null;
  is_active: boolean;
  note: string | null;
};

export type CounterpartyProfile = {
  ledger_category_id: string | null;
  relationship: string;
  relationship_manual: boolean;
  brand_group: string | null;
  internal_name: string | null;
  payment_delay_days: number | null;
  payment_due_day_of_month: number | null;
  manager_name: string | null;
  manager_phone: string | null;
  default_dds_article_id: string | null;
  service_period_required: boolean;
  service_period_mode: "automatic" | "manual";
  default_service_period_offset_months: number | null;
  requisites: Record<string, unknown>;
  requisites_verified: boolean;
  kassa_enabled: boolean;
  status: string;
} | null;

export type CounterpartyCard = {
  counterparty_id: string;
  name: string;
  inn: string | null;
  type: string;
  status: string;
  relationship: string;
  barter_balance: number;
  profile: CounterpartyProfile;
  aliases: Array<{ alias: string; source: string | null }>;
  collection_sources: CollectionSource[];
  routing_rules: Array<{
    id: string;
    prefix: string;
    target_counterparty_id: string;
    target_name: string;
  }>;
  invoices: CounterpartyInvoice[];
  drafts: Array<{
    id: string;
    amount: number;
    status: string;
    provider_ref: string | null;
    created_at: string;
  }>;
};

export type PaymentDraft = {
  id: string;
  counterparty_id: string;
  document_id: string;
  amount: number;
  status: string;
  provider_ref: string | null;
  last_error: string | null;
  // Выплата через Сейф (неофициальный поставщик): черновик выписан на карту ИП.
  pays_via_safe: boolean;
  created_at: string;
};

export type NeedsSetup = {
  count: number;
  items: Array<{
    counterparty_id: string;
    name: string;
    inn: string | null;
    created_at: string;
  }>;
};

const BASE = "/counterparties";

export async function getLedgerCategories(): Promise<LedgerCategory[]> {
  const response = await api.get<LedgerCategory[]>(`${BASE}/categories`);
  return response.data;
}

export async function getInvoices(params?: {
  status?: string;
  counterparty_id?: string;
  // Мультивыбор поставщиков — CSV из UUID (как status).
  counterparty_ids?: string;
  category_id?: string;
  in_draft?: boolean;
  direction?: string;
  relationship?: string;
  source?: string;
  // Диапазон по дате накладной (ISO YYYY-MM-DD), включительно.
  date_from?: string;
  date_to?: string;
  // Только «наши» накладные без документа в iiko (не отправлены / упал пуш).
  not_in_iiko?: boolean;
}): Promise<CounterpartyInvoice[]> {
  const response = await api.get<CounterpartyInvoice[]>(`${BASE}/invoices`, { params });
  return response.data;
}

export type ManualInvoicePayload = {
  counterparty_id: string;
  amount: number;
  number?: string | null;
  invoice_date?: string | null;
  due_date?: string | null;
  note?: string | null;
  vat_breakdown?: Record<string, number> | null;
};

export async function createManualInvoice(
  payload: ManualInvoicePayload,
): Promise<CounterpartyInvoice> {
  const response = await api.post<CounterpartyInvoice>(`${BASE}/invoices`, payload);
  return response.data;
}

export async function voidInvoice(id: string): Promise<CounterpartyInvoice> {
  const response = await api.post<CounterpartyInvoice>(`${BASE}/invoices/${id}/void`);
  return response.data;
}

export async function allocateCash(
  id: string,
  payload: { amount: number; cashflow_transaction_id?: string | null },
): Promise<CounterpartyInvoice> {
  const response = await api.post<CounterpartyInvoice>(
    `${BASE}/invoices/${id}/allocate-cash`,
    payload,
  );
  return response.data;
}

export type Wallet = { id: string; code: string; name: string; type: string };

/** Счета, с которых разрешена наличная оплата накладных (решение владельца):
 *  Сейф и Торговая касса Черникова. Банковские/ДДС-кошельки (Сбер, Т-Банк и пр.)
 *  в оплату накладных не выбираются — банк идёт только через черновик Т-Банка. */
export const CASH_PAYMENT_WALLET_CODES = new Set(["cash_safe", "tk_chernikova"]);
export type ExpenseArticle = { id: string; code: string; name: string };

export async function getWallets(): Promise<Wallet[]> {
  const response = await api.get<Wallet[]>(`${BASE}/wallets`);
  return response.data;
}

export async function getExpenseArticles(): Promise<ExpenseArticle[]> {
  const response = await api.get<ExpenseArticle[]>(`${BASE}/expense-articles`);
  return response.data;
}

export type ManualPaymentPayload = {
  amount: number;
  wallet_id: string;
  operation_date: string;
  article_id?: string | null;
  comment?: string | null;
};

export async function payInvoice(
  id: string,
  payload: ManualPaymentPayload,
): Promise<CounterpartyInvoice> {
  const response = await api.post<CounterpartyInvoice>(`${BASE}/invoices/${id}/pay`, payload);
  return response.data;
}

// --- предоплаты поставщикам (дебиторка) ---
export type SupplierPrepayment = {
  id: string;
  counterparty_id: string;
  kind: string;
  wallet_id: string | null;
  amount: number;
  amount_settled: number;
  status: string;
  article_id: string | null;
  note: string | null;
  created_at: string;
};

export type PrepaymentCreatePayload = {
  counterparty_id: string;
  wallet_id: string;
  amount: number;
  // Пусто — сервер поставит сегодня по МСК (клиентской дате не доверяем).
  operation_date?: string;
  article_id?: string | null;
  kind?: string;
  note?: string | null;
};

export async function createPrepayment(
  payload: PrepaymentCreatePayload,
): Promise<SupplierPrepayment> {
  const response = await api.post<SupplierPrepayment>(`${BASE}/prepayments`, payload);
  return response.data;
}

export async function getPrepayments(params?: {
  counterparty_id?: string;
  only_open?: boolean;
}): Promise<SupplierPrepayment[]> {
  const response = await api.get<SupplierPrepayment[]>(`${BASE}/prepayments`, { params });
  return response.data;
}

export type BankPrepaymentDraftPayload = {
  counterparty_id: string;
  amount: number;
  article_id?: string | null;
};

// «Банк по реквизитам»: standalone-черновик в банк; при оплате превращается в предоплату.
export async function createBankPrepaymentDraft(
  payload: BankPrepaymentDraftPayload,
): Promise<PaymentDraft> {
  const response = await api.post<PaymentDraft>(`${BASE}/prepayments/bank-draft`, payload);
  return response.data;
}

export async function settleInvoiceFromPrepayment(
  invoiceId: string,
  payload: { prepayment_id: string; amount?: number },
): Promise<CounterpartyInvoice> {
  const response = await api.post<CounterpartyInvoice>(
    `${BASE}/invoices/${invoiceId}/settle-from-prepayment`,
    payload,
  );
  return response.data;
}

export async function getRegistry(params?: {
  category_id?: string;
  include_archived?: boolean;
  kassa_only?: boolean;
}): Promise<RegistryItem[]> {
  const response = await api.get<RegistryItem[]>(`${BASE}/registry`, { params });
  return response.data;
}

export type CounterpartyDirectoryItem = {
  id: string;
  name: string;
  inn: string | null;
  status: string;
};

/** Один справочник для реестра, фильтров и классификации ДДС. */
export async function getCounterpartyDirectory(): Promise<CounterpartyDirectoryItem[]> {
  const items = await getRegistry();
  return items.map((item) => ({
    id: item.counterparty_id,
    name: item.name,
    inn: item.inn,
    status: item.status,
  }));
}

export async function getNeedsSetup(): Promise<NeedsSetup> {
  const response = await api.get<NeedsSetup>(`${BASE}/needs-setup`);
  return response.data;
}

export type IikoSupplierOption = {
  guid: string;
  name: string;
  inn: string | null;
};

// Поставщики из справочника iiko, ещё не привязанные к контрагенту (живой запрос к iiko).
export async function getIikoSuppliers(): Promise<IikoSupplierOption[]> {
  const response = await api.get<IikoSupplierOption[]>(`${BASE}/iiko-suppliers`);
  return response.data;
}

export async function getCounterpartyCard(id: string): Promise<CounterpartyCard> {
  const response = await api.get<CounterpartyCard>(`${BASE}/${id}`);
  return response.data;
}

export type CounterpartyCreatePayload = {
  name: string;
  inn?: string | null;
  type?: string;
  relationship?: string;
  internal_name?: string | null;
  brand_group?: string | null;
  payment_delay_days?: number | null;
  payment_due_day_of_month?: number | null;
  manager_name?: string | null;
  manager_phone?: string | null;
  default_dds_article_id?: string | null;
  confirm_no_dds_article?: boolean;
  service_period_required?: boolean;
  service_period_mode?: "automatic" | "manual";
  default_service_period_offset_months?: number | null;
  requisites?: Record<string, string>;
  requisites_verified?: boolean;
  iiko_supplier_guid?: string | null;
};

export async function createCounterparty(
  payload: CounterpartyCreatePayload,
): Promise<CounterpartyCard> {
  const response = await api.post<CounterpartyCard>(BASE, payload);
  return response.data;
}

export type ProfileUpdatePayload = {
  name?: string;
  inn?: string | null;
  type?: string;
  ledger_category_id?: string | null;
  relationship?: string | null;
  brand_group?: string | null;
  internal_name?: string | null;
  payment_delay_days?: number | null;
  payment_due_day_of_month?: number | null;
  manager_name?: string | null;
  manager_phone?: string | null;
  default_dds_article_id?: string | null;
  confirm_no_dds_article?: boolean;
  service_period_required?: boolean | null;
  service_period_mode?: "automatic" | "manual" | null;
  default_service_period_offset_months?: number | null;
  status?: string | null;
};

export async function updateProfile(
  id: string,
  payload: ProfileUpdatePayload,
): Promise<CounterpartyCard> {
  const response = await api.put<CounterpartyCard>(`${BASE}/${id}/profile`, payload);
  return response.data;
}

export async function setRequisites(
  id: string,
  payload: { requisites: Record<string, unknown>; verified: boolean },
): Promise<CounterpartyCard> {
  const response = await api.put<CounterpartyCard>(`${BASE}/${id}/requisites`, payload);
  return response.data;
}

export async function getRequisitesSuggestion(id: string): Promise<Record<string, unknown>> {
  const response = await api.get<Record<string, unknown>>(`${BASE}/${id}/requisites/suggestion`);
  return response.data;
}

export async function archiveCounterparty(id: string): Promise<CounterpartyCard> {
  const response = await api.post<CounterpartyCard>(`${BASE}/${id}/archive`);
  return response.data;
}

export async function unarchiveCounterparty(id: string): Promise<CounterpartyCard> {
  const response = await api.post<CounterpartyCard>(`${BASE}/${id}/unarchive`);
  return response.data;
}

export async function setKassaEnabled(id: string, enabled: boolean): Promise<CounterpartyCard> {
  const response = await api.post<CounterpartyCard>(`${BASE}/${id}/kassa-enabled`, { enabled });
  return response.data;
}

export async function addCollectionSource(
  id: string,
  payload: { kind: string; value?: string | null; note?: string | null },
): Promise<CounterpartyCard> {
  const response = await api.post<CounterpartyCard>(`${BASE}/${id}/sources`, payload);
  return response.data;
}

export async function deleteCollectionSource(
  counterpartyId: string,
  sourceId: string,
): Promise<CounterpartyCard> {
  const response = await api.delete<CounterpartyCard>(
    `${BASE}/${counterpartyId}/sources/${sourceId}`,
  );
  return response.data;
}

export async function addRoutingRule(
  counterpartyId: string,
  payload: { prefix: string; target_counterparty_id: string },
): Promise<CounterpartyCard> {
  const response = await api.post<CounterpartyCard>(`${BASE}/${counterpartyId}/routing`, payload);
  return response.data;
}

export async function deleteRoutingRule(
  counterpartyId: string,
  ruleId: string,
): Promise<CounterpartyCard> {
  const response = await api.delete<CounterpartyCard>(
    `${BASE}/${counterpartyId}/routing/${ruleId}`,
  );
  return response.data;
}

export async function createDraft(invoiceIds: string[]): Promise<PaymentDraft> {
  const response = await api.post<PaymentDraft>(`${BASE}/drafts`, { invoice_ids: invoiceIds });
  return response.data;
}

export type MatchCandidate = {
  bank_operation_id: string;
  operation_date: string;
  amount: number;
  official_name: string | null;
  inn: string | null;
  requisites: Record<string, unknown>;
};

export type MatchSuggestion = {
  invoice_id: string;
  invoice_number: string | null;
  invoice_amount: number;
  counterparty_id: string;
  counterparty_name: string;
  counterparty_has_inn: boolean;
  candidates: MatchCandidate[];
  confident: boolean;
};

export async function getMatchSuggestions(): Promise<MatchSuggestion[]> {
  const response = await api.get<MatchSuggestion[]>(`${BASE}/match/suggestions`);
  return response.data;
}

export async function confirmMatch(payload: {
  invoice_id: string;
  bank_operation_id: string;
  enrich: boolean;
}): Promise<{ invoice_id: string; payment_status: string; enriched: boolean }> {
  const response = await api.post<{
    invoice_id: string;
    payment_status: string;
    enriched: boolean;
  }>(`${BASE}/match/confirm`, payload);
  return response.data;
}

export async function syncInvoices(): Promise<Record<string, number>> {
  const response = await api.post<Record<string, number>>(`${BASE}/sync`);
  return response.data;
}

export type BarterInvoice = {
  id: string;
  number: string | null;
  invoice_date: string | null;
  amount: number;
  products: Array<string | null>;
};

export type BarterSettlementView = {
  id: string;
  amount: number;
  is_auto: boolean;
  we_lent: boolean; // true — лендером были мы (заём = расход), false — заняли нам
  loan_numbers: string[];
  return_numbers: string[];
};

export type BarterDetail = {
  relationship_balance: number;
  open_payables: BarterInvoice[];
  open_receivables: BarterInvoice[];
  settlements: BarterSettlementView[];
};

export async function getBarterDetail(id: string): Promise<BarterDetail> {
  const response = await api.get<BarterDetail>(`${BASE}/${id}/barter`);
  return response.data;
}

export type BarterSuggestion = {
  payable_ids: string[];
  receivable_ids: string[];
  amount: number;
  confident: boolean;
  we_lent: boolean; // кто лендер в предложенном зачёте (по хронологии)
};

export async function getBarterSuggestions(id: string): Promise<BarterSuggestion[]> {
  const response = await api.get<BarterSuggestion[]>(`${BASE}/${id}/barter/suggestions`);
  return response.data;
}

export async function autoSettleBarter(id: string): Promise<{ settled: number }> {
  const response = await api.post<{ settled: number }>(`${BASE}/${id}/barter/auto-settle`);
  return response.data;
}

export async function settleBarter(
  id: string,
  payload: { payable_ids: string[]; receivable_ids: string[] },
): Promise<{ id: string; amount: number }> {
  const response = await api.post<{ id: string; amount: number }>(
    `${BASE}/${id}/barter/settle`,
    payload,
  );
  return response.data;
}
