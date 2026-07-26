/** Контракт страницы «Налоги» — запросы к /api/v1/taxes/*.
 *
 * Типы зеркалят `app/schemas/taxes.py` один в один. Два места, где легко ошибиться:
 *
 * * деньги приходят из `Decimal` — pydantic может отдать их и числом, и строкой, поэтому
 *   денежный тип `Money = number | string`, а приведение живёт в одном месте (index.tsx);
 * * `null` и ноль в сверке — РАЗНЫЕ вещи: `null` значит «источника нет» (платёжки не было),
 *   ноль — «источник есть и в нём ноль». Ради второго случая модуль и строился, поэтому
 *   опциональность `calculated` / `documented` / `paid` не схлопывается в 0.
 */
import { api } from "@/lib/api";

const BASE = "/taxes";

export type Money = number | string;

// ── Расчёт: TaxStateRead ───────────────────────────────────────────────────────

export type TaxState = {
  year: number;
  as_of: string;
  period_code: string;

  income_ytd: Money;
  tax_computed: Money;

  /** Вычет А — взносы за работников, только по факту уплаты. */
  employees_paid: Money;
  /** Вычет Б — фиксированные взносы ИП «за себя». */
  fixed_available: Money;
  fixed_claimed: Money;
  /** Вычет В — допвзнос 1% с дохода свыше 300 000 ₽. */
  extra_accrued: Money;
  extra_available: Money;
  extra_claimed: Money;
  extra_prior_available: Money;
  extra_prior_claimed: Money;

  deduction_total: Money;
  deduction_limit: Money;
  deduction_applied: Money;
  /** Срезано лимитом 50% — сгорело навсегда. */
  deduction_burned: Money;
  /** Не заявлено — внутри года ещё можно использовать. */
  deduction_unclaimed: Money;
  /** Начислено, но не уплачено — уйдёт в вычет будущих периодов года. */
  deduction_deferred: Money;

  advances_paid: Money;
  amount_due: Money;
  overpayment: Money;

  total_burden: Money;
  /** Доля, а не проценты: 0.0512 = 5,12%. */
  effective_rate: Money;

  input_fingerprint: string;
  /** Готовые русские формулировки движка — показываем как есть, не переписывая. */
  warnings: string[];
  blocking: string[];
};

// ── Сверка: ReconciliationRead ─────────────────────────────────────────────────

export type ReconVerdict =
  | "ok"
  | "doc_mismatch"
  | "payment_mismatch"
  | "overdue"
  | "due"
  | "no_data";

export type ReconSeverity = "ok" | "info" | "warning" | "alert";

export type ReconLine = {
  label: string;
  tax_kind: string;
  period_code: string;
  due_date: string | null;
  /** Что насчитал движок. */
  calculated: Money | null;
  /** Что сказала платёжка бухгалтера. */
  documented: Money | null;
  /** Что реально ушло из банка. */
  paid: Money | null;
  verdict: ReconVerdict;
  severity: ReconSeverity;
  messages: string[];
  /** Короткий императив «что делать» — показывается в строке таблицы. */
  action?: string | null;
  /** Причина и суммы к действию — показываются под иконкой «i». */
  action_why?: string | null;
  /** Сколько платить по обязательству (для кнопки «Отправить в банк»); null — платить нечего. */
  payable_amount?: Money | null;
  /** Платёж уже в работе: 'ready_to_send' | 'in_bank' — кнопка отправки не показывается. */
  draft_status?: string | null;
};

export type Reconciliation = {
  year: number;
  as_of: string;
  lines: ReconLine[];
  alert_count: number;
  has_alerts: boolean;
};

/** Первый экран: расчёт и сверка одним ответом — обязаны быть на одну дату среза. */
export type TaxOverview = {
  as_of: string;
  year: number;
  period_code: string;
  state: TaxState;
  reconciliation: Reconciliation;
  alert_count: number;
  /** Выручка загружена не за весь период — цифра не окончательная. */
  is_blocked: boolean;
};

// ── Календарь: TaxCalendarRead ─────────────────────────────────────────────────

export type TaxPaymentStatus = "planned" | "paid" | "cancelled";

export type TaxCalendarItem = {
  id: string;
  bundle_id: string;
  kind: string;
  amount: Money;
  recipient: string;
  for_year: number;
  for_period: string | null;
  status: TaxPaymentStatus;
  /** У плановой строки — срок уплаты, у уплаченной — дата списания. */
  paid_on: string;
  /** Заполнен только у плановых строк. */
  due_date: string | null;
  /** Срок прошёл, а деньги не ушли. Считается на «сегодня», не на дату среза. */
  is_overdue: boolean;
  source_kind: string;
  quality_status: string;
  document_number: string | null;
  purpose: string | null;
  note: string | null;
};

