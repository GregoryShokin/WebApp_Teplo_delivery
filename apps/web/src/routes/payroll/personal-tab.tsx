import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, LoaderCircle } from "lucide-react";
import { Fragment, useEffect, useMemo, useState } from "react";
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
import { EmployeeCombobox } from "@/components/ui-app/EmployeeCombobox";
import { EmptyState } from "@/components/ui-app/EmptyState";
import {
  apiErrorMessage,
  getEmployeePayrollReport,
  getEmployees,
  type PayrollPersonalReport,
} from "@/lib/api";
import { PAYROLL_ROLE_LABELS } from "@/lib/i18n/employee";
import { roleColorClasses } from "@/lib/role-colors";
import { cn } from "@/lib/utils";
import { formatDate, formatMoney } from "./runs";

function payrollRoleLabel(role: string | null | undefined): string {
  if (!role) {
    return "—";
  }
  return PAYROLL_ROLE_LABELS[role as keyof typeof PAYROLL_ROLE_LABELS] ?? role;
}

// Чипы ролей расчётки: каждая роль — своим цветом (единая палитра ролей). Для объединённой
// расчётки повара (пиццерист+сушист) показываем оба чипа; пустой список — фолбэк на текст.
function RoleChips({ roles, fallback }: { roles?: string[]; fallback?: string | null }) {
  const list = roles && roles.length > 0 ? roles : fallback ? [fallback] : [];
  if (list.length === 0) {
    return <span className="text-muted-foreground">—</span>;
  }
  return (
    <span className="flex flex-wrap gap-1">
      {list.map((role) => {
        const colors = roleColorClasses(role);
        return (
          <span
            className={cn(
              "inline-flex h-5 items-center rounded-sm border px-1.5 text-[11px] leading-none",
              colors.container,
              colors.primaryText,
            )}
            key={role}
          >
            {payrollRoleLabel(role)}
          </span>
        );
      })}
    </span>
  );
}

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
  // Роль смены (для подсветки в подневной таблице) — у оклада/процента.
  role?: string | null;
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
    label: "Выдача депозита",
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
  // Персональный отчёт: сотрудники, получающие зарплату (кроме обычных курьеров). Признак
  // in_personal_report вычисляется на бэке из должности + admin_payroll_excluded.
  const eligibleEmployees = useMemo(
    () =>
      (employeesQuery.data ?? [])
        .filter((employee) => employee.in_personal_report)
        .sort((left, right) => left.full_name.localeCompare(right.full_name, "ru")),
    [employeesQuery.data],
  );
  // Уволенные (inactive) скрыты по умолчанию; toggle добавляет их. Сотрудники «в
  // увольнении» (dismissing) видны как активные — расчёты по ним ещё идут, их карточка
  // нужна в персональном отчёте до полного закрытия долга.
  const statusFilteredEmployees = useMemo(
    () =>
      eligibleEmployees.filter(
        (employee) =>
          showFired || employee.status === "active" || employee.status === "dismissing",
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
    // `report.daily` — это сводная история сотрудника, которая может включать
    // несколько пересчётов одной недели. Для ведомости берём только компоненты
    // выбранного run_id: это тот же источник, из которого получаются её KPI.
    const rows = buildPayslipOperations(openWeek);
    // Запланированную выдачу депозита показываем из данных периода: она привязана к ведомости,
    // тогда как deposit_transaction датирована временем прогона и может выпасть из периода.
    if (openWeek.deposit_payout > 0 && !rows.some((row) => row.kind === "deposit_payout")) {
      rows.push({
        id: `dep-payout-${openWeek.period_id}-${openWeek.run_id}`,
        date: openWeek.period_end,
        kind: "deposit_payout",
        amount: String(openWeek.deposit_payout),
        comment: null,
        periodStart: openWeek.period_start,
      });
      rows.sort((left, right) => {
        if (left.date !== right.date) {
          return left.date < right.date ? 1 : -1;
        }
        return KIND_ORDER[left.kind] - KIND_ORDER[right.kind] || left.id.localeCompare(right.id);
      });
    }
    return rows;
  }, [openWeek]);
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
      cell: (row) => <RoleChips fallback={row.role} roles={row.roles?.map((item) => item.role)} />,
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
      key: "deposit_payout",
      header: "Выдача депозита",
      cell: (row) => formatMoney(row.deposit_payout),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "total_payable",
      header: "К выплате",
      // «На руки» = ФОТ (total_payable) + выдача депозита (в total_payable не входит).
      cell: (row) => formatMoney(row.total_payable + row.deposit_payout),
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
                  value={formatMoney(report.totals.total_payable + report.totals.deposit_payout)}
                  description={
                    report.totals.deposit_payout > 0
                      ? "ФОТ + выдача депозита"
                      : "По найденным периодам"
                  }
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
                  value={formatMoney(report.fund_accumulated)}
                  description="Накоплено (леджер фонда)"
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
                    <PeriodLedgers
                      columns={periodColumns}
                      employeePosition={report.employee_position}
                      onRowClick={setOpenWeek}
                      periods={report.periods}
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
                  <PeriodLedgers
                    columns={periodColumns}
                    employeePosition={report.employee_position}
                    periods={report.periods}
                  />
                </section>
              ) : null}

              <PayslipDialog
                open={openWeek !== null}
                onOpenChange={(open) => !open && setOpenWeek(null)}
                period={openWeek}
                employeeName={report.employee_name}
                operations={openWeekOperations}
                fundReportTotal={report.fund_accumulated}
              />
            </>
          )}
        </div>
      ) : null}
    </section>
  );
}

