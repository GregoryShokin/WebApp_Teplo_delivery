import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

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
  kassa_invoice: "Касса",
  kassa_cheque: "Касса (чек)",
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

export const COUNTERPARTY_REQUISITE_FIELDS: Array<{ key: string; label: string }> = [
  { key: "recipientName", label: "Получатель" },
  { key: "inn", label: "ИНН получателя" },
  { key: "kpp", label: "КПП" },
  { key: "bankAcnt", label: "Расчётный счёт" },
  { key: "bankBik", label: "БИК" },
  { key: "recipientCorrAccountNumber", label: "Корр. счёт" },
];

export const OFFICIAL_SUPPLIER_REQUIRED_REQUISITE_KEYS = [
  "bankBik",
  "inn",
  "bankAcnt",
  "recipientCorrAccountNumber",
] as const;

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

// Роль сведённой бартер-накладной (направление + хронологическая роль) → подпись.
// Ранний по дате — заём, поздний — возврат; чей заём видно по направлению товара.
const BARTER_ROLE_LABELS: Record<string, string> = {
  "receivable:loan": "Мы заняли", // расход раньше → наш заём партнёру
  "receivable:return": "Мы вернули", // расход позже → мы вернули партнёру
  "payable:loan": "Заняли нам", // приход раньше → партнёр занял нам
  "payable:return": "Нам вернули", // приход позже → партнёр вернул нам
};

export function InvoiceStatusBadge({
  status,
  direction,
  barterSettled,
  barterRole,
}: {
  status: string;
  direction?: string;
  barterSettled?: boolean;
  barterRole?: string | null;
}) {
  // Бартер — учёт займов сырьём, а не оплата. У сведённой накладной показываем РОЛЬ по
  // хронологии (заём/возврат + чей), а цвет — по направлению товара: приход оранжевый,
  // расход зелёный (так пара заём↔возврат видна двумя цветами).
  if (barterSettled) {
    const cls =
      direction === "payable"
        ? "border-orange-200 bg-orange-50 text-orange-700"
        : "border-emerald-200 bg-emerald-50 text-emerald-700";
    const label =
      (barterRole && direction && BARTER_ROLE_LABELS[`${direction}:${barterRole}`]) || "Возвращено";
    return <Badge className={cls}>{label}</Badge>;
  }
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

export function MetricCard({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent?: "danger" | "info";
}) {
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
