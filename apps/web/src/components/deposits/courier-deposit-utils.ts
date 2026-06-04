import type {
  CourierDepositCategory,
  CourierDepositRow,
  CourierDepositStatusFilter,
  CourierDepositTransactionType,
} from "@/lib/api";

const moneyFormatter = new Intl.NumberFormat("ru-RU", {
  maximumFractionDigits: 0,
  minimumFractionDigits: 0,
});

const percentFormatter = new Intl.NumberFormat("ru-RU", {
  maximumFractionDigits: 1,
  minimumFractionDigits: 0,
});

export const COURIER_STATUS_LABELS: Record<CourierDepositStatusFilter, string> = {
  active: "Активные",
  fired: "Уволенные",
  all: "Все",
};

export const COURIER_CATEGORY_LABELS: Record<CourierDepositCategory | "all", string> = {
  primary: "Primary",
  secondary: "Secondary",
  all: "Все",
};

export const COURIER_TRANSACTION_LABELS: Record<CourierDepositTransactionType, string> = {
  top_up: "Пополнение",
  return: "Возврат",
  forfeit: "Списание",
};

export function formatCents(value: number | null | undefined) {
  const cents = Number(value ?? 0);
  return `${moneyFormatter.format(cents / 100)} ₽`;
}

export function formatPercent(value: number | null | undefined) {
  const percent = Number(value ?? 0);
  return `${percentFormatter.format(percent)}%`;
}

export function formatDate(value: string | null | undefined) {
  if (!value) {
    return "—";
  }
  return new Intl.DateTimeFormat("ru-RU").format(new Date(`${value}T00:00:00`));
}

export function formatDateTime(value: string | null | undefined) {
  if (!value) {
    return "—";
  }
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    month: "2-digit",
    year: "numeric",
  }).format(new Date(value));
}

export function todayKey() {
  return new Date().toISOString().slice(0, 10);
}

export function centsFromRubInput(value: string) {
  const normalized = value.replace(",", ".").trim();
  if (!normalized) {
    return null;
  }
  const amount = Number(normalized);
  if (!Number.isFinite(amount) || amount < 0) {
    return null;
  }
  return Math.round(amount * 100);
}

export function rubInputFromCents(value: number) {
  return String(Math.round(value / 100));
}

export function isPositiveRubInput(value: string) {
  const cents = centsFromRubInput(value);
  return cents !== null && cents > 0;
}

export function isNonNegativeRubInput(value: string) {
  return centsFromRubInput(value) !== null;
}

export function statusLabel(status: string) {
  if (status === "active") {
    return "Работает";
  }
  if (status === "requires_setup") {
    return "Резерв";
  }
  return "Уволен";
}

export function transactionTone(type: CourierDepositTransactionType) {
  return type === "top_up" ? "positive" : "negative";
}

export function progressValue(row: Pick<CourierDepositRow, "progress_pct">) {
  const value = Number(row.progress_pct);
  if (!Number.isFinite(value)) {
    return 0;
  }
  return Math.max(0, Math.min(value, 100));
}

export function shortId(value: string) {
  return value.slice(0, 8);
}
