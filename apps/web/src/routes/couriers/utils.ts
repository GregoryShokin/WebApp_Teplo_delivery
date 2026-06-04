import type { CourierDepositCategory, CourierDepositStatusFilter, KpiThreshold } from "@/lib/api";
import { cn } from "@/lib/utils";

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

const numberFormatter = new Intl.NumberFormat("ru-RU", {
  maximumFractionDigits: 1,
  minimumFractionDigits: 0,
});

export function formatMetric(value: number | null | undefined, fallback = "—") {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) {
    return fallback;
  }
  return numberFormatter.format(Number(value));
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

export function todayInput() {
  const now = new Date();
  return [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, "0"),
    String(now.getDate()).padStart(2, "0"),
  ].join("-");
}

export function currentMonthKey() {
  return toMonthKey(new Date());
}

export function previousMonthKey() {
  return toMonthKey(addMonths(new Date(), -1));
}

export function shiftMonth(month: string, delta: number) {
  return toMonthKey(addMonths(monthDate(month), delta));
}

export function formatMonthLabel(month: string) {
  const label = new Intl.DateTimeFormat("ru-RU", {
    month: "long",
    year: "numeric",
  }).format(monthDate(month));
  return label.charAt(0).toUpperCase() + label.slice(1);
}

export function thresholdLabel(threshold: KpiThreshold | null | undefined) {
  if (threshold === "green") {
    return "зелёный";
  }
  if (threshold === "yellow") {
    return "жёлтый";
  }
  if (threshold === "red") {
    return "красный";
  }
  return "нет данных";
}

export function thresholdBadgeClass(threshold: KpiThreshold | null | undefined) {
  return cn(
    "rounded-md border px-2 py-0.5 text-xs font-medium",
    threshold === "green" && "border-emerald-200 bg-emerald-50 text-emerald-800",
    threshold === "yellow" && "border-amber-200 bg-amber-50 text-amber-800",
    threshold === "red" && "border-red-200 bg-red-50 text-red-800",
    !threshold && "border-border bg-muted text-muted-foreground",
  );
}

export function categoryBadgeClass(category: CourierDepositCategory | null | undefined) {
  return cn(
    "rounded-md border px-2 py-0.5 text-xs font-medium",
    category === "primary" && "border-emerald-200 bg-emerald-50 text-emerald-800",
    category === "secondary" && "border-slate-200 bg-slate-50 text-slate-700",
    !category && "border-red-200 bg-red-50 text-red-800",
  );
}

export function scoreClass(score: number) {
  if (score > 0) {
    return "text-emerald-700";
  }
  if (score < 0) {
    return "text-red-700";
  }
  return "text-muted-foreground";
}

function monthDate(month: string) {
  const [year, monthIndex] = month.split("-").map(Number);
  return new Date(year, monthIndex - 1, 1);
}

function addMonths(date: Date, delta: number) {
  return new Date(date.getFullYear(), date.getMonth() + delta, 1);
}

function toMonthKey(date: Date) {
  return [date.getFullYear(), String(date.getMonth() + 1).padStart(2, "0")].join("-");
}
