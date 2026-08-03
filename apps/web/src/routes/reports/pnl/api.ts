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
  /** Платежи месяца, которые число строки не меняют и не изменят: закрывают документы
   *  других периодов, посчитаны другим модулём, вообще не расход. Показываются ОДНОЙ
   *  строкой — таблицей они только отвлекают от того, ради чего расшифровку открыли. */
  aside_amount: string;
  aside_count: number;
  groups: DrillGroup[];
};

/** Из чего сложилась строка: контрагенты, сотрудники, документы, направления. */
export async function fetchPnlLineDrill(line: string, month: string): Promise<PnlDrill> {
  const response = await api.get<PnlDrill>(`/reports/pnl/lines/${encodeURIComponent(line)}`, {
    params: { month },
    timeout: 60_000,
  });
  return response.data;
}