export type TaxCalendar = {
  year: number;
  today: string;
  items: TaxCalendarItem[];
  planned_total: Money;
  paid_total: Money;
  overdue_total: Money;
  overdue_count: number;
};

// ── Реестр платежей: TaxPaymentListRead ────────────────────────────────────────

export type TaxPaymentRow = {
  id: string;
  bundle_id: string;
  paid_on: string;
  kind: string;
  amount: Money;
  recipient: string;
  for_year: number;
  for_period: string | null;
  status: TaxPaymentStatus;
  source_kind: string;
  /** 'reconstructed' — разнос выведен расчётом, а не взят из уведомления. */
  quality_status: string;
  document_number: string | null;
  purpose: string | null;
  note: string | null;
  cashflow_transaction_id: string | null;
  bank_operation_id: string | null;
};

/** Журнал ФАКТИЧЕСКИХ уплат: только реальные списания из банка. Планы — в календаре. */
export type TaxPaymentList = {
  year: number;
  items: TaxPaymentRow[];
  total: number;
  paid_total: Money;
  /** Уплачено по видам платежа: ключ — kind. */
  totals_by_kind: Record<string, Money>;
};

// ── Документы бухгалтера: TaxDocumentListRead ──────────────────────────────────

export type TaxDocumentStatus =
  | "parsed"
  | "needs_review"
  | "promoted"
  | "unsupported"
  | "error"
  | "ignored";

export type TaxDocumentType =
  | "payment_order"
  | "payroll_statement"
  | "turnover_statement"
  | "unknown";

/** Строка сотрудника в раскладке оборотки (лист 'л1'). `null` ≠ 0 — ячейка пуста. */
export type TaxTurnoverRow = {
  tab_number?: string | null;
  employee?: string | null;
  oklad?: Money | null;
  days?: Money | null;
  accrued?: Money | null;
  ndfl?: Money | null;
  advance?: Money | null;
  contributions?: Money | null;
  injury?: Money | null;
  deduction?: Money | null;
  to_pay?: Money | null;
  /** Для платёжной ведомости Т-53 строка несёт одну сумму `amount`. */
  amount?: Money | null;
};

/** Результат разбора отдаётся как есть (JSONB) — фиксируем только читаемые нами поля. */
export type TaxRecognition = {
  tax_kind?: string | null;
  amount?: Money | null;
  due_date?: string | null;
  period_hint?: string | null;
  kbk?: string | null;
  recipient?: string | null;
  document_number?: string | null;
  /** Ведомость Т-53: номер, тип выплаты (advance/salary), расчётный период, итог. */
  doc_number?: string | null;
  payout_kind?: string | null;
  period_start?: string | null;
  period_end?: string | null;
  total?: Money | null;
  /** Оборотка: месяц/год и помесячные итоги по колонкам. */
  year?: number | null;
  month?: number | null;
  accrued_total?: Money | null;
  ndfl_total?: Money | null;
  contributions_total?: Money | null;
  injury_total?: Money | null;
  rows?: TaxTurnoverRow[];
  /** Почему документ ушёл в needs_review — русский текст, показываем владельцу. */
  review_reasons?: string[];
  /** Почему документ «не поддержан» (кадровый, нечитаемый формат) — русский текст. */
  reason?: string | null;
  /** След ИИ-разбора: объяснение и ПРЕДЛОЖЕНИЕ полей (ИИ сам ничего не применяет). */
  ai_review?: {
    summary: string;
    confidence: number;
    document_type?: string | null;
    needs_human: boolean;
    reasons?: string[];
    /** Предложенные поля платёжки — применяются кнопкой «Применить» в окне разбора. */
    proposal?: Record<string, string> | null;
    /** Владелец подтвердил предложение — поля применены. */
    applied?: boolean;
    applied_at?: string;
    model?: string;
    at?: string;
  } | null;
  /** Кто проверял документ: 'ai_confirmed' — предложил ИИ, подтвердил владелец. */
  reviewed_by?: string | null;
  [key: string]: unknown;
};

export type TaxDocumentRow = {
  id: string;
  mailbox: string;
  from_addr: string | null;
  subject: string | null;
  received_at: string | null;
  filename: string | null;
  mime: string | null;
  document_type: TaxDocumentType | string;
  status: TaxDocumentStatus | string;
  recognition: TaxRecognition;
  error: string | null;
  tax_payment_bundle_id: string | null;
  created_at: string | null;
  /** Есть ли сохранённый исходный файл, который можно открыть кнопкой. */
  has_file: boolean;
};

