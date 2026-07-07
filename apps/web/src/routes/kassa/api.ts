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
  // Возврат(ы) по этой покупке, уже пришедшие в выписку (refundIn с тем же rrn).
  refund_amount: number | null;
  refund_count: number;
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
  is_return: boolean; // позиция возвращена (красная строка, не проведена)
};

export type Cheque = {
  id: string;
  number: string | null;
  counterparty_id: string;
  counterparty_name: string;
  issued_at: string | null;
  amount: number; // проведено (net, без возвращённых позиций)
  returned_total: number; // сумма возвращённых позиций
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
  // Позиция возвращена в магазин: остаётся в чеке (сверка «копейка в копейку»),
  // но не проводится — чек проводится net.
  is_return?: boolean;
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

// --- кассовый журнал и «Выплата из кассы» (ТК Черникова) -------------------------

// Маршрут выплаты по статье: сотрудниковые статьи ведут в авансовый леджер,
// предоплата — в дебиторку поставщика, прочие — голая расходная проводка.
export type KassaPayoutFlow =
  | "expense"
  | "employee_advance"
  | "employee_loan"
  | "supplier_prepayment";

// Маршрут строки журнала: выплаты + приход по пресету («Внесение в кассу»).
export type KassaJournalFlow = KassaPayoutFlow | "payin";

export type KassaJournalItem = {
  id: string;
  operation_date: string;
  direction: "in" | "out";
  amount: number;
  article_id: string | null;
  article_name: string | null;
  purpose: string | null;
  comment: string | null;
  source_kind: string;
  counterparty_label: string | null;
  employee_id: string | null;
  counterparty_id: string | null;
  kassa_flow: KassaJournalFlow | null;
  editable: boolean;
  created_at: string;
};

export type KassaJournal = {
  wallet_name: string;
  balance: number;
  period_in: number;
  period_out: number;
  // Шапка «в кассе N ₽ · из них целевые M ₽» + бейдж вкладки «К выдаче».
  targets_total: number;
  pending_count: number;
  items: KassaJournalItem[];
};

export type KassaPayoutArticle = {
  id: string;
  code: string;
  name: string;
  flow: KassaPayoutFlow;
};

export type KassaPayoutEmployee = {
  id: string;
  full_name: string;
  position: string | null;
};

export type KassaPayoutContext = {
  wallet_name: string;
  balance: number;
};

export type KassaPayoutPayload = {
  article_id: string;
  amount: number;
  comment?: string | null;
  employee_id?: string | null;
  counterparty_id?: string | null;
};

export type KassaPayoutResult = {
  kind: KassaPayoutFlow;
  transaction_id: string | null;
  advance_id: string | null;
  prepayment_id: string | null;
};

export async function getKassaJournal(params: {
  date_from?: string;
  date_to?: string;
  direction?: "in" | "out";
  search?: string;
}): Promise<KassaJournal> {
  const response = await api.get<KassaJournal>(`${BASE}/journal`, { params });
  return response.data;
}

export async function getKassaPayoutArticles(): Promise<KassaPayoutArticle[]> {
  const response = await api.get<KassaPayoutArticle[]>(`${BASE}/payout-articles`);
  return response.data;
}

export async function getKassaPayoutEmployees(): Promise<KassaPayoutEmployee[]> {
  const response = await api.get<KassaPayoutEmployee[]>(`${BASE}/payout-employees`);
  return response.data;
}

export async function getKassaPayoutContext(): Promise<KassaPayoutContext> {
  const response = await api.get<KassaPayoutContext>(`${BASE}/payout-context`);
  return response.data;
}

export async function createKassaPayout(payload: KassaPayoutPayload): Promise<KassaPayoutResult> {
  const response = await api.post<KassaPayoutResult>(`${BASE}/payouts`, payload);
  return response.data;
}

export async function updateKassaPayout(
  transactionId: string,
  payload: KassaPayoutPayload,
): Promise<KassaPayoutResult> {
  const response = await api.patch<KassaPayoutResult>(
    `${BASE}/payouts/${transactionId}`,
    payload,
  );
  return response.data;
}

export async function deleteKassaPayout(transactionId: string): Promise<void> {
  await api.delete(`${BASE}/payouts/${transactionId}`);
}

// --- вкладка «К выдаче»: целёвки в кассе + разрешения на авансы/займы ------------

export type KassaTarget = {
  id: string;
  article_id: string | null;
  article_name: string | null;
  counterparty_id: string | null;
  counterparty_name: string | null;
  // Происхождение (например, накладные закупа) — из назначения целёвки.
  purpose: string | null;
  amount: number;
  amount_paid: number;
  outstanding: number;
  // Авто-целёвка оплаченного банковского черновика закупа.
  from_bank_payout: boolean;
  created_at: string;
};

export type KassaAdvancePermission = {
  id: string;
  employee_id: string;
  employee_name: string;
  kind: "advance" | "loan";
  amount: number;
  comment: string | null;
  created_by_label: string | null;
  created_at: string;
};

/** Внештатник с непогашенным: одна строка (имя + Σ неоплаченных смен открытого периода). */
export type KassaFreelancer = {
  employee_id: string;
  name: string;
  unpaid_total: number;
  shift_count: number;
};

/** Смена внештатника открытого периода (для модалки): единица = явка. */
export type KassaFreelancerShift = {
  attendance_entry_id: string;
  work_date: string;
  hours: number;
  amount: number;
  paid: boolean;
};

export type KassaPending = {
  wallet_name: string;
  balance: number;
  targets: KassaTarget[];
  permissions: KassaAdvancePermission[];
  freelancers: KassaFreelancer[];
  targets_total: number;
  pending_count: number;
};

export type KassaFreelancerSyncReport = {
  freelancers: KassaFreelancer[];
};

export async function getKassaPending(): Promise<KassaPending> {
  const response = await api.get<KassaPending>(`${BASE}/pending`);
  return response.data;
}

/** Смены внештатника открытого периода (для модалки выдачи). */
export async function getKassaFreelancerShifts(
  employeeId: string,
): Promise<KassaFreelancerShift[]> {
  const response = await api.get<{ shifts: KassaFreelancerShift[] }>(
    `${BASE}/freelancer-shifts/${employeeId}`,
  );
  return response.data.shifts;
}

/** «Синхронизировать смены»: перечитать явки текущей недели из iiko + вернуть список. */
export async function syncKassaFreelancerShifts(): Promise<KassaFreelancerSyncReport> {
  const response = await api.post<KassaFreelancerSyncReport>(`${BASE}/freelancer-shifts/sync`);
  return response.data;
}

/** «Выплатить»: выдать выбранные смены (явки) внештатника целиком (движок пишет статью ЗП). */
export async function payKassaFreelancerShifts(
  attendanceEntryIds: string[],
): Promise<KassaPending> {
  const response = await api.post<KassaPending>(`${BASE}/freelancer-shifts/payout`, {
    attendance_entry_ids: attendanceEntryIds,
  });
  return response.data;
}

/** «Выдано»: расход целёвки наличными из кассы (частично можно, сверх остатка — 409). */
export async function payKassaTarget(
  allocationId: string,
  amount: number,
): Promise<KassaPending> {
  const response = await api.post<KassaPending>(`${BASE}/targets/${allocationId}/payout`, {
    amount,
  });
  return response.data;
}

/** «Выплачено»: исполнить разрешение на аванс/заём (только вся сумма). */
export async function disburseKassaAdvancePermission(advanceId: string): Promise<KassaPending> {
  const response = await api.post<KassaPending>(
    `${BASE}/advance-permissions/${advanceId}/disburse`,
  );
  return response.data;
}

/** Отменить разрешение со стороны кассы — создатель увидит «отменено кассой». */
export async function cancelKassaAdvancePermission(advanceId: string): Promise<KassaPending> {
  const response = await api.post<KassaPending>(
    `${BASE}/advance-permissions/${advanceId}/cancel`,
  );
  return response.data;
}

// --- «Внесение в кассу» по пресетам ---------------------------------------------

/** Пресет внесения для формы кассира: имя + шаблон комментария (без статьи ДДС). */
export type KassaPayinPresetOption = {
  id: string;
  name: string;
  comment_template: string | null;
};

export type KassaPayinResult = {
  transaction_id: string | null;
  preset_id: string | null;
};

export async function getKassaPayinPresets(): Promise<KassaPayinPresetOption[]> {
  const response = await api.get<KassaPayinPresetOption[]>(`${BASE}/payin-presets`);
  return response.data;
}

export async function createKassaPayin(payload: {
  preset_id: string;
  amount: number;
  comment?: string | null;
}): Promise<KassaPayinResult> {
  const response = await api.post<KassaPayinResult>(`${BASE}/payins`, payload);
  return response.data;
}

export async function updateKassaPayin(
  transactionId: string,
  payload: { amount: number; comment?: string | null },
): Promise<KassaPayinResult> {
  const response = await api.patch<KassaPayinResult>(`${BASE}/payins/${transactionId}`, payload);
  return response.data;
}

export async function deleteKassaPayin(transactionId: string): Promise<void> {
  await api.delete(`${BASE}/payins/${transactionId}`);
}

// --- каталог пресетов внесения (Настройки → Касса) ------------------------------

export type KassaPayinPreset = {
  id: string;
  name: string;
  mechanism: string;
  article_id: string;
  article_name: string;
  counterparty_id: string | null;
  counterparty_name: string | null;
  comment_template: string | null;
  is_active: boolean;
  sort_order: number;
};

export type KassaPayinPresetArticle = {
  id: string;
  code: string;
  name: string;
  activity_type: string;
};

export type KassaPayinPresetCounterparty = {
  id: string;
  name: string;
};

export type KassaPayinPresetPayload = {
  name: string;
  article_id: string;
  counterparty_id?: string | null;
  comment_template?: string | null;
  is_active?: boolean;
  sort_order?: number;
};

export async function getKassaPayinPresetsAll(): Promise<KassaPayinPreset[]> {
  const response = await api.get<KassaPayinPreset[]>(`${BASE}/payin-presets/all`);
  return response.data;
}

export async function getKassaPayinPresetArticles(): Promise<KassaPayinPresetArticle[]> {
  const response = await api.get<KassaPayinPresetArticle[]>(`${BASE}/payin-preset-articles`);
  return response.data;
}

export async function getKassaPayinPresetCounterparties(): Promise<
  KassaPayinPresetCounterparty[]
> {
  const response = await api.get<KassaPayinPresetCounterparty[]>(
    `${BASE}/payin-preset-counterparties`,
  );
  return response.data;
}

export async function createKassaPayinPreset(
  payload: KassaPayinPresetPayload,
): Promise<KassaPayinPreset> {
  const response = await api.post<KassaPayinPreset>(`${BASE}/payin-presets`, payload);
  return response.data;
}

export async function updateKassaPayinPreset(
  presetId: string,
  payload: KassaPayinPresetPayload,
): Promise<KassaPayinPreset> {
  const response = await api.patch<KassaPayinPreset>(`${BASE}/payin-presets/${presetId}`, payload);
  return response.data;
}

export async function deleteKassaPayinPreset(presetId: string): Promise<void> {
  await api.delete(`${BASE}/payin-presets/${presetId}`);
}
