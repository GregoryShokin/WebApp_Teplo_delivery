import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Calculator, LoaderCircle, RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import { EmptyState } from "@/components/ui-app/EmptyState";
import { StatusBadge } from "@/components/ui-app/StatusBadge";
import { apiErrorMessage, getPayrollAggregate, type PayrollAggregate } from "@/lib/api";
import { cn } from "@/lib/utils";
import { formatDate, formatMoney } from "./runs";

type PayrollAggregatePeriod = PayrollAggregate["periods"][number];

type PayrollAccrualsTabProps = {
  onNavigate: (path: string) => void;
};

export function PayrollAccrualsTab({ onNavigate }: PayrollAccrualsTabProps) {
  const defaultRange = useMemo(() => defaultAccrualsRange(), []);
  const [dateFrom, setDateFrom] = useState(defaultRange.from);
  const [dateTo, setDateTo] = useState(defaultRange.to);
  const hasValidRange = Boolean(dateFrom && dateTo && dateFrom <= dateTo);

  const aggregateQuery = useQuery({
    queryKey: ["payroll-aggregate", dateFrom, dateTo],
    queryFn: () => getPayrollAggregate({ date_from: dateFrom, date_to: dateTo }),
    enabled: hasValidRange,
  });

  const aggregate = aggregateQuery.data;
  const totals = aggregate?.totals;
  const periodColumns: Array<DataTableColumn<PayrollAggregatePeriod>> = [
    {
      key: "period",
      header: "Период",
      cell: (row) => (
        <div className="min-w-[170px]">
          <div className="font-medium">
            {formatDate(row.period_start)} — {formatDate(row.period_end)}
          </div>
          <div className="text-xs text-muted-foreground">Run {shortId(row.run_id)}</div>
        </div>
      ),
    },
    {
      key: "status",
      header: "Статус",
      cell: (row) => <StatusBadge status={row.run_status} />,
    },
    {
      key: "lines",
      header: "Строк",
      cell: (row) => row.lines_count,
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "base_pay",
      header: "Оклад",
      cell: (row) => formatAggregateMoney(row.base_pay),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "premium",
      header: "Премия",
      cell: (row) => formatAggregateMoney(row.premium),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "percent_pay",
      header: "%",
      cell: (row) => formatAggregateMoney(row.percent_pay),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "deduction",
      header: "Удержания",
      cell: (row) => formatAggregateMoney(row.deduction),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "fund_accrual",
      header: "Фонд",
      cell: (row) => formatAggregateMoney(row.fund_accrual),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "total_payable",
      header: "К выплате",
      cell: (row) => formatAggregateMoney(row.total_payable),
      className: "text-right font-semibold tabular-nums",
      headerClassName: "text-right",
    },
  ];

  return (
    <section className="space-y-5">
      <div className="grid gap-3 rounded-lg border bg-card p-3 md:grid-cols-[160px_160px_auto_1fr] md:items-end">
        <Label className="grid gap-2">
          <span>С даты</span>
          <Input
            onChange={(event) => setDateFrom(event.target.value)}
            type="date"
            value={dateFrom}
          />
        </Label>
        <Label className="grid gap-2">
          <span>По дату</span>
          <Input onChange={(event) => setDateTo(event.target.value)} type="date" value={dateTo} />
        </Label>
        <Button
          disabled={!hasValidRange || aggregateQuery.isFetching}
          onClick={() => aggregateQuery.refetch()}
          type="button"
          variant="outline"
        >
          {aggregateQuery.isFetching ? (
            <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
          ) : (
            <RefreshCw size={16} aria-hidden="true" />
          )}
          Обновить
        </Button>
        <div className="text-sm text-muted-foreground md:justify-self-end">
          {aggregate
            ? `${aggregate.runs_count} расчётов · ${aggregate.employees_count} сотрудников`
            : "—"}
        </div>
      </div>

      {!hasValidRange ? (
        <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
          Дата начала должна быть раньше даты окончания
        </div>
      ) : null}

      {aggregateQuery.isError ? (
        <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {apiErrorMessage(aggregateQuery.error, "Не удалось загрузить начисления")}
        </div>
      ) : null}

      <section className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <AccrualMetric
          title="ФОТ начислено"
          value={formatAggregateMoney(totals?.gross)}
          description="Оклад, премии, %, отпуск"
          strong
        />
        <AccrualMetric
          title="К выплате"
          value={formatAggregateMoney(totals?.total_payable)}
          description="После удержаний"
          strong
        />
        <AccrualMetric
          title="Удержания"
          value={formatAggregateMoney(totals?.deduction)}
          description="Штрафы и депозит"
          tone="warning"
        />
        <AccrualMetric
          title="Оклад"
          value={formatAggregateMoney(totals?.base_pay)}
          description="Базовая часть"
        />
        <AccrualMetric
          title="Процент от выручки"
          value={formatAggregateMoney(totals?.percent_pay)}
          description="Переменная часть"
        />
        <AccrualMetric
          title="Премии / штрафы"
          value={`${formatAggregateMoney(totals?.bonus_total)} / ${formatAggregateMoney(totals?.penalty_total)}`}
          description="Ручные корректировки"
        />
        <AccrualMetric
          title="Накопительный фонд"
          value={formatAggregateMoney(totals?.fund_accrual)}
          description="Начислено в фонд"
        />
        <AccrualMetric
          title="Депозит удержано"
          value={formatAggregateMoney(totals?.deposit_withheld)}
          description="Выделено из удержаний"
        />
      </section>

      {aggregate && aggregate.periods.length === 0 && !aggregateQuery.isFetching ? (
        <EmptyState
          icon={<AlertTriangle className="h-5 w-5" aria-hidden="true" />}
          title="Расчётов за выбранный период нет"
          description="Выберите другой диапазон дат."
        />
      ) : (
        <section className="space-y-2">
          <div className="flex items-center gap-2 text-sm font-semibold">
            <Calculator size={16} aria-hidden="true" />
            По периодам
          </div>
          <DataTable
            columns={periodColumns}
            rows={aggregate?.periods ?? []}
            isLoading={aggregateQuery.isLoading || aggregateQuery.isFetching}
            getRowKey={(row) => row.period_id}
            onRowClick={(row) => onNavigate(`/payroll/runs/${row.run_id}`)}
            emptyMessage="Расчётов за выбранный период нет"
          />
        </section>
      )}
    </section>
  );
}

function AccrualMetric({
  description,
  strong = false,
  title,
  tone = "default",
  value,
}: {
  description: string;
  strong?: boolean;
  title: string;
  tone?: "default" | "warning";
  value: string;
}) {
  return (
    <Card
      className={cn("shadow-none", tone === "warning" ? "border-amber-200 bg-amber-50" : undefined)}
    >
      <CardContent className="p-4">
        <div className="text-sm text-muted-foreground">{title}</div>
        <div className={cn("mt-2 text-2xl font-semibold tabular-nums", strong && "font-bold")}>
          {value}
        </div>
        <div className="mt-2 text-sm text-muted-foreground">{description}</div>
      </CardContent>
    </Card>
  );
}

function defaultAccrualsRange() {
  const to = new Date();
  const from = addDays(to, -27);
  return {
    from: dateInputValue(from),
    to: dateInputValue(to),
  };
}

function formatAggregateMoney(value: string | number | null | undefined) {
  return formatMoney(Number(value ?? 0));
}

function shortId(value: string) {
  return value.slice(0, 8);
}

function addDays(value: Date, days: number) {
  const date = new Date(value);
  date.setDate(date.getDate() + days);
  return date;
}

function dateInputValue(value: Date) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}
