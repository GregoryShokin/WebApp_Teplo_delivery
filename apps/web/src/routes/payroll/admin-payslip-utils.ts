// Чистые утилиты контура ведомости администрации (без JSX). Логика повторяет
// «Расчёты» (run-detail.tsx), где локальные аналоги не экспортируются.
import type { PayrollLine, PayrollPaymentMethod } from "@/lib/api";

export const PAYMENT_METHOD_OPTIONS: Array<{ value: PayrollPaymentMethod; label: string }> = [
  { value: "business_card", label: "Бизнес-карта" },
  { value: "cash", label: "Наличные" },
  { value: "transfer", label: "Перевод" },
  { value: "other", label: "Другое" },
];

export function paymentMethodLabel(method: PayrollPaymentMethod) {
  return PAYMENT_METHOD_OPTIONS.find((option) => option.value === method)?.label ?? "Другое";
}

export function isFinalStatus(status: string) {
  return status === "finalized" || status === "final";
}

export function moneyValue(value: number | string | null | undefined) {
  const numeric = Number(value ?? 0);
  return Number.isFinite(numeric) ? numeric : 0;
}

export function moneyInputValue(value: number | string | null | undefined) {
  return String(moneyValue(value));
}

export function parseMoneyInput(value: string) {
  const normalized = value.trim().replace(",", ".");
  if (!normalized) {
    return null;
  }
  const numeric = Number(normalized);
  return Number.isFinite(numeric) ? numeric : null;
}

export function normalizeMoney(value: number) {
  return Math.round(value * 100) / 100;
}

export function todayDateInputValue() {
  const value = new Date();
  value.setMinutes(value.getMinutes() - value.getTimezoneOffset());
  return value.toISOString().slice(0, 10);
}

export function formatDateTime(value: string | null) {
  if (!value) {
    return "—";
  }
  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "short",
    timeStyle: "short",
    timeZone: "Europe/Moscow",
  }).format(new Date(value));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

/** Начисление текущего периода для on_demand-строки (½ оклада, из components.proration). */
export function onDemandPeriodAccrual(line: PayrollLine): number {
  const components = (line.components ?? {}) as Record<string, unknown>;
  const proration = (components.proration ?? {}) as Record<string, unknown>;
  const raw = proration.accrual_amount;
  if (typeof raw === "number") {
    return raw;
  }
  const parsed = typeof raw === "string" ? Number(raw) : 0;
  return Number.isFinite(parsed) ? parsed : 0;
}

/** Число смен посудомойки (components.kind === "dishwasher_shifts"), иначе null. */
export function dishwasherShiftCount(line: PayrollLine): number | null {
  const components = line.components;
  if (!components || typeof components !== "object") {
    return null;
  }
  const record = components as Record<string, unknown>;
  if (record.kind !== "dishwasher_shifts") {
    return null;
  }
  const shifts = record.shifts;
  return typeof shifts === "number" ? shifts : Number(shifts ?? 0);
}

// «Уже выплачено банком»: сумма привязанных к сотруднику выплат из журнала ДДС,
// зачтённая в этой ведомости (уменьшает «К выплате»). Детализация — в components.
export function extractEmployeePayoutOffset(line: PayrollLine): number {
  const components = line.components;
  if (!components || typeof components !== "object") {
    return 0;
  }
  const raw = (components as Record<string, unknown>).employee_payout_offsets;
  if (!Array.isArray(raw)) {
    return 0;
  }
  return raw.reduce((sum: number, item) => {
    if (item && typeof item === "object") {
      return sum + Number((item as Record<string, unknown>).amount ?? 0);
    }
    return sum;
  }, 0);
}

export type Recovery = { advanceId: string; kind: string; amount: number };

