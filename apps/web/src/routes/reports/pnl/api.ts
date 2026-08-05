/** Контракт страницы «ОПиУ» — запрос к /api/v1/reports/pnl.
 *
 * Типы зеркалят `app/api/v1/routes/reports_pnl.py`. Главное, что нельзя схлопывать:
 * `amount` может быть `null`, и это НЕ ноль. `null` — цифры нет (источник не настроен,
 * зеркало iiko не заливалось, документ не пришёл); ноль печатается только при статусе
 * `zero_confirmed`, когда источник ответил и движения не было. Ради этого различия
 * строился весь отчёт, поэтому опциональность не превращается в `?? 0` нигде.
 *
 * Суммы приходят СТРОКАМИ: json-число проходит через float и теряет копейки на отчёте из
 * восьмидесяти строк. Страница их форматирует, а не считает — арифметика уже сделана на
 * сервере.
 */
import { api } from "@/lib/api";

export type LineStatus =
  | "ok"
  | "zero_confirmed"
  | "manual"
  | "waiting_document"
  | "overdue_document"
  | "no_data"
  | "not_configured"
  | "needs_review"
  | "not_used"
  | "before_accounting_start"
  | "incomplete";

export type PnlComponent = {
  stream: string;
  amount: string | null;
  status: LineStatus;
  excluded_amount: string;
  unrecognized_paid: string;
  note: string | null;
};

export type PnlLine = {
  code: string;
  title: string;
  block: string;
  kind: "source" | "memo" | "subtotal" | "ratio" | "stat" | "service";
  level: number;
  sign_role: number;
  month_basis: "document" | "cash" | "calendar";
  amount: string | null;
  status: LineStatus;
  pct_of_revenue: string | null;
  norm_min: string | null;
  norm_max: string | null;
  missing_lines: string[];
  source_note: string | null;
  drill_available: boolean;
  components: PnlComponent[];
};

export type PnlReconciliation = {
  cash_out_total: string;
  cash_in_total: string;
  by_verdict: Record<string, string>;
  unmapped: string;
  unmapped_count: number;
  balanced: boolean;
  drift: string;
};

export type PnlWarning = {
  code: string;
  message: string;
  line_code: string | null;
  amount: string | null;
};

export type PnlReport = {
  month: string;
  accounting_start: string;
  lines: PnlLine[];
  reconciliation: PnlReconciliation;
  warnings: PnlWarning[];
  quality: Record<string, string>;
};

/** Отчёт за месяц. `month` — строка ГГГГ-ММ: у отчёта нет дня. */
export async function fetchPnlReport(month: string): Promise<PnlReport> {
  // Отчёт считается на лету из шести контуров — глобальных 15 секунд ему мало на холодном
  // запросе. Пер-запросный override принят в проекте (налоги, ОС, контрагенты).
  const response = await api.get<PnlReport>("/reports/pnl", {
    params: { month },
    timeout: 60_000,
  });
  return response.data;
}

export type PayrollLedgerAmounts = {
  base_pay: string;
  percent_pay: string;
  bonuses: string;
  vacation_pay: string;
  fund_accrual: string;
  other_penalties: string;
  audit_penalties: string;
  fund_forfeited: string;
  deposit_written_off: string;
  salary_expense: string;
  fund_expense: string;
};

export type PayrollLedgerEmployee = PayrollLedgerAmounts & {
  employee_id: string | null;
  employee_name: string;
  roles: string[];
};

export type PayrollLedger = {
  month: string;
  totals: PayrollLedgerAmounts;
  employees: PayrollLedgerEmployee[];
};

export async function fetchPayrollLedger(month: string): Promise<PayrollLedger> {
  const response = await api.get<PayrollLedger>("/reports/pnl/ledgers/payroll", {
    params: { month },
    timeout: 60_000,
  });
  return response.data;
}

export type PartnerCommissionLedgerRow = {
  partner_name: string;
  revenue_amount: string;
  commission_rate: string;
  commission_amount: string;
  rows_count: number;
  source_ref: string;
  synced_at: string;
};

export type PartnerCommissionLedger = {
  month: string;
  revenue_amount: string | null;
  commission_amount: string | null;
  synced_at: string | null;
  rows: PartnerCommissionLedgerRow[];
};

export async function fetchPartnerCommissionLedger(
  month: string,
): Promise<PartnerCommissionLedger> {
  const response = await api.get<PartnerCommissionLedger>("/reports/pnl/ledgers/partners", {
    params: { month },
    timeout: 60_000,
  });
  return response.data;
}

export type GoodsLedgerSummary = {
  line_code: string;
  line_title: string;
  source_kind: GoodsSourceKind | "temporary";
  amount: string | null;
  details_amount: string;
  details_complete: boolean;
  /** Бывает ли у строки поимённая номенклатура. У результата инвентаризации её нет
   *  по построению — обещать её появление после синка нельзя. */
  details_expected: boolean;
  synced_at: string | null;
  opening_amount: string | null;
  receipts_amount: string | null;
  closing_amount: string | null;
  surplus_amount: string | null;
  shortage_amount: string | null;
};

export type GoodsSourceKind = "inventory" | "incoming_invoice";