// Детализация периодов: обычно одна таблица. Для «субститута» (сотрудник с окладом по
// замещаемой должности — например повар/кассир, исполняющий Помощника менеджера) сверху
// добавляется переключатель Основная / Замещающая должность; по умолчанию — основная.
function PeriodLedgers({
  periods,
  columns,
  employeePosition,
  onRowClick,
}: {
  periods: PayrollPersonalReportPeriod[];
  columns: Array<DataTableColumn<PayrollPersonalReportPeriod>>;
  employeePosition: string | null;
  onRowClick?: (row: PayrollPersonalReportPeriod) => void;
}) {
  const [ledgerView, setLedgerView] = useState<"main" | "substitute">("main");
  const rowKey = (row: PayrollPersonalReportPeriod) =>
    `${row.period_id}-${row.run_id}-${row.role}`;
  const hoverClass = onRowClick ? "hover:bg-muted/50" : undefined;
  const substitute = periods.filter((period) => period.is_substitute);

  const table = (rows: PayrollPersonalReportPeriod[]) => (
    <DataTable
      columns={columns}
      rows={rows}
      getRowKey={rowKey}
      onRowClick={onRowClick}
      rowClassName={hoverClass}
      emptyMessage="Нет начислений за выбранный период"
    />
  );

  if (substitute.length === 0) {
    return table(periods);
  }

  const main = periods.filter((period) => !period.is_substitute);
  const substituteLabel = substitute[0]?.role ?? "Замещающая должность";
  const rows = ledgerView === "substitute" ? substitute : main;

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-3">
        <div className="inline-flex w-fit overflow-hidden rounded-md border">
          <button
            className={`px-4 py-1.5 text-sm ${
              ledgerView === "main" ? "bg-primary/10 font-medium text-primary" : ""
            }`}
            onClick={() => setLedgerView("main")}
            type="button"
          >
            Основная должность
          </button>
          <button
            className={`px-4 py-1.5 text-sm ${
              ledgerView === "substitute" ? "bg-primary/10 font-medium text-primary" : ""
            }`}
            onClick={() => setLedgerView("substitute")}
            type="button"
          >
            Замещающая должность
          </button>
        </div>
        <span className="text-xs text-muted-foreground">
          {ledgerView === "substitute" ? substituteLabel : employeePosition ?? "—"}
        </span>
      </div>
      {table(rows)}
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
        role: day.role,
      });
    }
    if (day.percent_pay > 0) {
      pushOperation({
        id: `pp-${day.date}`,
        date: day.date,
        kind: "percent_pay",
        amount: String(day.percent_pay),
        comment: null,
        role: day.role,
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

function buildPayslipOperations(period: PayrollPersonalReportPeriod): OperationRow[] {
  const rows: OperationRow[] = [];
  for (const day of period.days ?? []) {
    const date = String(day.date ?? "");
    if (!date) continue;
    const add = (kind: OperationKind, value: unknown) => {
      const amount = Number(value ?? 0);
      if (amount <= 0) return;
      rows.push({
        id: `${period.run_id}-${kind}-${date}-${rows.length}`,
        date,
        kind,
        amount: String(amount),
        comment: null,
        role: typeof day.role === "string" ? day.role : null,
      });
    };
    add("base_pay", day.base_pay);
    add("percent_pay", day.percent_pay);
    add("vacation_pay", day.vacation_pay);
    add("ndfl", day.ndfl_withheld);
  }

  const adjustments = period.adjustments ?? {};
  for (const [source, kind] of [
    ["bonuses", "premium"],
    ["penalties", "manual_penalty"],
  ] as const) {
    for (const adjustment of adjustments[source] ?? []) {
      const date = String(adjustment.work_date ?? "");
      const amount = Number(adjustment.amount ?? 0);
      if (!date || amount <= 0) continue;
      rows.push({
        id: `${period.run_id}-${source}-${String(adjustment.id ?? rows.length)}`,
        date,
        kind,
        amount: String(amount),
        comment:
          typeof adjustment.comment === "string"
            ? adjustment.comment
            : typeof adjustment.category === "string"
              ? adjustment.category
              : null,
      });
    }
  }

  rows.sort((left, right) => {
    if (left.date !== right.date) return left.date < right.date ? 1 : -1;
    return KIND_ORDER[left.kind] - KIND_ORDER[right.kind] || left.id.localeCompare(right.id);
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
                  <span className="flex flex-wrap items-center gap-1.5">
                    <Badge
                      variant="outline"
                      className={cn(
                        "border-transparent font-normal shadow-none",
                        meta.badgeClass,
                      )}
                    >
                      {meta.label}
                    </Badge>
                    {row.role ? <RoleChips roles={[row.role]} /> : null}
                  </span>
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

const PAYSLIP_COLUMNS: Array<{ kind: OperationKind; withComment: boolean }> = [
  { kind: "base_pay", withComment: false },
  { kind: "percent_pay", withComment: false },
  { kind: "vacation_pay", withComment: false },
  { kind: "premium", withComment: true },
  { kind: "deposit_payout", withComment: false },
  { kind: "manual_penalty", withComment: true },
  { kind: "audit_penalty", withComment: false },
  { kind: "deposit_accrual", withComment: false },
  { kind: "deposit_writeoff", withComment: false },
  { kind: "ndfl", withComment: false },
];
// Фонд показываем отдельным KPI-виджетом, а не колонкой детализации (накопление вне «к выплате»).
const PAYSLIP_FUND_KINDS = new Set<OperationKind>(["fund_accrual", "fund_payout"]);

function formatPlainAmount(value: number) {
  return new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 }).format(Math.abs(value));
}

// Pivot-ведомость: строки — даты, колонки — типы операций (с парным комментарием у премии и
// штрафа), крайняя колонка — нетто за день. Колонки строятся динамически по присутствующим типам.
function PayslipDialog({
  open,
  onOpenChange,
  period,
  employeeName,
  operations,
  fundReportTotal,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  period: PayrollPersonalReportPeriod | null;
  employeeName: string;
  operations: OperationRow[];
  fundReportTotal: number;
}) {
  const tableOps = useMemo(
    () => operations.filter((row) => row.date && !PAYSLIP_FUND_KINDS.has(row.kind)),
    [operations],
  );
  const dates = useMemo(() => {
    const set = new Set(tableOps.map((row) => row.date));
    return [...set].sort((left, right) => (left < right ? 1 : -1));
  }, [tableOps]);
  const columns = useMemo(
    () => PAYSLIP_COLUMNS.filter((col) => tableOps.some((row) => row.kind === col.kind)),
    [tableOps],
  );

  const cell = (date: string, kind: OperationKind) => {
    const matched = tableOps.filter((row) => row.date === date && row.kind === kind);
    const amount = matched.reduce((acc, row) => acc + Number(row.amount || 0), 0);
    const comment = matched
      .map((row) => row.comment)
      .filter((value): value is string => Boolean(value))
      .join("; ");
    return { amount, comment };
  };
  const dayTotal = (date: string) =>
    tableOps
      .filter((row) => row.date === date)
      .reduce((acc, row) => {
        const value = Number(row.amount || 0);
        return acc + (KIND_META[row.kind].isDeduction ? -value : value);
      }, 0);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-5xl">
        <DialogHeader>
          <DialogTitle>
            Ведомость {period ? formatDate(period.period_start) : ""} —{" "}
            {period ? formatDate(period.period_end) : ""}
          </DialogTitle>
          <DialogDescription>
            {employeeName} · {period?.role}
          </DialogDescription>
        </DialogHeader>
        {period ? (
          <>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
              <KpiSmall label="Оклад" value={formatMoney(period.base_pay)} />
              <KpiSmall label="Процент" value={formatMoney(period.percent_pay)} />
              <KpiSmall label="Удержано" value={formatMoney(period.deduction)} />
              <div className="rounded-md border border-emerald-200 bg-emerald-50 p-3">
                <div className="text-xs text-emerald-700">К выплате</div>
                <div className="mt-1 font-semibold tabular-nums text-emerald-700">
                  {formatMoney(period.total_payable + period.deposit_payout)}
                </div>
              </div>
              <div className="rounded-md border border-violet-200 bg-violet-50 p-3">
                <div className="text-xs text-violet-700">Накопит. фонд</div>
                <div className="mt-1 flex items-baseline gap-1.5">
                  <span className="font-semibold tabular-nums text-violet-700">
                    +{formatMoney(period.fund_accrual)}
                  </span>
                  <span className="text-xs text-violet-600">за ведомость</span>
                </div>
                <div className="text-xs text-violet-600">
                  накоплено: {formatMoney(fundReportTotal)}
                </div>
              </div>
            </div>

            {dates.length === 0 ? (
              <div className="rounded-md border bg-card px-3 py-6 text-center text-sm text-muted-foreground">
                Операций за период нет
              </div>
            ) : (
              <div className="overflow-x-auto rounded-md border bg-card">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b bg-muted/35 text-xs text-muted-foreground">
                      <th className="px-2 py-2 text-left font-medium">Дата</th>
                      {columns.map((col, index) => (
                        <Fragment key={col.kind}>
                          <th
                            className={cn(
                              "whitespace-nowrap px-2 py-2 text-right font-medium",
                              index > 0 && "border-l",
                            )}
                          >
                            {KIND_META[col.kind].label}
                          </th>
                          {col.withComment ? (
                            <th className="px-2 py-2 text-left font-medium">Комментарий</th>
                          ) : null}
                        </Fragment>
                      ))}
                      <th className="whitespace-nowrap border-l-2 px-2 py-2 text-right font-medium">
                        Итог дня
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {dates.map((date) => {
                      const total = dayTotal(date);
                      return (
                        <tr className="border-b last:border-b-0 even:bg-muted/30" key={date}>
                          <td className="whitespace-nowrap px-2 py-2 align-top tabular-nums text-muted-foreground">
                            {formatDate(date)}
                          </td>
                          {columns.map((col, index) => {
                            const data = cell(date, col.kind);
                            const meta = KIND_META[col.kind];
                            return (
                              <Fragment key={col.kind}>
                                <td
                                  className={cn(
                                    "whitespace-nowrap px-2 py-2 text-right align-top font-medium tabular-nums",
                                    index > 0 && "border-l",
                                    data.amount === 0
                                      ? "text-muted-foreground"
                                      : meta.isDeduction
                                        ? "text-rose-700"
                                        : "text-emerald-700",
                                  )}
                                >
                                  {data.amount === 0
                                    ? ""
                                    : `${meta.isDeduction ? "−" : "+"}${formatPlainAmount(data.amount)}`}
                                </td>
                                {col.withComment ? (
                                  <td className="px-2 py-2 align-top text-xs leading-snug text-muted-foreground">
                                    {data.comment || ""}
                                  </td>
                                ) : null}
                              </Fragment>
                            );
                          })}
                          <td
                            className={cn(
                              "whitespace-nowrap border-l-2 px-2 py-2 text-right align-top font-semibold tabular-nums",
                              total < 0 ? "text-rose-700" : "text-emerald-700",
                            )}
                          >
                            {total < 0 ? "−" : "+"}
                            {formatPlainAmount(total)}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </>
        ) : null}
      </DialogContent>
    </Dialog>
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