export function extractRecoveries(line: PayrollLine): Recovery[] {
  const components = line.components;
  if (!components || typeof components !== "object") {
    return [];
  }
  const raw = (components as Record<string, unknown>).advance_recoveries;
  if (!Array.isArray(raw)) {
    return [];
  }
  const result: Recovery[] = [];
  for (const item of raw) {
    if (item && typeof item === "object") {
      const record = item as Record<string, unknown>;
      const advanceId = typeof record.advance_id === "string" ? record.advance_id : "";
      const kind = typeof record.kind === "string" ? record.kind : "";
      result.push({ advanceId, kind, amount: Number(record.amount ?? 0) });
    }
  }
  return result;
}

// «На руки»/«выплачено» — те же формулы, что в «Расчётах» (для администрации депозит = 0).
export function lineOnHand(line: PayrollLine) {
  return moneyValue(line.total_payable) + moneyValue(line.deposit_payout);
}

export function linePaidOnHand(line: PayrollLine) {
  const salaryPaid = moneyValue(line.paid_amount ?? 0);
  return salaryPaid + (line.payment_status === "paid" ? moneyValue(line.deposit_payout) : 0);
}

export type AdjustmentComponent = {
  id: string;
  workDate: string;
  category: string;
  amount: number;
  comment: string | null;
};

function adjustmentItems(value: unknown): AdjustmentComponent[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter(isRecord).map((item) => ({
    id: String(item.id ?? ""),
    workDate: String(item.work_date ?? ""),
    category: String(item.category ?? "Корректировка"),
    amount: Number(item.amount ?? 0),
    comment: typeof item.comment === "string" && item.comment ? item.comment : null,
  }));
}

export function lineAdjustments(line: PayrollLine) {
  const adjustments = isRecord(line.components.adjustments) ? line.components.adjustments : {};
  return {
    bonuses: adjustmentItems(adjustments.bonuses),
    penalties: adjustmentItems(adjustments.penalties),
  };
}

type LineComponentMoneyItem = { kind: string; amount: number };

function lineComponentMoneyItems(line: PayrollLine, key: string): LineComponentMoneyItem[] {
  const value = isRecord(line.components) ? line.components[key] : undefined;
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter(isRecord).map((item) => ({
    kind: String(item.kind ?? ""),
    amount: moneyValue(item.amount as number | string | null | undefined),
  }));
}

function flowAmountsByKind(items: LineComponentMoneyItem[]) {
  return items.reduce(
    (totals, item) => {
      const key = item.kind === "advance" || item.kind === "loan" ? item.kind : "unspecified";
      totals[key] += item.amount;
      return totals;
    },
    { advance: 0, loan: 0, unspecified: 0 },
  );
}

export function lineSalaryBeforeSettlement(line: PayrollLine) {
  const salaryDeductions = Math.max(
    0,
    moneyValue(line.deduction) - moneyValue(line.deposit_withholding),
  );
  return normalizeMoney(
    moneyValue(line.base_pay) +
      moneyValue(line.percent_pay) +
      moneyValue(line.premium) +
      moneyValue(line.vacation_pay) -
      salaryDeductions -
      moneyValue(line.ndfl_withheld),
  );
}

export function lineSettlementFlows(line: PayrollLine) {
  const issuances = lineComponentMoneyItems(line, "advance_issuances");
  const recoveries = lineComponentMoneyItems(line, "advance_recoveries");
  const payoutOffsets = lineComponentMoneyItems(line, "employee_payout_offsets");
  const issuedByKind = flowAmountsByKind(issuances);
  const recoveredByKind = flowAmountsByKind(recoveries);
  const detailedIssued = issuedByKind.advance + issuedByKind.loan + issuedByKind.unspecified;

  return {
    advanceIssued: issuedByKind.advance,
    loanIssued: issuedByKind.loan,
    unspecifiedIssued: normalizeMoney(
      issuedByKind.unspecified + Math.max(0, moneyValue(line.advance_issued) - detailedIssued),
    ),
    advanceRecovered: recoveredByKind.advance,
    loanRecovered: recoveredByKind.loan,
    unspecifiedRecovered: recoveredByKind.unspecified,
    previouslyPaid: normalizeMoney(payoutOffsets.reduce((sum, item) => sum + item.amount, 0)),
  };
}
