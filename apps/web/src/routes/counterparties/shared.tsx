import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export type CounterpartiesTab = "inbox" | "drafts" | "registry";

export const COUNTERPARTIES_TABS: Array<{ value: CounterpartiesTab; label: string; path: string }> = [
  { value: "inbox", label: "К оплате", path: "/counterparties" },
  { value: "drafts", label: "Черновики и мэчинг", path: "/counterparties/drafts" },
  { value: "registry", label: "Реестр", path: "/counterparties/registry" },
];

export function counterpartiesTabPath(tab: CounterpartiesTab) {
  return COUNTERPARTIES_TABS.find((item) => item.value === tab)?.path ?? "/counterparties";
}

const rubFormatter = new Intl.NumberFormat("ru-RU", {
  style: "currency",
  currency: "RUB",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

export function formatRub(value: number | string | null | undefined) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? rubFormatter.format(numeric) : "—";
}

export function formatDate(value: string | null | undefined) {
  if (!value) {
    return "—";
  }
  return new Intl.DateTimeFormat("ru-RU", { dateStyle: "short" }).format(new Date(value));
}

export const INVOICE_STATUS_LABELS: Record<string, string> = {
  unpaid: "Не оплачено",
  partially_paid: "Частично",
  paid: "Оплачено",
  void: "Аннулировано",
};

export const RELATIONSHIP_LABELS: Record<string, string> = {
  official: "Официальный",
  informal: "Неофициальный",
  barter: "Бартер",
};

export const RELATIONSHIP_HINTS: Record<string, string> = {
  official: "Оплата банковским переводом (черновик в банк)",
  informal: "Оплата картой/наличными, перевод недоступен",
  barter: "Двусторонние накладные, расчёт по сальдо",
};

export const INVOICE_DIRECTION_LABELS: Record<string, string> = {
  payable: "К оплате",
  receivable: "Доходная",
};

export const SOURCE_LABELS: Record<string, string> = {
  iiko: "iiko",
  manual: "Вручную",
  email: "Почта",
  telegram: "Telegram",
};

export const COLLECTION_KIND_LABELS: Record<string, string> = {
  iiko: "iiko",
  email: "Почта",
  telegram: "Telegram",
  manual: "Ручной ввод",
};

export const COUNTERPARTY_TYPE_LABELS: Record<string, string> = {
  legal_entity: "Юрлицо",
  individual: "ИП / физлицо",
  bank: "Банк",
  tax_authority: "Налоговая",
};

export function formatVat(breakdown: Record<string, string> | null | undefined) {
  const entries = Object.entries(breakdown ?? {});
  if (entries.length === 0) {
    return "Без НДС";
  }
  return entries
    .sort((a, b) => Number(a[0]) - Number(b[0]))
    .map(([rate, amount]) => `${rate}% — ${formatRub(amount)}`)
    .join("; ");
}

export function isOverdue(dueDate: string | null | undefined, status: string) {
  if (!dueDate || status === "paid" || status === "void") {
    return false;
  }
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return new Date(dueDate) < today;
}

export function InvoiceStatusBadge({ status }: { status: string }) {
  const className =
    status === "paid"
      ? "border-emerald-200 bg-emerald-50 text-emerald-700"
      : status === "partially_paid"
        ? "border-sky-200 bg-sky-50 text-sky-700"
        : status === "void"
          ? "border-muted bg-muted text-muted-foreground"
          : "border-amber-200 bg-amber-50 text-amber-700";
  return <Badge className={className}>{INVOICE_STATUS_LABELS[status] ?? status}</Badge>;
}

export function RelationshipBadge({ relationship }: { relationship: string }) {
  const className =
    relationship === "barter"
      ? "border-violet-200 bg-violet-50 text-violet-700"
      : relationship === "informal"
        ? "border-orange-200 bg-orange-50 text-orange-700"
        : "border-slate-200 bg-slate-50 text-slate-700";
  return (
    <Badge className={className} title={RELATIONSHIP_HINTS[relationship]}>
      {RELATIONSHIP_LABELS[relationship] ?? relationship}
    </Badge>
  );
}

export function DraftStatusBadge({ status }: { status: string }) {
  const className =
    status === "paid"
      ? "border-emerald-200 bg-emerald-50 text-emerald-700"
      : status === "failed"
        ? "border-red-200 bg-red-50 text-red-700"
        : "border-sky-200 bg-sky-50 text-sky-700";
  const label =
    status === "paid"
      ? "Оплачен"
      : status === "failed"
        ? "Ошибка"
        : status === "updated"
          ? "Обновлён"
          : "Создан";
  return <Badge className={className}>{label}</Badge>;
}

export function MetricCard({ label, value, accent }: { label: string; value: string; accent?: "danger" | "info" }) {
  return (
    <div className="rounded-md bg-muted/50 p-4">
      <div className="text-xs font-medium uppercase text-muted-foreground">{label}</div>
      <div
        className={cn(
          "mt-1 text-2xl font-medium tabular-nums",
          accent === "danger" ? "text-red-600" : accent === "info" ? "text-sky-700" : undefined,
        )}
      >
        {value}
      </div>
    </div>
  );
}