export type TaxDocumentList = {
  items: TaxDocumentRow[];
  total: number;
  /** Счётчики по ВСЕЙ таблице, а не по отфильтрованной выборке. */
  status_counts: Record<string, number>;
};

export type PromotionAction = "created" | "updated" | "skipped";

export type PromotionResult = {
  intake_id: string;
  tax_payment_id: string | null;
  action: PromotionAction | string;
  /** Причина пропуска — русский текст, годный к показу без перевода. */
  reason: string | null;
};

export type TaxPromotionSummary = {
  created: number;
  updated: number;
  skipped: number;
  results: PromotionResult[];
};

// ── Реестр источников: TaxSourcesRead ──────────────────────────────────────────

export type TaxSourceMethod = "api" | "file_cache" | "derived" | "manual" | "code_constant";
export type TaxSourceConfidence = "verified" | "reconstructed" | "unverified" | "missing";

export type TaxSource = {
  key: string;
  title: string;
  system: string;
  method: TaxSourceMethod | string;
  confidence: TaxSourceConfidence | string;
  note: string;
  /** Чем источник должен стать. Пусто — менять не планируем. */
  target: string;
};

export type TaxSources = {
  version: number;
  sources: TaxSource[];
  /** Входы с доверием reconstructed / missing — граница доверия к цифре. */
  weakest_links: TaxSource[];
};

// ── Запросы ────────────────────────────────────────────────────────────────────

export async function getTaxOverview(asOf?: string): Promise<TaxOverview> {
  const response = await api.get<TaxOverview>(`${BASE}/overview`, {
    params: asOf ? { as_of: asOf } : undefined,
  });
  return response.data;
}

export async function getTaxReconciliation(asOf?: string): Promise<Reconciliation> {
  const response = await api.get<Reconciliation>(`${BASE}/reconciliation`, {
    params: asOf ? { as_of: asOf } : undefined,
  });
  return response.data;
}

export async function getTaxCalendar(year?: number): Promise<TaxCalendar> {
  const response = await api.get<TaxCalendar>(`${BASE}/calendar`, {
    params: year ? { year } : undefined,
  });
  return response.data;
}

export async function getTaxPayments(params?: {
  year?: number;
  kind?: string;
}): Promise<TaxPaymentList> {
  const response = await api.get<TaxPaymentList>(`${BASE}/payments`, { params });
  return response.data;
}

export async function getTaxDocuments(params?: {
  status?: TaxDocumentStatus;
  limit?: number;
}): Promise<TaxDocumentList> {
  const response = await api.get<TaxDocumentList>(`${BASE}/documents`, { params });
  return response.data;
}

export async function promoteTaxDocuments(): Promise<TaxPromotionSummary> {
  const response = await api.post<TaxPromotionSummary>(`${BASE}/documents/promote`);
  return response.data;
}

export async function refreshTaxDocuments(): Promise<Record<string, number | string>> {
  // Живой IMAP-проход по ящикам бухгалтера медленнее обычного запроса — даём ему свой
  // увеличенный таймаут, иначе клиент оборвётся (15 c по умолчанию) ложной ошибкой, хотя
  // сервер проход довёл и документы уже сохранены.
  const response = await api.post<Record<string, number | string>>(
    `${BASE}/documents/refresh`,
    undefined,
    { timeout: 120_000 },
  );
  return response.data;
}

/** Ручная дозаправка нераспознанных полей платёжки при проверке (пустое — как распознано). */
export type TaxReviewOverrides = {
  tax_kind?: string | null;
  amount?: number | null;
  due_date?: string | null;
  period_hint?: string | null;
};

export async function reviewTaxDocument(
  intakeId: string,
  status: "parsed" | "ignored",
  overrides?: TaxReviewOverrides,
): Promise<TaxDocumentRow> {
  const response = await api.post<TaxDocumentRow>(
    `${BASE}/documents/${intakeId}/review`,
    { status, ...(overrides ?? {}) },
  );
  return response.data;
}

/** Исходный файл документа под авторизацией — тянем blob (прямую ссылку без токена API не
 *  отдаст) и возвращаем object-URL. Открытие/скачивание делает вызывающий: окно надо открыть
 *  синхронно по клику, иначе попап блокируется браузером после await. */
export async function fetchTaxDocumentFileUrl(intakeId: string): Promise<string> {
  const response = await api.get(`${BASE}/documents/${intakeId}/file`, {
    responseType: "blob",
  });
  return URL.createObjectURL(response.data as Blob);
}

/** Результат ИИ-разбора одного документа. Предложение применяет владелец, не ИИ. */
export type AiDocumentReview = {
  intake_id: string;
  filename: string;
  summary: string;
  confidence: number;
  document_type: string | null;
  proposal: Record<string, string> | null;
  needs_human: boolean;
  reasons: string[];
};

