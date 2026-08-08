/** Контракт страницы «Баланс» — запросы к /api/v1/balance.
 *
 * Типы зеркалят `app/api/v1/routes/balance.py`. Два различия, которые нельзя схлопывать:
 *
 * `amount` может быть `null`, и это НЕ ноль. `null` означает «величина неизвестна»: регистра
 * нет, снимок не собрался, строка ждёт человека. Ноль печатается только со статусом
 * `zero_confirmed` — когда владелец подтвердил, что величины нет (кредитов у бизнеса нет).
 * Свести оба случая к нулю значит соврать одинаково там, где правда разная.
 *
 * `status='plug'` у капитала — это не «посчитано», а «выведено как остаток». Строка обязана
 * читаться иначе прочих, иначе равенство «актив = пассив» примут за проверку, хотя оно
 * тождественно по построению.
 *
 * Суммы приходят СТРОКАМИ: json-число проходит через float и теряет копейки. Страница их
 * форматирует, но не складывает — арифметика уже сделана на сервере.
 */
import { api } from "@/lib/api";

export type BalanceLineStatus =
  | "ok"
  | "approximate"
  | "manual"
  | "zero_confirmed"
  | "not_applicable"
  | "missing"
  | "plug";

export type BalanceLine = {
  line_code: string;
  title: string;
  block: "asset" | "liability" | "equity";
  group_code: string;
  sort_order: number;
  sign: number;
  kind: "source" | "memo" | "subtotal";
  amount: string | null;
  status: BalanceLineStatus;
  source_kind: string | null;
  source_note: string | null;
  detail: Record<string, unknown> | null;
};

export type BalanceFlag = {
  code: string;
  severity: "blocking" | "warning" | "info";
  title: string;
  explanation: string;
  count: number;
  amount: string;
  rows: Record<string, unknown>[] | null;
};

export type BalanceReconciliation = {
  delta_equity: string;
  expected: string;
  residual: string;
  tolerance: string;
  explained: boolean;
  net_profit: string | null;
  net_profit_status: string | null;
  dividends: string;
  contributions: string;
  returns: string;
};

export type BalanceView = {
  as_of: string;
  period_month: string;
  status: "preview" | "draft" | "final";
  method: "inventory" | "rolling";
  asset_total: string;
  liability_total: string;
  equity_plug: string;
  lines: BalanceLine[];
  flags: BalanceFlag[];
  reconciliation: BalanceReconciliation | null;
  residual_reason: string | null;
  finalized_at: string | null;
  note: string | null;
};

export type BalanceSnapshotRow = {
  as_of: string;
  status: "draft" | "final";
  method: "inventory" | "rolling";
  asset_total: string;
  liability_total: string;
  equity_plug: string;
  recon_residual: string | null;
  finalized_at: string | null;
};

export type BalanceDriftRow = {
  line_code: string;
  title: string;
  frozen_amount: string | null;
  live_amount: string | null;
  delta: string;
};

export async function fetchBalancePreview(asOf: string): Promise<BalanceView> {
  const { data } = await api.get<BalanceView>("/balance/preview", { params: { as_of: asOf } });
  return data;
}

export async function fetchBalanceSnapshot(asOf: string): Promise<BalanceView> {
  const { data } = await api.get<BalanceView>(`/balance/snapshots/${asOf}`);
  return data;
}

export async function fetchBalanceSnapshots(): Promise<BalanceSnapshotRow[]> {
  const { data } = await api.get<BalanceSnapshotRow[]>("/balance/snapshots");
  return data;
}

export async function freezeBalance(asOf: string, note?: string): Promise<BalanceView> {
  const { data } = await api.post<BalanceView>("/balance/snapshots", {
    as_of: asOf,
    note: note ?? null,
  });
  return data;
}

export async function finalizeBalance(
  asOf: string,
  residualReason?: string,
): Promise<BalanceView> {
  const { data } = await api.post<BalanceView>(`/balance/snapshots/${asOf}/finalize`, {
    residual_reason: residualReason ?? null,
  });
  return data;
}

export async function fetchBalanceDrift(asOf: string): Promise<BalanceDriftRow[]> {
  const { data } = await api.get<BalanceDriftRow[]>(`/balance/snapshots/${asOf}/drift`);
  return data;
}

export async function setBalanceManualValue(
  asOf: string,
  lineCode: string,
  payload: { amount: string; reason: string; source_document?: string | null },
): Promise<void> {
  await api.put(`/balance/manual/${asOf}/${lineCode}`, payload);
}
