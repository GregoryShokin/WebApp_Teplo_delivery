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
  operational_scope: "warehouse" | "finance" | "unknown";
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
  // bill — счёт (основание для платежа, не долг); closing — УПД/акт/накладная.
  doc_kind?: string;
  // active — документ в силе; pending — дата ещё не наступила, долга нет и платить нечего
  // (правило 4 канона). См. isNotYetInForce в shared.tsx.
  activation_status?: string;
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
  // Откуда карточка появилась: iiko / manual / email / sbis.
  origin: string | null;
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
  // Пустая статья — принятое решение, а не недозаполненность: хранится, чтобы карточку
  // без статьи не приходилось подтверждать заново при каждом открытии.
  confirm_no_dds_article: boolean;
  service_period_required: boolean;
  default_service_period_offset_months: number | null;
  // Предоплатная модель по банк-фиду (Манго): списания с карты пополняют дебиторку.
  bank_payments_create_prepayment: boolean;
  // До какого числа месяца после периода услуги ждём закрывающий документ (null — с 1-го).
  closing_doc_expected_day: number | null;
  // 'goods' | 'service' | null (определять по факту складских накладных).
  settlement_contour: string | null;
  // 'fixed_tariff' | 'per_invoice' | 'one_off' | null — чего экран признания ждёт от платежа.
  service_billing_mode: string | null;
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
  operational_scope?: "warehouse" | "finance" | "unknown";
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
  // По умолчанию backend отдаёт только поставщиков (пикеры накладных/платежей).
  // true — полный список с банками/налоговой: страница реестра и справочник ДДС.
  include_non_suppliers?: boolean;
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
  // Классификации ДДС нужны и не-поставщики: комиссия банка, налоговая и т.п.
  const items = await getRegistry({ include_non_suppliers: true });
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

// Сверка расчётов: платежи и закрывающие документы одной хронологией (вкладка «Сверка»).
export type LedgerRow = {
  kind: "payment" | "document";
  id: string;
  row_date: string;
  amount: number;
  title: string;
  subtitle: string | null;
  period_start: string | null;
  period_end: string | null;
  /** Для платежа — сколько не подтверждено документом; для документа — неоплаченный остаток. */
  uncovered: number;
  status: "ok" | "waiting" | "overdue";
  expected_by: string | null;
  days_overdue: number;
  /** Остаток расчётов ПОСЛЕ строки: >0 — мы заплатили вперёд, <0 — должны мы. */
  balance_after: number;
  prepayment_id: string | null;
  /** Документ создан нами (абонентский платёж без закрывающих), а не прислан контрагентом. */
  self_billed: boolean;
};

export type LedgerMonth = {
  month: string;
  paid: number;
  documented: number;
  gap: number;
  has_overdue: boolean;
};

export type SettlementLedger = {
  counterparty_id: string;
  counterparty_name: string;
  contour: "goods" | "service";
  contour_manual: boolean;
  closing_doc_expected_day: number | null;
  opening_balance: number;
  closing_balance: number;
  total_paid: number;
  total_documented: number;
  overdue_amount: number;
  /** Признано нами без первички: в P&L есть, в налоговые расходы УСН не идёт. */
  self_billed_amount: number;
  has_barter: boolean;
  rows: LedgerRow[];
  months: LedgerMonth[];
};

export async function getSettlementLedger(
  counterpartyId: string,
  params?: { date_from?: string; date_to?: string },
): Promise<SettlementLedger> {
  const response = await api.get<SettlementLedger>(
    `/accounting/suppliers/${counterpartyId}/ledger`,
    { params },
  );
  return response.data;
}

export type SettlementGap = {
  counterparty_id: string;
  counterparty_name: string;
  period_start: string;
  period_end: string;
  amount: number;
  expected_by: string;
  days_overdue: number;
  payments: number;
  last_payment_date: string;
};

export async function getSettlementGaps(includeGoods = false): Promise<{
  items: SettlementGap[];
  total_amount: number;
  as_of: string;
}> {
  const response = await api.get<{
    items: SettlementGap[];
    total_amount: number;
    as_of: string;
  }>("/accounting/suppliers/gaps", { params: { include_goods: includeGoods } });
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
  default_service_period_offset_months?: number | null;
  bank_payments_create_prepayment?: boolean | null;
  closing_doc_expected_day?: number | null;
  settlement_contour?: string | null;
  service_billing_mode?: string | null;
  status?: string | null;
};

export type MerchantRule = {
  id: string;
  name: string;
  purpose_pattern: string | null;
  article_id: string | null;
  is_active: boolean;
};

export async function getMerchantRules(id: string): Promise<MerchantRule[]> {
  const response = await api.get<MerchantRule[]>(`${BASE}/${id}/merchant-rules`);
  return response.data;
}

export async function createMerchantRule(
  id: string,
  payload: { purpose_pattern: string; article_id?: string | null },
): Promise<{ rule: MerchantRule; updated_existing: boolean; backfilled: number }> {
  const response = await api.post(`${BASE}/${id}/merchant-rule`, payload);
  return response.data;
}

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

export type RequisitesCandidate = {
  key: string;
  source: "bank" | "email";
  source_label: string;
  bank_name: string | null;
  last_seen_on: string | null;
  last_amount: string | null;
  hits: number;
  requisites: Record<string, string>;
  missing: string[];
  existing_counterparty_id: string | null;
  existing_counterparty_name: string | null;
  own_account: boolean;
};