export type AiAuditFinding = {
  severity: "info" | "warning" | "alert" | string;
  title: string;
  detail: string;
};

export type AiAuditReport = {
  verdict: string;
  findings: AiAuditFinding[];
  documents: AiDocumentReview[];
};

export async function aiReviewTaxDocument(intakeId: string): Promise<AiDocumentReview> {
  // Модель думает секунды-десятки секунд — обычного клиентского таймаута мало.
  const response = await api.post<AiDocumentReview>(
    `${BASE}/documents/${intakeId}/ai-review`,
    undefined,
    { timeout: 120_000 },
  );
  return response.data;
}

/** «Да, делай»: применить сохранённое предложение ИИ (та же валидация, что у ручной проверки). */
export async function aiApplyTaxProposal(intakeId: string): Promise<TaxDocumentRow> {
  const response = await api.post<TaxDocumentRow>(`${BASE}/documents/${intakeId}/ai-apply`);
  return response.data;
}

export async function aiReviewAllTaxes(): Promise<AiAuditReport> {
  // Полная ревизия — несколько документов подряд + общий аудит: даём щедрый запас.
  const response = await api.post<AiAuditReport>(`${BASE}/ai-review`, undefined, {
    timeout: 300_000,
  });
  return response.data;
}

export async function getTaxSources(): Promise<TaxSources> {
  const response = await api.get<TaxSources>(`${BASE}/sources`);
  return response.data;
}

// ── Зарплатный критерий льготы по НДС ──────────────────────────────────────────

export type VatMonthAccrual = {
  month: number;
  accrued: Money;
  oklad: Money | null;
  /** Начислено = оклад → месяц отработан полностью (репрезентативен для показателя). */
  full_month: boolean;
};

export type VatWageCriterion = {
  year: number;
  active_employee: string | null;
  active_tab: string | null;
  months: VatMonthAccrual[];
  /** Средняя по полным месяцам (основной показатель) и по всем месяцам с выплатами. */
  indicator_full: Money | null;
  indicator_all: Money | null;
  threshold: Money | null;
  /** Проходит ли критерий. null — порог не введён или показателя нет. */
  passes: boolean | null;
  margin: Money | null;
  messages: string[];
};

export async function getVatCriterion(year?: number): Promise<VatWageCriterion> {
  const response = await api.get<VatWageCriterion>(`${BASE}/vat-criterion`, {
    params: year ? { year } : undefined,
  });
  return response.data;
}

export async function setVatThreshold(
  year: number,
  amount: number,
): Promise<VatWageCriterion> {
  const response = await api.post<VatWageCriterion>(`${BASE}/vat-criterion/threshold`, {
    year,
    amount,
  });
  return response.data;
}

// ── Черновик платёжки в банк ───────────────────────────────────────────────────

export type TaxBankDraftResult = {
  document_id: string;
  status: string;
  provider_ref: string | null;
};

export async function createTaxBankDraft(
  amount: number,
  purpose?: string,
): Promise<TaxBankDraftResult> {
  const response = await api.post<TaxBankDraftResult>(`${BASE}/bank-draft`, {
    amount,
    purpose,
  });
  return response.data;
}

// ── Налоговый платёж в очереди «Активные платежи» ──────────────────────────────

export type TaxPaymentDraft = {
  id: string;
  tax_kind: string;
  for_year: number | null;
  for_period: string | null;
  title: string | null;
  amount: Money;
  purpose: string;
  due_date: string | null;
  kbk: string | null;
  recipient_name: string | null;
  status: string;
  bank_provider: string;
  document_id: string | null;
  provider_ref: string | null;
  last_error: string | null;
  created_at: string | null;
};

/** «Отправить в банк» на «Налогах»: подготовить платёж в окно активных платежей. */
export async function createTaxPaymentDraft(payload: {
  tax_kind: string;
  amount: number;
  purpose?: string | null;
  for_year?: number | null;
  for_period?: string | null;
  due_date?: string | null;
  title?: string | null;
}): Promise<TaxPaymentDraft> {
  const response = await api.post<TaxPaymentDraft>(`${BASE}/payment-drafts`, payload);
  return response.data;
}

/** Отправить подготовленный платёж в банк (сумму/назначение можно поправить). */
export async function sendTaxPaymentDraftToBank(
  draftId: string,
  payload: { amount?: number | null; purpose?: string | null },
): Promise<TaxPaymentDraft> {
  const response = await api.post<TaxPaymentDraft>(
    `${BASE}/payment-drafts/${draftId}/send-to-bank`,
    payload,
  );
  return response.data;
}
