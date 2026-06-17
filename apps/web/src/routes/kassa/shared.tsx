import { Badge } from "@/components/ui/badge";

export type KassaActiveTab = "shift-close" | "invoices" | "receipts";

export const KASSA_ACTIVE_TAB_STORAGE_KEY = "kassa.activeTab";

export const KASSA_TABS: Array<{ value: KassaActiveTab; label: string; path: string }> = [
  { value: "shift-close", label: "Закрытие смены", path: "/kassa" },
  { value: "invoices", label: "Накладные", path: "/kassa/invoices" },
  { value: "receipts", label: "Чеки", path: "/kassa/receipts" },
];

export function kassaTabPath(tab: KassaActiveTab) {
  return KASSA_TABS.find((item) => item.value === tab)?.path ?? "/kassa";
}

export function isKassaTab(value: unknown): value is KassaActiveTab {
  return KASSA_TABS.some((item) => item.value === value);
}

// Категория изъятия смены iiko.
export const payoutCategoryLabels: Record<string, string> = {
  main_cash: "Инкассация в кассу",
  courier_salary: "ЗП курьеров",
  alisa: "Алиса (партнёр)",
  unknown: "Прочее",
};

export function PayoutCategoryBadge({ category }: { category: string }) {
  const className =
    category === "main_cash"
      ? "border-emerald-200 bg-emerald-50 text-emerald-700"
      : category === "courier_salary"
        ? "border-amber-200 bg-amber-50 text-amber-700"
        : category === "alisa"
          ? "border-sky-200 bg-sky-50 text-sky-700"
          : "border-muted bg-muted text-muted-foreground";
  return <Badge className={className}>{payoutCategoryLabels[category] ?? category}</Badge>;
}

// Статус оплаты чека.
export const chequeStatusLabels: Record<string, string> = {
  paid: "Оплачен",
  partially_paid: "Частично",
  unpaid: "Не оплачен",
  void: "Аннулирован",
};

export function ChequeStatusBadge({ status }: { status: string }) {
  const className =
    status === "paid"
      ? "border-emerald-200 bg-emerald-50 text-emerald-700"
      : status === "partially_paid"
        ? "border-amber-200 bg-amber-50 text-amber-700"
        : "border-muted bg-muted text-muted-foreground";
  return <Badge className={className}>{chequeStatusLabels[status] ?? status}</Badge>;
}

// Близость card-операции ко времени чека (tier из подбора).
export const tierLabels: Record<number, string> = {
  1: "точно по времени",
  2: "в тот же день",
  3: "около",
  4: "по дате",
};

export function CardTierBadge({ tier }: { tier: number | null }) {
  if (tier == null) {
    return null;
  }
  const className =
    tier === 1
      ? "border-emerald-200 bg-emerald-50 text-emerald-700"
      : tier === 2
        ? "border-sky-200 bg-sky-50 text-sky-700"
        : "border-muted bg-muted text-muted-foreground";
  return (
    <Badge className={className} variant="outline">
      {tierLabels[tier] ?? `tier ${tier}`}
    </Badge>
  );
}

export function ShiftPostedBadge({ posted }: { posted: boolean }) {
  return (
    <Badge
      className={
        posted
          ? "border-emerald-200 bg-emerald-50 text-emerald-700"
          : "border-amber-200 bg-amber-50 text-amber-700"
      }
    >
      {posted ? "Проведена в ДДС" : "Не проведена"}
    </Badge>
  );
}