/** Реквизиты из истории платежей и распознанных счетов — по ИНН, названию или счёту.
 *  Не привязано к карточке: работает и в окне «Новый контрагент», до её создания. */
export async function searchRequisitesInHistory(query: string): Promise<RequisitesCandidate[]> {
  const response = await api.get<RequisitesCandidate[]>(`${BASE}/requisites/search`, {
    params: { query },
  });
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

export async function createDraft(
  invoiceIds: string[],
  // Банк-плательщик: bank_draft (Т-Банк, по умолчанию) или bank_draft_sber (Сбер).
  channel?: "bank_draft" | "bank_draft_sber",
): Promise<PaymentDraft> {
  const response = await api.post<PaymentDraft>(`${BASE}/drafts`, {
    invoice_ids: invoiceIds,
    ...(channel ? { channel } : {}),
  });
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

// --- СБИС (ЭДО): зеркало входящих документов для сверки с накладными ---
// СБИС — не источник оплат (решение владельца): к оплате ведёт iiko-синк, здесь
// подсвечиваем «пришло по ЭДО, но нет в iiko».

const SBIS_BASE = "/sbis";

export type SbisMatchedInvoice = {
  id: string;
  number: string | null;
  invoice_date: string | null;
  // Decimal с бэка приходит строкой.
  amount: number | string;
  source: string;
  payment_status: string;
};

export type SbisIntakeStatus =
  | "mirror"
  | "new_counterparty"
  | "materialized"
  | "duplicate"
  | "sent_to_recognition";

export type SbisDocument = {
  id: string;
  doc_type: string | null;
  regulation: string | null;
  title: string | null;
  number: string | null;
  doc_date: string | null;
  // Decimal с бэка приходит строкой.
  amount: number | string | null;
  amount_wo_vat: number | string | null;
  counterparty_name: string | null;
  counterparty_inn: string | null;
  state_name: string | null;
  attachment_kind: string | null;
  link_cabinet: string | null;
  has_pdf: boolean;
  match_status: "unmatched" | "matched" | "dismissed";
  match_note: string | null;
  matched_invoice: SbisMatchedInvoice | null;
  last_synced_at: string;
  // Маршрутизация: режим определяет карточка контрагента (канал 'sbis').
  intake_status: SbisIntakeStatus;
  counterparty_id: string | null;
  counterparty_status: string | null;
  channel_enabled: boolean;
  has_email_channel: boolean;
  invoice: SbisMatchedInvoice | null;
};

export type SbisSyncResult = {
  configured: boolean;
  fetched: number;
  created: number;
  updated: number;
  matched: number;
  skipped_deleted: number;
  new_counterparties: number;
  materialized: number;
  duplicates: number;
  settled_from_prepayments: number;
  sent_to_recognition: number;
};

export async function getSbisDocuments(matchStatus?: string): Promise<SbisDocument[]> {
  const response = await api.get<SbisDocument[]>(`${SBIS_BASE}/documents`, {
    params: matchStatus ? { match_status: matchStatus } : undefined,
  });
  return response.data;
}

export async function enableSbisChannel(id: string): Promise<SbisDocument> {
  const response = await api.post<SbisDocument>(`${SBIS_BASE}/documents/${id}/enable-channel`);
  return response.data;
}

export async function syncSbisDocuments(): Promise<SbisSyncResult> {
  const response = await api.post<SbisSyncResult>(`${SBIS_BASE}/sync`);
  return response.data;
}

export async function fetchSbisPdfUrl(id: string): Promise<string> {
  const response = await api.get(`${SBIS_BASE}/documents/${id}/pdf`, { responseType: "blob" });
  return URL.createObjectURL(response.data as Blob);
}

export async function dismissSbisDocument(id: string): Promise<SbisDocument> {
  const response = await api.post<SbisDocument>(`${SBIS_BASE}/documents/${id}/dismiss`);
  return response.data;
}

export async function restoreSbisDocument(id: string): Promise<SbisDocument> {
  const response = await api.post<SbisDocument>(`${SBIS_BASE}/documents/${id}/restore`);
  return response.data;
}

export type ServiceAgreement = {
  id: string;
  title: string;
  monthly_amount: number;
  dds_article_id: string | null;
  dds_article_name: string | null;
  documents_mode: string;
  accrual_enabled: boolean;
  started_on: string;
  ended_on: string | null;
  note: string | null;
  is_active: boolean;
};

export async function getServiceAgreements(counterpartyId: string): Promise<ServiceAgreement[]> {
  const response = await api.get<ServiceAgreement[]>(
    `${BASE}/${counterpartyId}/service-agreements`,
  );
  return response.data;
}

export async function createServiceAgreement(
  counterpartyId: string,
  payload: {
    title: string;
    monthly_amount: number;
    dds_article_id: string;
    documents_mode: string;
    started_on: string;
  },
): Promise<ServiceAgreement> {
  const response = await api.post<ServiceAgreement>(
    `${BASE}/${counterpartyId}/service-agreements`,
    payload,
  );
  return response.data;
}

/** Закрытие датой, а не удаление: на договор ссылаются уже начисленные месяцы. */
export async function closeServiceAgreement(
  counterpartyId: string,
  agreementId: string,
  endedOn: string,
): Promise<ServiceAgreement> {
  const response = await api.post<ServiceAgreement>(
    `${BASE}/${counterpartyId}/service-agreements/${agreementId}/close`,
    { ended_on: endedOn },
  );
  return response.data;
}