export type GoodsLedgerRow = {
  line_code: string;
  line_title: string;
  source_kind: "inventory" | "incoming_invoice";
  product_guid: string;
  product_name: string;
  source_amount: string;
  amount: string;
  rows_count: number;
  opening_quantity: string | null;
  opening_amount: string | null;
  receipts_amount: string | null;
  closing_quantity: string | null;
  closing_amount: string | null;
  audit_date: string | null;
  surplus_amount: string;
};

export type GoodsLedger = {
  month: string;
  summaries: GoodsLedgerSummary[];
  rows: GoodsLedgerRow[];
};

export async function fetchGoodsLedger(month: string): Promise<GoodsLedger> {
  const response = await api.get<GoodsLedger>("/reports/pnl/ledgers/goods", {
    params: { month },
    timeout: 60_000,
  });
  return response.data;
}

export type GoodsClassificationStatus =
  "include" | "requires_owner_review" | "exclude" | "workup" | "stocked" | "unclassified";

export type GoodsClassificationOption = {
  line_code: string;
  line_title: string;
  source_kind: GoodsSourceKind;
  temporary: boolean;
};

export type GoodsClassificationSource = {
  source_kind: GoodsSourceKind;
  amount: string | null;
  rows_count: number;
  observed_in_month: boolean;
  last_seen_month: string | null;
};

export type GoodsClassificationRow = {
  product_guid: string;
  product_name: string;
  product_code: string | null;
  selected_source_kind: GoodsSourceKind | null;
  sources: GoodsClassificationSource[];
  line_code: string | null;
  line_title: string | null;
  status: GoodsClassificationStatus;
  revision_product: boolean;
  note: string | null;
  updated_at: string | null;
};

export type GoodsClassificationLedger = {
  month: string;
  attention_count: number;
  attention_amount: string;
  rules_count: number;
  options: GoodsClassificationOption[];
  rows: GoodsClassificationRow[];
};

export type GoodsClassificationDecision = {
  source_kind: GoodsSourceKind;
  status: Exclude<GoodsClassificationStatus, "unclassified">;
  line_code: string | null;
};

export async function fetchGoodsClassifications(month: string): Promise<GoodsClassificationLedger> {
  const response = await api.get<GoodsClassificationLedger>(
    "/reports/pnl/ledgers/goods/classifications",
    { params: { month }, timeout: 60_000 },
  );
  return response.data;
}

export async function updateGoodsClassification(
  month: string,
  row: Pick<GoodsClassificationRow, "product_guid" | "note">,
  decision: GoodsClassificationDecision,
): Promise<GoodsClassificationLedger> {
  const response = await api.patch<GoodsClassificationLedger>(
    `/reports/pnl/ledgers/goods/classifications/${row.product_guid}`,
    { ...decision, note: row.note },
    { params: { month }, timeout: 60_000 },
  );
  return response.data;
}

export type RecognitionLedgerTotals = {
  recognized: string;
  waiting_document: string;
  missing_period: string;
  without_primary: string;
  unattributed: string;
  unrecognized: string;
};

export type RecognitionLedgerRow = {
  status: "recognized" | "waiting_document" | "missing_period";
  source_kind: "accrual" | "prepayment" | "invoice";
  source_id: string;
  counterparty_id: string | null;
  counterparty_name: string;
  article_id: string | null;
  article_name: string;
  line_code: string | null;
  line_title: string | null;
  amount: string;
  event_date: string | null;
  service_period_start: string | null;
  service_period_end: string | null;
  has_primary: boolean | null;
  reason: string;
};

export type RecognitionLedger = {
  month: string;
  totals: RecognitionLedgerTotals;
  rows: RecognitionLedgerRow[];
};

export async function fetchRecognitionLedger(month: string): Promise<RecognitionLedger> {
  const response = await api.get<RecognitionLedger>("/reports/pnl/ledgers/recognition", {
    params: { month },
    timeout: 60_000,
  });
  return response.data;
}

/** Строка расшифровки. `kind` решает, как её читать, а не как покрасить. */
export type DrillRowKind = "included" | "waiting" | "excluded" | "info";

export type DrillRow = {
  title: string;
  subtitle: string | null;
  row_date: string | null;
  amount: string;
  kind: DrillRowKind;
};

export type DrillGroup = {
  stream: string;
  title: string;
  amount: string;
  note: string | null;
  /** Входит ли группа в итог строки. У «оплачено, документа нет» — нет. */
  counts_in_total: boolean;
  rows: DrillRow[];
};

export type PnlDrill = {
  line_code: string;
  line_title: string;
  month: string;
  total: string;
  undecomposed: string[];
  /** То, что прошло по строке, но её число не меняет и не изменит: платежи за другие
   *  периоды, переводы, списания фондов, накопленных до начала учёта. По одной строке на
   *  причину — таблицей они только отвлекают от того, ради чего расшифровку открыли. */
  asides: DrillAside[];
  groups: DrillGroup[];
};

export type DrillAside = {
  amount: string;
  count: number;
  reason: string;
};

/** Из чего сложилась строка: контрагенты, сотрудники, документы, направления. */
export async function fetchPnlLineDrill(line: string, month: string): Promise<PnlDrill> {
  const response = await api.get<PnlDrill>(`/reports/pnl/lines/${encodeURIComponent(line)}`, {
    params: { month },
    timeout: 60_000,
  });
  return response.data;
}
