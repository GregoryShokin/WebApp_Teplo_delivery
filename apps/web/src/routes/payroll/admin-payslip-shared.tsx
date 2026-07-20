// Презентационные компоненты контура ведомости администрации. Повторяют вёрстку
// «Расчётов» (run-detail.tsx), где локальные аналоги не экспортируются. Чистые
// утилиты вынесены в ./admin-payslip-utils (fast-refresh: файл — только компоненты).
import { ChevronRight } from "lucide-react";

import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import type { PayrollLine } from "@/lib/api";

import {
  lineOnHand,
  lineSalaryBeforeSettlement,
  lineSettlementFlows,
  moneyValue,
  normalizeMoney,
  type AdjustmentComponent,
} from "./admin-payslip-utils";
import { formatDate, formatMoney } from "./runs";

export function KpiCard({
  description,
  title,
  value,
}: {
  description: string;
  title: string;
  value: string;
}) {
  return (
    <Card className="shadow-none">
      <CardContent className="p-4">
        <div className="text-sm text-muted-foreground">{title}</div>
        <div className="mt-2 text-2xl font-semibold tabular-nums">{value}</div>
        <div className="mt-2 text-sm text-muted-foreground">{description}</div>
      </CardContent>
    </Card>
  );
}

export function ComponentValue({
  dense = false,
  label,
  strong = false,
  value,
}: {
  dense?: boolean;
  label: string;
  strong?: boolean;
  value: string;
}) {
  return (
    <div className={cn("rounded-md border bg-background p-3", dense ? "p-2" : undefined)}>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={cn("mt-1 tabular-nums", strong ? "font-semibold" : "font-medium")}>
        {value}
      </div>
    </div>
  );
}

function adjustmentTypeLabel(item: AdjustmentComponent, kind: "bonus" | "deduction") {
  if (kind === "bonus") {
    return "Премия";
  }
  return item.category.toLowerCase().includes("штраф") ? "Штраф" : "Удержание";
}

export function AdjustmentDisclosure({
  defaultOpen = false,
  items,
  kind,
  title,
}: {
  defaultOpen?: boolean;
  items: AdjustmentComponent[];
  kind: "bonus" | "deduction";
  title: string;
}) {
  const total = items.reduce((sum, item) => sum + moneyValue(item.amount), 0);

  return (
    <details className="group rounded-md border bg-card" open={defaultOpen || undefined}>
      <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-3 py-2 text-sm [&::-webkit-details-marker]:hidden">
        <span className="flex items-center gap-2 font-semibold">
          <ChevronRight
            className="h-4 w-4 text-muted-foreground transition-transform group-open:rotate-90"
            aria-hidden="true"
          />
          {title}
          <span className="font-normal text-muted-foreground">· {items.length}</span>
        </span>
        <span
          className={cn(
            "font-medium tabular-nums",
            kind === "bonus" && total > 0 ? "text-emerald-700" : undefined,
            kind === "deduction" && total > 0 ? "text-rose-700" : undefined,
          )}
        >
          {kind === "bonus" && total > 0 ? "+" : kind === "deduction" && total > 0 ? "−" : ""}
          {formatMoney(total)}
        </span>
      </summary>
      <div className="border-t px-3 py-2">
        {items.length > 0 ? (
          <div className="divide-y">
            {items.map((item) => (
              <div
                className="flex flex-wrap items-center gap-x-2 gap-y-1 py-1.5 text-xs"
                key={item.id}
              >
                <span className="text-muted-foreground">{formatDate(item.workDate)}</span>
                <span className="border-l pl-2 font-medium">{adjustmentTypeLabel(item, kind)}</span>
                <span className="border-l pl-2">{item.category}</span>
                {item.comment ? (
                  <span className="min-w-0 flex-1 border-l pl-2 text-muted-foreground">
                    {item.comment}
                  </span>
                ) : (
                  <span className="min-w-0 flex-1" />
                )}
                <span
                  className={cn(
                    "ml-auto border-l pl-2 font-semibold tabular-nums",
                    kind === "bonus" ? "text-emerald-700" : "text-rose-700",
                  )}
                >
                  {kind === "bonus" ? "+" : "−"}
                  {formatMoney(item.amount)}
                </span>
              </div>
            ))}
          </div>
        ) : (
          <div className="text-xs text-muted-foreground">
            {kind === "bonus" ? "За этот период премий нет." : "Штрафов и удержаний нет."}
          </div>
        )}
      </div>
    </details>
  );
}

type PayoutFormulaTerm = { label: string; amount: number };

/** Формула «зарплата ± потоки = к выплате», как в «Расчётах» (депозитные слагаемые у
 *  администрации нулевые и отфильтровываются). */
export function PayoutFormula({ line }: { line: PayrollLine }) {
  const salary = lineSalaryBeforeSettlement(line);
  const flows = lineSettlementFlows(line);
  const terms: PayoutFormulaTerm[] = [
    { label: "удержание депозита", amount: -moneyValue(line.deposit_withholding) },
    { label: "выдача депозита", amount: moneyValue(line.deposit_payout) },
    { label: "аванс", amount: flows.advanceIssued },
    { label: "заём", amount: flows.loanIssued },
    { label: "аванс/заём", amount: flows.unspecifiedIssued },
    { label: "возврат аванса", amount: -flows.advanceRecovered },
    { label: "возврат займа", amount: -flows.loanRecovered },
    { label: "возврат аванса/займа", amount: -flows.unspecifiedRecovered },
    { label: "выплачено ранее", amount: -flows.previouslyPaid },
  ].filter((term) => Math.abs(term.amount) >= 0.005);

  const explainedTotal = normalizeMoney(salary + terms.reduce((sum, term) => sum + term.amount, 0));
  const finalTotal = lineOnHand(line);
  const unexplained = normalizeMoney(finalTotal - explainedTotal);
  if (Math.abs(unexplained) >= 0.005) {
    terms.push({
      label: unexplained > 0 ? "прочие выплаты" : "прочие удержания",
      amount: unexplained,
    });
  }

  return (
    <div className="mt-1 flex flex-wrap items-baseline gap-x-2 gap-y-1 text-sm text-emerald-900">
      <span>зарплата {formatMoney(salary)}</span>
      {terms.map((term, index) => (
        <span className="contents" key={`${term.label}-${index}`}>
          <span aria-hidden="true">{term.amount >= 0 ? "+" : "−"}</span>
          <span>
            {term.label} {formatMoney(Math.abs(term.amount))}
          </span>
        </span>
      ))}
      <span aria-hidden="true">=</span>
      <strong className="text-lg tabular-nums">{formatMoney(finalTotal)}</strong>
    </div>
  );
}
