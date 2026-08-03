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
