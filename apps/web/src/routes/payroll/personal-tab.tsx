import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Check, ChevronDown, LoaderCircle } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import { EmptyState } from "@/components/ui-app/EmptyState";
import {
  apiErrorMessage,
  type Employee,
  getEmployeePayrollReport,
  getEmployees,
  type PayrollPersonalReport,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { formatDate, formatMoney } from "./runs";

type PayrollPersonalReportPeriod = PayrollPersonalReport["periods"][number];
type ReportTableView = "weekly" | "daily";

type OperationKind =
  | "base_pay"
  | "percent_pay"
  | "vacation_pay"
  | "ndfl"
  | "premium"
  | "manual_penalty"
  | "audit_penalty"
  | "deposit_accrual"
  | "deposit_payout"
  | "deposit_writeoff"
  | "fund_accrual"
  | "fund_payout";

type OperationRow = {
  id: string;
  date: string;
  kind: OperationKind;
  amount: string;
  comment: string | null;
  periodStart?: string;
};

const KIND_META: Record<
  OperationKind,
  { label: string; badgeClass: string; isDeduction: boolean }
> = {
  base_pay: {
    label: "Оклад",
    badgeClass: "bg-slate-100 text-slate-800",
    isDeduction: false,
  },
  percent_pay: {
    label: "Процент",
    badgeClass: "bg-cyan-100 text-cyan-800",
    isDeduction: false,
  },
  vacation_pay: {
    label: "Больничный/отпуск",
    badgeClass: "bg-purple-100 text-purple-800",
    isDeduction: false,
  },
  premium: {
    label: "Премия",
    badgeClass: "bg-emerald-100 text-emerald-800",
    isDeduction: false,
  },
  manual_penalty: {
    label: "Штраф",
    badgeClass: "bg-rose-100 text-rose-800",
    isDeduction: true,
  },
  audit_penalty: {
    label: "Штраф по ревизии",
    badgeClass: "bg-orange-100 text-orange-800",
    isDeduction: true,
  },
  deposit_payout: {
    label: "Возврат депозита",
    badgeClass: "bg-emerald-50 text-emerald-700",
    isDeduction: false,
  },
  deposit_accrual: {
    label: "Удержание депозита",
    badgeClass: "bg-amber-100 text-amber-800",
    isDeduction: true,
  },
  deposit_writeoff: {
    label: "Списание депозита",
    badgeClass: "bg-rose-50 text-rose-700",
    isDeduction: true,
  },
  fund_accrual: {
    label: "Накопит. фонд",
    badgeClass: "bg-teal-100 text-teal-800",
    isDeduction: false,
  },
  fund_payout: {
    label: "Выплата фонда",
    badgeClass: "bg-teal-50 text-teal-600",
    isDeduction: false,
  },
  ndfl: {
    label: "НДФЛ",
    badgeClass: "bg-gray-100 text-gray-700",
    isDeduction: true,
  },
};

const KIND_ORDER: Record<OperationKind, number> = {
  base_pay: 1,
  percent_pay: 2,
  vacation_pay: 3,
  premium: 4,
  manual_penalty: 5,
  audit_penalty: 6,
  deposit_payout: 7,
  deposit_accrual: 8,
  deposit_writeoff: 9,
  fund_accrual: 10,
  fund_payout: 11,
  ndfl: 12,
};

export function PayrollPersonalReportPageTab() {
  const defaultRange = useMemo(() => defaultPersonalReportRange(), []);
  const [employeeId, setEmployeeId] = useState("");
  const [showFired, setShowFired] = useState(false);
  const [dateFrom, setDateFrom] = useState(defaultRange.from);
  const [dateTo, setDateTo] = useState(defaultRange.to);
  const [tableView, setTableView] = useState<ReportTableView>("weekly");
  const [openWeek, setOpenWeek] = useState<PayrollPersonalReportPeriod | null>(null);
  const [reportParams, setReportParams] = useState<{
    employee_id: string;
    date_from: string;
    date_to: string;
  } | null>(null);

  const employeesQuery = useQuery({
    queryKey: ["employees", "payroll-personal-report"],
    queryFn: () => getEmployees({ status: "all" }),
  });
  // Персональный отчёт ведётся только по поварам и кассирам.
  const eligibleEmployees = useMemo(
    () =>
      (employeesQuery.data ?? [])
        .filter(
          (employee) => employee.position === "Повар" || employee.position === "Кассир",
        )
        .sort((left, right) => left.full_name.localeCompare(right.full_name, "ru")),
    [employeesQuery.data],
  );
  // Уволенные скрыты по умолчанию; toggle добавляет их к активным.
  const statusFilteredEmployees = useMemo(
    () =>
      eligibleEmployees.filter(
        (employee) => showFired || employee.status === "active",
      ),
    [eligibleEmployees, showFired],
  );
  const defaultEmployeeId =
    eligibleEmployees.find((employee) => employee.status === "active")?.id ?? "";

  useEffect(() => {
    if (!employeeId && defaultEmployeeId) {
      setEmployeeId(defaultEmployeeId);
    }
  }, [defaultEmployeeId, employeeId]);

  // Если выбранный сотрудник выпал из фильтра (например, выключили уволенных) — сбросить.
  useEffect(() => {
    if (employeeId && !statusFilteredEmployees.some((employee) => employee.id === employeeId)) {
      setEmployeeId(statusFilteredEmployees[0]?.id ?? "");
    }
  }, [employeeId, statusFilteredEmployees]);

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
    setOpenWeek(null);
    setReportParams({ employee_id: employeeId, date_from: dateFrom, date_to: dateTo });
  }

  const report = reportQuery.data;
  const operations = useMemo(() => (report ? buildOperations(report) : []), [report]);
  const openWeekOperations = useMemo(() => {
    if (!openWeek) {
      return [];
    }
    return operations.filter((row) => {
      if (row.periodStart) {
        return row.periodStart === openWeek.period_start;
      }
      return row.date >= openWeek.period_start && row.date <= openWeek.period_end;
    });
  }, [openWeek, operations]);
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

  return (
    <section className="space-y-4">
      <div className="grid gap-3 rounded-lg border bg-card p-3 lg:grid-cols-[minmax(220px,1fr)_160px_160px_auto] lg:items-end">
        <div className="grid gap-2">
          <span className="text-sm font-medium leading-none">Сотрудник</span>
          <EmployeeCombobox
            disabled={eligibleEmployees.length === 0 || employeesQuery.isLoading}
            employees={statusFilteredEmployees}
            onChange={setEmployeeId}
            value={employeeId}
          />
          <label className="flex items-center gap-2 text-sm text-muted-foreground">
            <Switch checked={showFired} onCheckedChange={setShowFired} />
            <span>Показать уволенных</span>
          </label>
        </div>

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
                      getRowKey={(row) => `${row.period_id}-${row.run_id}-${row.role}`}
                      onRowClick={setOpenWeek}
                      rowClassName="hover:bg-muted/50"
                      emptyMessage="Нет начислений за выбранный период"
                    />
                  </TabsContent>
                  <TabsContent value="daily">
                    <OperationsTable rows={operations} />
                  </TabsContent>
                </Tabs>
              </section>

              {tableView === "daily" ? (
                <section className="space-y-2">
                  <div className="text-sm font-semibold">История периодов</div>
                  <DataTable
                    columns={periodColumns}
                    rows={report.periods}
                    getRowKey={(row) => `${row.period_id}-${row.run_id}-${row.role}`}
                    emptyMessage="Нет начислений за выбранный период"
                  />
                </section>
              ) : null}

              <Dialog open={openWeek !== null} onOpenChange={(open) => !open && setOpenWeek(null)}>
                <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-3xl">
                  <DialogHeader>
                    <DialogTitle>
                      Ведомость {openWeek ? formatDate(openWeek.period_start) : ""} —{" "}
                      {openWeek ? formatDate(openWeek.period_end) : ""}
                    </DialogTitle>
                    <DialogDescription>
                      {report.employee_name} · {openWeek?.role}
                    </DialogDescription>
                  </DialogHeader>
                  {openWeek ? (
                    <>
                      <div className="grid gap-3 md:grid-cols-4">
                        <KpiSmall label="Оклад" value={formatMoney(openWeek.base_pay)} />
                        <KpiSmall label="Процент" value={formatMoney(openWeek.percent_pay)} />
                        <KpiSmall label="Удержано" value={formatMoney(openWeek.deduction)} />
                        <KpiSmall label="К выплате" value={formatMoney(openWeek.total_payable)} />
                      </div>
                      <OperationsTable rows={openWeekOperations} />
                    </>
                  ) : null}
                </DialogContent>
              </Dialog>
            </>
          )}
        </div>
      ) : null}
    </section>
  );
}

