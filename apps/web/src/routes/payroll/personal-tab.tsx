import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, LoaderCircle } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import { EmptyState } from "@/components/ui-app/EmptyState";
import {
  apiErrorMessage,
  getEmployeePayrollReport,
  getEmployees,
  type PayrollPersonalReport,
} from "@/lib/api";
import { formatDate, formatDateTime, formatMoney } from "./runs";

type PayrollPersonalReportPeriod = PayrollPersonalReport["periods"][number];
type PayrollPersonalReportDaily = PayrollPersonalReport["daily"][number];
type PayrollPersonalReportAdjustment = PayrollPersonalReport["adjustments"][number];
type PayrollPersonalReportDepositTransaction =
  PayrollPersonalReport["deposit_transactions"][number];
type ReportTableView = "weekly" | "daily";

export function PayrollPersonalReportPageTab() {
  const defaultRange = useMemo(() => defaultPersonalReportRange(), []);
  const [employeeId, setEmployeeId] = useState("");
  const [dateFrom, setDateFrom] = useState(defaultRange.from);
  const [dateTo, setDateTo] = useState(defaultRange.to);
  const [tableView, setTableView] = useState<ReportTableView>("weekly");
  const [reportParams, setReportParams] = useState<{
    employee_id: string;
    date_from: string;
    date_to: string;
  } | null>(null);

  const employeesQuery = useQuery({
    queryKey: ["employees", "payroll-personal-report"],
    queryFn: () => getEmployees({ status: "all" }),
  });
  const sortedEmployees = useMemo(
    () =>
      [...(employeesQuery.data ?? [])].sort((left, right) =>
        left.full_name.localeCompare(right.full_name, "ru"),
      ),
    [employeesQuery.data],
  );
  const defaultEmployeeId = sortedEmployees[0]?.id ?? "";

  useEffect(() => {
    if (!employeeId && defaultEmployeeId) {
      setEmployeeId(defaultEmployeeId);
    }
  }, [defaultEmployeeId, employeeId]);

  useEffect(() => {
    if (!reportParams && employeeId && dateFrom && dateTo && dateFrom <= dateTo) {
      setReportParams({ employee_id: employeeId, date_from: dateFrom, date_to: dateTo });
    }
  }, [dateFrom, dateTo, employeeId, reportParams]);

  const reportQuery = useQuery({
    queryKey: ["payroll-personal-report", reportParams],
    queryFn: () => {
      if (!reportParams) {
        throw new Error("Report params are not set");
      }
      return getEmployeePayrollReport(reportParams);
    },
    enabled: reportParams !== null,
  });

  function buildReport() {
    if (!employeeId || !dateFrom || !dateTo) {
      toast.error("Выберите сотрудника и даты");
      return;
    }
    if (dateFrom > dateTo) {
      toast.error("Дата начала должна быть раньше даты окончания");
      return;
    }
    setReportParams({ employee_id: employeeId, date_from: dateFrom, date_to: dateTo });
  }

  const report = reportQuery.data;
  const hasReportData = Boolean(
    report &&
    (report.periods.length > 0 ||
      report.daily.length > 0 ||
      report.adjustments.length > 0 ||
      report.deposit_transactions.length > 0 ||
      Number(report.opening_balance) !== 0 ||
      Number(report.closing_balance) !== 0),
  );
  const periodColumns: Array<DataTableColumn<PayrollPersonalReportPeriod>> = [
    {
      key: "period",
      header: "Период",
      cell: (row) => `${formatDate(row.period_start)} — ${formatDate(row.period_end)}`,
    },
    {
      key: "role",
      header: "Роль",
      cell: (row) => row.role,
    },
    {
      key: "base_pay",
      header: "Оклад",
      cell: (row) => formatMoney(row.base_pay),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "premium",
      header: "Премия",
      cell: (row) => formatMoney(row.premium),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "percent_pay",
      header: "%",
      cell: (row) => formatMoney(row.percent_pay),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "fund_accrual",
      header: "Фонд",
      cell: (row) => formatMoney(row.fund_accrual),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "ndfl_withheld",
      header: "НДФЛ",
      cell: (row) => formatMoney(row.ndfl_withheld),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "deduction",
      header: "Удержано",
      cell: (row) => formatMoney(row.deduction),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "total_payable",
      header: "К выплате",
      cell: (row) => formatMoney(row.total_payable),
      className: "text-right font-semibold tabular-nums",
      headerClassName: "text-right",
    },
  ];
  const adjustmentColumns: Array<DataTableColumn<PayrollPersonalReportAdjustment>> = [
    {
      key: "work_date",
      header: "Дата",
      cell: (row) => formatDate(row.work_date),
    },
    {
      key: "type",
      header: "Тип",
      cell: (row) => (
        <Badge className="rounded-md border-border bg-background text-foreground shadow-none">
          {adjustmentTypeLabel(row.type)}
        </Badge>
      ),
    },
    {
      key: "category",
      header: "Категория",
      cell: (row) => row.category_name,
    },
    {
      key: "amount",
      header: "Сумма",
      cell: (row) => formatMoney(row.amount),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "comment",
      header: "Комментарий",
      cell: (row) => row.comment ?? <span className="text-muted-foreground">—</span>,
    },
  ];
  const dailyColumns: Array<DataTableColumn<PayrollPersonalReportDaily>> = [
    {
      key: "date",
      header: "Дата",
      cell: (row) => formatDate(row.date),
      className: "whitespace-nowrap",
    },
    {
      key: "base_pay",
      header: "Оклад",
      cell: (row) => formatMoney(row.base_pay),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "percent_pay",
      header: "Процент",
      cell: (row) => formatMoney(row.percent_pay),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "premium",
      header: "Премия",
      cell: (row) => formatMoney(row.premium),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "vacation_pay",
      header: "Больничные/отпуск",
      cell: (row) => formatMoney(row.vacation_pay),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "fund_accrual",
      header: "Накопительный фонд",
      cell: (row) => formatMoney(row.fund_accrual),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "deposit_out",
      header: "Депозит возврат",
      cell: (row) => formatMoney(row.deposit_out),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "deposit_in",
      header: "Депозит удержание",
      cell: (row) => formatMoney(row.deposit_in),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "penalty",
      header: "Штрафы и удержания",
      cell: (row) => formatMoney(row.penalty),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "audit_penalty",
      header: "Штрафы по ревизиям",
      cell: (row) => formatMoney(row.audit_penalty),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "ndfl",
      header: "Удержание НДФЛ",
      cell: (row) => formatMoney(row.ndfl_withheld),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "salary_payout",
      header: "Выплата ЗП",
      cell: () => <span className="text-muted-foreground">—</span>,
      className: "text-center",
      headerClassName: "text-center",
    },
    {
      key: "advance_payout",
      header: "Выплата аванса",
      cell: () => <span className="text-muted-foreground">—</span>,
      className: "text-center",
      headerClassName: "text-center",
    },
    {
      key: "comment",
      header: "Комментарий",
      cell: (row) => row.comment ?? <span className="text-muted-foreground">—</span>,
      className: "min-w-52",
    },
  ];
  const depositColumns: Array<DataTableColumn<PayrollPersonalReportDepositTransaction>> = [
    {
      key: "created_at",
      header: "Дата",
      cell: (row) => formatDateTime(row.created_at),
    },
    {
      key: "type",
      header: "Тип",
      cell: (row) => depositTransactionTypeLabel(row.transaction_type),
    },
    {
      key: "amount",
      header: "Сумма",
      cell: (row) => formatMoney(row.amount),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
  ];

  return (
    <section className="space-y-4">
      <div className="grid gap-3 rounded-lg border bg-card p-3 lg:grid-cols-[minmax(220px,1fr)_160px_160px_auto] lg:items-end">
        <Label className="grid gap-2">
          <span>Сотрудник</span>
          <Select
            disabled={sortedEmployees.length === 0 || employeesQuery.isLoading}
            onValueChange={setEmployeeId}
            value={employeeId}
          >
            <SelectTrigger>
              <SelectValue placeholder="Выберите сотрудника" />
            </SelectTrigger>
            <SelectContent>
              {sortedEmployees.map((employee) => (
                <SelectItem key={employee.id} value={employee.id}>
                  {employee.full_name}
                  {employee.status === "inactive" ? " · уволен" : ""}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Label>

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
          disabled={!employeeId || !dateFrom || !dateTo || reportQuery.isFetching}
          onClick={buildReport}
          type="button"
        >
          {reportQuery.isFetching ? (
            <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
          ) : null}
          Построить
        </Button>
      </div>

      {employeesQuery.isError ? (
        <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {apiErrorMessage(employeesQuery.error, "Не удалось загрузить сотрудников")}
        </div>
      ) : null}

      {reportQuery.isError ? (
        <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {apiErrorMessage(reportQuery.error, "Не удалось построить отчёт")}
        </div>
      ) : null}

      {report ? (
        <div className="space-y-4">
          <section className="rounded-lg border bg-card p-4">
            <div className="font-semibold">{report.employee_name}</div>
            <div className="mt-1 text-sm text-muted-foreground">
              {[
                report.employee_position,
                `${formatDate(report.date_from)} — ${formatDate(report.date_to)}`,
              ]
                .filter(Boolean)
                .join(" · ")}
            </div>
          </section>

          {!hasReportData ? (
            <EmptyState
              icon={<AlertTriangle className="h-5 w-5" aria-hidden="true" />}
              title="Нет начислений за выбранный период"
              description="По выбранным датам нет строк расчёта, корректировок или депозитных операций."
            />
          ) : (
            <>
              <section className="grid gap-3 md:grid-cols-3 2xl:grid-cols-6">
                <PersonalMetric
                  title="К выплате"
                  value={formatMoney(report.totals.total_payable)}
                  description="По найденным периодам"
                />
                <PersonalMetric
                  title="Начислено"
                  value={formatMoney(
                    report.totals.base_pay +
                      report.totals.premium +
                      report.totals.percent_pay +
                      report.totals.vacation_pay +
                      report.totals.bonus_total,
                  )}
                  description="Оклад, %, отпуск, премии"
                />
                <PersonalMetric
                  title="Удержано"
                  value={formatMoney(report.totals.deduction)}
                  description="Штрафы и депозит"
                />
                <PersonalMetric
                  title="Фонд"
                  value={formatMoney(report.totals.fund_accrual)}
                  description="Накопительный фонд"
                />
                <PersonalMetric
                  title="Долг на конец"
                  value={formatReportMoney(report.closing_balance)}
                  description={balanceDescription(report.closing_balance)}
                />
                <PersonalMetric
                  title="Смены"
                  value={String(report.shifts_count)}
                  description="Рабочие дни"
                />
              </section>

              <section className="space-y-2">
                <Tabs
                  value={tableView}
                  onValueChange={(value) => setTableView(value as ReportTableView)}
                >
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div className="text-sm font-semibold">Детализация</div>
                    <TabsList>
                      <TabsTrigger value="weekly">По неделям</TabsTrigger>
                      <TabsTrigger value="daily">По дням</TabsTrigger>
                    </TabsList>
                  </div>
                  <TabsContent value="weekly">
                    <DataTable
                      columns={periodColumns}
                      rows={report.periods}
                      getRowKey={(row) => `${row.run_id}-${row.role}`}
                      emptyMessage="Нет начислений за выбранный период"
                    />
                  </TabsContent>
                  <TabsContent value="daily">
                    <DataTable
                      columns={dailyColumns}
                      rows={report.daily}
                      getRowKey={(row) => row.date}
                      emptyMessage="Нет дневной детализации за выбранный период"
                    />
                  </TabsContent>
                </Tabs>
              </section>

              <section className="space-y-2">
                <div className="text-sm font-semibold">Премии и штрафы</div>
                <DataTable
                  columns={adjustmentColumns}
                  rows={report.adjustments}
                  getRowKey={(row) => row.id}
                  emptyMessage="Корректировок нет"
                />
              </section>

              <section className="space-y-2">
                <div className="text-sm font-semibold">Депозит</div>
                <DataTable
                  columns={depositColumns}
                  rows={report.deposit_transactions}
                  getRowKey={(row) => row.id}
                  emptyMessage="Операций депозита нет"
                />
              </section>

              {tableView === "daily" ? (
                <section className="space-y-2">
                  <div className="text-sm font-semibold">История периодов</div>
                  <DataTable
                    columns={periodColumns}
                    rows={report.periods}
                    getRowKey={(row) => `${row.run_id}-${row.role}`}
                    emptyMessage="Нет начислений за выбранный период"
                  />
                </section>
              ) : null}
            </>
          )}
        </div>
      ) : null}
    </section>
  );
}

function PersonalMetric({
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
        <div className="mt-2 break-words text-xl font-semibold tabular-nums">{value}</div>
        <div className="mt-2 text-sm text-muted-foreground">{description}</div>
      </CardContent>
    </Card>
  );
}

function defaultPersonalReportRange() {
  const to = new Date();
  const from = addDays(to, -89);
  return {
    from: dateInputValue(from),
    to: dateInputValue(to),
  };
}

function adjustmentTypeLabel(type: PayrollPersonalReportAdjustment["type"]) {
  return type === "bonus" ? "Премия" : "Штраф";
}

function depositTransactionTypeLabel(type: string) {
  const labels: Record<string, string> = {
    accrual: "Удержание (накопление)",
    payout: "Возврат",
    dismissal_payout: "Возврат при увольнении",
    dismissal_writeoff: "Списание при увольнении",
    write_off: "Списание",
  };
  return labels[type] ?? type;
}

function formatReportMoney(value: number | string | null | undefined) {
  const amount = Number(value);
  if (!Number.isFinite(amount)) {
    return "—";
  }
  return formatMoney(amount);
}

function balanceDescription(value: string) {
  const amount = Number(value);
  if (amount > 0) {
    return "Должны сотруднику";
  }
  if (amount < 0) {
    return "Сотрудник должен";
  }
  return "Расчёт";
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
