import { Badge } from "@/components/ui/badge";

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

// Итог авто-штрафа смены за недостачу (поле penalty_status).
export const penaltyStatusLabels: Record<string, string> = {
  applied: "Штраф кассирам",
  waived: "Штраф отменён",
  manual_review: "Ручной разбор",
};

// Итог проверки инкассации смены (поле uncollected_status): деньги сверх стартового
// флоута остались в ящике — инкассацию забыли или провели не на всю наличку.
export const uncollectedStatusLabels: Record<string, string> = {
  missing: "Без инкассации",
  partial: "Инкассация не вся",
};

export function ShiftUncollectedBadge({ status }: { status: string | null }) {
  if (!status || status === "none") {
    return null;
  }
  const className =
    status === "missing"
      ? "border-red-200 bg-red-50 text-red-700"
      : "border-amber-200 bg-amber-50 text-amber-700";
  return (
    <Badge className={className} variant="outline">
      {uncollectedStatusLabels[status] ?? status}
    </Badge>
  );
}

export function ShiftPenaltyBadge({ status }: { status: string | null }) {
  if (!status || status === "none") {
    return null;
  }
  const className =
    status === "applied"
      ? "border-red-200 bg-red-50 text-red-700"
      : status === "manual_review"
        ? "border-amber-200 bg-amber-50 text-amber-700"
        : "border-muted bg-muted text-muted-foreground";
  return (
    <Badge className={className} variant="outline">
      {penaltyStatusLabels[status] ?? status}
    </Badge>
  );
}