function employeeOptionLabel(employee: Employee) {
  return `${employee.full_name}${employee.status === "inactive" ? " · уволен" : ""}`;
}

// Комбобокс с поиском прямо внутри выпадающего списка (без cmdk/Popover —
// Radix Select перехватывает ввод, поэтому собираем лёгкий собственный вариант).
function EmployeeCombobox({
  disabled,
  employees,
  onChange,
  value,
}: {
  disabled?: boolean;
  employees: Employee[];
  onChange: (id: string) => void;
  value: string;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const containerRef = useRef<HTMLDivElement>(null);

  const selected = employees.find((employee) => employee.id === value) ?? null;
  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) {
      return employees;
    }
    return employees.filter((employee) =>
      employee.full_name.toLowerCase().includes(query),
    );
  }, [employees, search]);

  useEffect(() => {
    if (!open) {
      setSearch("");
      return;
    }
    const handlePointer = (event: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handlePointer);
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("mousedown", handlePointer);
      document.removeEventListener("keydown", handleKey);
    };
  }, [open]);

  return (
    <div className="relative" ref={containerRef}>
      <button
        aria-expanded={open}
        className="flex h-10 w-full items-center justify-between rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
        disabled={disabled}
        onClick={() => setOpen((current) => !current)}
        type="button"
      >
        <span className={cn("line-clamp-1", !selected && "text-muted-foreground")}>
          {selected ? employeeOptionLabel(selected) : "Выберите сотрудника"}
        </span>
        <ChevronDown className="h-4 w-4 shrink-0 opacity-50" aria-hidden="true" />
      </button>

      {open ? (
        <div className="absolute z-50 mt-1 w-full rounded-md border bg-popover text-popover-foreground shadow-md">
          <div className="border-b p-1">
            <Input
              autoFocus
              className="h-9"
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Поиск сотрудника"
              value={search}
            />
          </div>
          <div className="max-h-64 overflow-y-auto p-1">
            {filtered.length === 0 ? (
              <div className="px-2 py-2 text-sm text-muted-foreground">
                Сотрудники не найдены
              </div>
            ) : (
              filtered.map((employee) => (
                <button
                  className={cn(
                    "flex w-full items-center justify-between gap-2 rounded-sm px-2 py-1.5 text-left text-sm outline-none hover:bg-accent hover:text-accent-foreground focus:bg-accent focus:text-accent-foreground",
                    employee.id === value && "bg-accent/60",
                  )}
                  key={employee.id}
                  onClick={() => {
                    onChange(employee.id);
                    setOpen(false);
                  }}
                  type="button"
                >
                  <span className="line-clamp-1">{employeeOptionLabel(employee)}</span>
                  {employee.id === value ? (
                    <Check className="h-4 w-4 shrink-0" aria-hidden="true" />
                  ) : null}
                </button>
              ))
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function buildOperations(report: PayrollPersonalReport): OperationRow[] {
  const rows: OperationRow[] = [];
  const periodStartForDate = (date: string) =>
    report.periods.find((period) => date >= period.period_start && date <= period.period_end)
      ?.period_start;
  const pushOperation = (row: Omit<OperationRow, "periodStart">) => {
    const periodStart = periodStartForDate(row.date);
    rows.push(periodStart ? { ...row, periodStart } : row);
  };

  for (const day of report.daily) {
    if (day.base_pay > 0) {
      pushOperation({
        id: `bp-${day.date}`,
        date: day.date,
        kind: "base_pay",
        amount: String(day.base_pay),
        comment: null,
      });
    }
    if (day.percent_pay > 0) {
      pushOperation({
        id: `pp-${day.date}`,
        date: day.date,
        kind: "percent_pay",
        amount: String(day.percent_pay),
        comment: null,
      });
    }
    if (day.vacation_pay > 0) {
      pushOperation({
        id: `vp-${day.date}`,
        date: day.date,
        kind: "vacation_pay",
        amount: String(day.vacation_pay),
        comment: null,
      });
    }
    if (day.fund_accrual > 0) {
      pushOperation({
        id: `fa-${day.date}`,
        date: day.date,
        kind: "fund_accrual",
        amount: String(day.fund_accrual),
        comment: null,
      });
    }
    if (day.ndfl_withheld > 0) {
      pushOperation({
        id: `ndfl-${day.date}`,
        date: day.date,
        kind: "ndfl",
        amount: String(day.ndfl_withheld),
        comment: null,
      });
    }
  }

  for (const adj of report.adjustments) {
    pushOperation({
      id: `adj-${adj.id}`,
      date: adj.work_date,
      kind: adj.type === "bonus" ? "premium" : "manual_penalty",
      amount: String(adj.amount),
      comment: adj.comment ?? adj.custom_label ?? adj.category_name ?? null,
    });
  }

  for (const tx of report.deposit_transactions) {
    const date = tx.created_at?.slice(0, 10) ?? "";
    let kind: OperationKind;
    if (tx.transaction_type === "accrual") {
      kind = "deposit_accrual";
    } else if (tx.transaction_type === "payout" || tx.transaction_type === "dismissal_payout") {
      kind = "deposit_payout";
    } else {
      kind = "deposit_writeoff";
    }
    pushOperation({
      id: `dep-${tx.id}`,
      date,
      kind,
      amount: String(tx.amount),
      comment: null,
    });
  }

  // TODO: Add fund_payout once the backend exposes it separately in the personal report.
  rows.sort((left, right) => {
    if (left.date !== right.date) {
      return left.date < right.date ? 1 : -1;
    }
    const kindOrder = KIND_ORDER[left.kind] - KIND_ORDER[right.kind];
    return kindOrder || left.id.localeCompare(right.id);
  });

  return rows;
}

function OperationsTable({ rows }: { rows: OperationRow[] }) {
  if (rows.length === 0) {
    return (
      <div className="rounded-md border bg-card px-3 py-6 text-center text-sm text-muted-foreground">
        Операций за период нет
      </div>
    );
  }

  return (
    <div className="overflow-x-auto rounded-md border bg-card">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b bg-muted/35">
            <th className="w-[110px] px-3 py-2 text-left font-medium">Дата</th>
            <th className="w-[180px] px-3 py-2 text-left font-medium">Тип</th>
            <th className="px-3 py-2 text-left font-medium">Комментарий</th>
            <th className="w-[140px] px-3 py-2 text-right font-medium">Сумма</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const meta = KIND_META[row.kind];
            const colorClass = meta.isDeduction ? "text-rose-700" : "text-emerald-700";
            return (
              <tr className="border-b last:border-b-0" key={row.id}>
                <td className="whitespace-nowrap px-3 py-2 align-top tabular-nums">
                  {row.date ? formatDate(row.date) : "—"}
                </td>
                <td className="px-3 py-2 align-top">
                  <Badge
                    variant="outline"
                    className={cn(
                      "border-transparent font-normal shadow-none",
                      meta.badgeClass,
                    )}
                  >
                    {meta.label}
                  </Badge>
                </td>
                <td className="min-w-56 px-3 py-2 align-top text-muted-foreground">
                  {row.comment ?? "—"}
                </td>
                <td
                  className={cn(
                    "whitespace-nowrap px-3 py-2 text-right align-top font-medium tabular-nums",
                    colorClass,
                  )}
                >
                  {formatOperationMoney(row)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function formatOperationMoney(row: OperationRow) {
  const amount = Number(row.amount);
  if (!Number.isFinite(amount)) {
    return "—";
  }
  const meta = KIND_META[row.kind];
  const sign = meta.isDeduction ? "−" : "+";
  return `${sign}${formatMoney(Math.abs(amount))}`;
}

function KpiSmall({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border bg-background p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-1 font-semibold tabular-nums">{value}</div>
    </div>
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
