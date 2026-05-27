import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeft,
  ArrowUpDown,
  CheckCircle2,
  ExternalLink,
  LoaderCircle,
  RefreshCw,
  Search,
} from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import { EmptyState } from "@/components/ui-app/EmptyState";
import { PageHeader } from "@/components/ui-app/PageHeader";
import { StatusBadge } from "@/components/ui-app/StatusBadge";
import {
  finalizePayrollRun,
  getEmployees,
  getPayrollRun,
  getPayrollRunLines,
  getSettings,
  type AppSetting,
  type Employee,
  type PayrollLine,
} from "@/lib/api";
import { getAuthSnapshot } from "@/lib/auth";
import { cn } from "@/lib/utils";
import {
  formatDate,
  formatDateTime,
  formatMoney,
  formatPeriodRange,
  formatRatio,
  runRevenue,
} from "./runs";

type PayrollRunDetailRouteProps = {
  runId: string;
  onNavigate: (path: string) => void;
};

type SortKey = "name" | "role" | "hours" | "total";
type SortDirection = "asc" | "desc";

type PayrollLineRowModel = {
  line: PayrollLine;
  employee?: Employee;
  employeeName: string;
  hours: number;
};

export function PayrollRunDetailRoute({ runId, onNavigate }: PayrollRunDetailRouteProps) {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("name");
  const [sortDirection, setSortDirection] = useState<SortDirection>("asc");
  const [selectedLineId, setSelectedLineId] = useState<string | null>(null);

  const runQuery = useQuery({
    queryKey: ["payroll-run", runId],
    queryFn: () => getPayrollRun(runId),
  });
  const linesQuery = useQuery({
    queryKey: ["payroll-run-lines", runId],
    queryFn: () => getPayrollRunLines(runId),
  });
  const employeesQuery = useQuery({
    queryKey: ["employees", "payroll-line-map"],
    queryFn: () => getEmployees({ status: "all" }),
  });
  const settingsQuery = useQuery({
    queryKey: ["settings", "payroll-detail-target-ratio"],
    queryFn: () => getSettings(),
  });

  const employeesById = useMemo(() => {
    const map = new Map<string, Employee>();
    for (const employee of employeesQuery.data ?? []) {
      map.set(employee.id, employee);
    }
    return map;
  }, [employeesQuery.data]);

  const rows = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const prepared = (linesQuery.data ?? []).map((line) => {
      const employee = employeesById.get(line.employee_id);
      return {
        line,
        employee,
        employeeName: employee?.full_name ?? "Сотрудник требует настройки",
        hours: lineHours(line),
      };
    });

    return prepared
      .filter((row) => {
        if (!needle) {
          return true;
        }
        return (
          row.employeeName.toLowerCase().includes(needle) ||
          row.line.role.toLowerCase().includes(needle)
        );
      })
      .sort((left, right) => compareRows(left, right, sortKey, sortDirection));
  }, [employeesById, linesQuery.data, search, sortDirection, sortKey]);

  const selectedLine = rows.find((row) => row.line.id === selectedLineId) ?? null;
  const run = runQuery.data;
  const lines = linesQuery.data ?? [];
  const targetRatio = getTargetFotRatio(settingsQuery.data);
  const totalPayable = Number(run?.summary.total_payable ?? 0);
  const totalRevenue = runRevenue(lines);
  const payrollRatio = totalRevenue > 0 ? totalPayable / totalRevenue : null;
  const employeeCount = new Set(lines.map((line) => line.employee_id)).size;
  const totalHours = lines.reduce((sum, line) => sum + lineHours(line), 0);
  const blockers = run?.blocking_issues ?? [];
  const isFinal = run ? isFinalStatus(run.status) : false;
  const canManagePayroll = canFinalizeByRole();
  const canFinalize =
    Boolean(run) &&
    run?.status === "completed" &&
    blockers.length === 0 &&
    !isFinal &&
    canManagePayroll;

  const finalizeMutation = useMutation({
    mutationFn: () => finalizePayrollRun(runId),
    onSuccess: async () => {
      toast.success("Расчёт финализирован");
      await queryClient.invalidateQueries({ queryKey: ["payroll-run", runId] });
      await queryClient.invalidateQueries({ queryKey: ["payroll-runs"] });
    },
    onError: (mutationError) => toast.error((mutationError as Error).message),
  });

  function setSort(nextKey: SortKey) {
    if (nextKey === sortKey) {
      setSortDirection(sortDirection === "asc" ? "desc" : "asc");
      return;
    }
    setSortKey(nextKey);
    setSortDirection("asc");
  }

  function finalize() {
    if (isFinal || !canFinalize) {
      return;
    }
    if (
      !window.confirm("Финализировать расчёт? После закрытия повторный расчёт будет заблокирован.")
    ) {
      return;
    }
    finalizeMutation.mutate();
  }

  const tableColumns: Array<DataTableColumn<PayrollLineRowModel>> = [
    {
      key: "name",
      header: (
        <SortButton active={sortKey === "name"} onClick={() => setSort("name")}>
          Имя
        </SortButton>
      ),
      cell: (row) => (
        <div className="min-w-[220px]">
          <div className="font-medium">{row.employeeName}</div>
          <div className="text-xs text-muted-foreground">
            {row.employee?.position || "Роль из явок"}
          </div>
        </div>
      ),
    },
    {
      key: "role",
      header: (
        <SortButton active={sortKey === "role"} onClick={() => setSort("role")}>
          Роль
        </SortButton>
      ),
      cell: (row) => row.line.role || "Не задана",
    },
    {
      key: "hours",
      header: (
        <SortButton active={sortKey === "hours"} onClick={() => setSort("hours")}>
          Часов
        </SortButton>
      ),
      cell: (row) => formatHours(row.hours),
      className: "tabular-nums",
    },
    {
      key: "base",
      header: "Оклад",
      cell: (row) => formatMoney(row.line.base_pay),
      className: "tabular-nums",
    },
    {
      key: "premium",
      header: "Премия",
      cell: (row) => formatMoney(row.line.premium),
      className: "tabular-nums",
    },
    {
      key: "percent",
      header: "%",
      cell: (row) => formatMoney(row.line.percent_pay),
      className: "tabular-nums",
    },
    {
      key: "deduction",
      header: "Депозиты",
      cell: (row) => formatMoney(row.line.deduction),
      className: "tabular-nums",
    },
    {
      key: "total",
      header: (
        <SortButton active={sortKey === "total"} onClick={() => setSort("total")}>
          Итого
        </SortButton>
      ),
      cell: (row) => formatMoney(row.line.total_payable),
      className: "font-semibold tabular-nums",
    },
  ];

  return (
    <div className="space-y-5">
      <PageHeader
        title={run ? formatPeriodRange(run.period) : "Расчёт ЗП"}
        description={run ? runMeta(run) : "Загрузка расчёта"}
        action={
          <>
            {run ? <StatusBadge status={run.status} /> : null}
            <Button onClick={() => onNavigate("/payroll/runs")} title="Назад" variant="outline">
              <ArrowLeft size={16} aria-hidden="true" />
              Назад
            </Button>
            <Button
              onClick={() => {
                void queryClient.invalidateQueries({ queryKey: ["payroll-run", runId] });
                void queryClient.invalidateQueries({ queryKey: ["payroll-run-lines", runId] });
              }}
              title="Обновить"
              variant="outline"
            >
              <RefreshCw size={16} aria-hidden="true" />
              Обновить
            </Button>
            <Button onClick={finalize} disabled={!canFinalize || finalizeMutation.isPending}>
              {finalizeMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <CheckCircle2 size={16} aria-hidden="true" />
              )}
              Финализировать
            </Button>
          </>
        }
      />

      {blockers.length > 0 ? (
        <section className="rounded-lg border border-amber-200 bg-amber-50 p-4 text-amber-900">
          <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0" aria-hidden="true" />
            <div className="min-w-0 flex-1">
              <div className="font-semibold">
                Невозможно финализировать: {blockers.length}{" "}
                {pluralizeIssue(blockers.length)} в расчёте
              </div>
              <div className="mt-3 grid gap-2">
                {blockers.map((issue, index) => (
                  <BlockingIssue issue={issue} key={index} onNavigate={onNavigate} />
                ))}
              </div>
            </div>
          </div>
        </section>
      ) : null}

      <section className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <KpiCard title="Сотрудников" value={String(employeeCount)} description="В прогоне" />
        <KpiCard title="Часов отработано" value={formatHours(totalHours)} description="По iiko-явкам" />
        <KpiCard title="ФОТ итого" value={formatMoney(totalPayable)} description="К выплате" />
        <KpiCard
          title="% от выручки"
          value={formatRatio(payrollRatio)}
          description={`Порог ${formatRatio(targetRatio)}`}
          tone={payrollRatio !== null && payrollRatio > targetRatio ? "warning" : "default"}
        />
      </section>

      <section className="grid gap-3 rounded-lg border bg-card p-3 md:grid-cols-[minmax(220px,360px)_1fr] md:items-center">
        <div className="flex h-10 items-center gap-2 rounded-md border border-input bg-background px-3">
          <Search size={16} className="text-muted-foreground" aria-hidden="true" />
          <input
            className="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Сотрудник или роль"
            value={search}
          />
        </div>
        <div className="text-sm text-muted-foreground">
          {rows.length} {pluralizeEmployeeLine(rows.length)}
        </div>
      </section>

      {runQuery.isError ? (
        <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {(runQuery.error as Error).message}
        </div>
      ) : null}

      {(linesQuery.data ?? []).length === 0 && !linesQuery.isLoading ? (
        <EmptyState
          icon={<AlertTriangle className="h-5 w-5" aria-hidden="true" />}
          title="Строк расчёта нет"
          description="После успешного запуска здесь появятся сотрудники и суммы к выплате."
        />
      ) : (
        <DataTable
          columns={tableColumns}
          rows={rows}
          isLoading={linesQuery.isLoading || runQuery.isLoading}
          getRowKey={(row) => row.line.id}
          onRowClick={(row) => setSelectedLineId(row.line.id)}
          emptyMessage="Сотрудники по фильтру не найдены"
        />
      )}

      <Sheet
        open={Boolean(selectedLine)}
        onOpenChange={(open) => {
          if (!open) {
            setSelectedLineId(null);
          }
        }}
      >
        <SheetContent className="w-full overflow-y-auto sm:max-w-2xl" side="right">
          {selectedLine ? <PayrollLineDrawer row={selectedLine} /> : null}
        </SheetContent>
      </Sheet>
    </div>
  );
}

function KpiCard({
  description,
  title,
  tone = "default",
  value,
}: {
  description: string;
  title: string;
  tone?: "default" | "warning";
  value: string;
}) {
  return (
    <Card
      className={cn(
        "shadow-none",
        tone === "warning" ? "border-amber-200 bg-amber-50" : undefined,
      )}
    >
      <CardContent className="p-4">
        <div className="text-sm text-muted-foreground">{title}</div>
        <div className="mt-2 text-2xl font-semibold tabular-nums">{value}</div>
        <div className="mt-2 text-sm text-muted-foreground">{description}</div>
      </CardContent>
    </Card>
  );
}

function SortButton({
  active,
  children,
  onClick,
}: {
  active: boolean;
  children: string;
  onClick: () => void;
}) {
  return (
    <button
      className={cn(
        "inline-flex items-center gap-1 text-xs font-semibold uppercase",
        active ? "text-foreground" : "text-muted-foreground",
      )}
      onClick={(event) => {
        event.stopPropagation();
        onClick();
      }}
      type="button"
    >
      {children}
      <ArrowUpDown size={13} aria-hidden="true" />
    </button>
  );
}

function BlockingIssue({
  issue,
  onNavigate,
}: {
  issue: Record<string, unknown>;
  onNavigate: (path: string) => void;
}) {
  const type = String(issue.type ?? "issue");
  const employeeName = readableEmployeeName(issue);
  const workDate = String(issue.work_date ?? issue.date ?? "");
  const staffAction = shouldOpenStaff(type);
  const shiftAction = shouldOpenShift(type);

  return (
    <div className="grid gap-3 rounded-md border border-amber-200 bg-background p-3 md:grid-cols-[1fr_auto] md:items-center">
      <div className="min-w-0">
        <div className="font-medium">{issueTitle(type)}</div>
        <div className="mt-1 text-sm text-muted-foreground">
          {[employeeName, workDate ? formatDate(workDate) : null].filter(Boolean).join(" · ") ||
            "Проверьте данные явок и настройки сотрудника."}
        </div>
      </div>
      <div className="flex flex-wrap gap-2">
        {staffAction ? (
          <Button onClick={() => onNavigate("/staff")} size="sm" variant="outline">
            <ExternalLink size={15} aria-hidden="true" />
            Перейти в Штат
          </Button>
        ) : null}
        {shiftAction ? (
          <Button onClick={() => onNavigate("/schedule")} size="sm" variant="outline">
            <ExternalLink size={15} aria-hidden="true" />
            Открыть смену
          </Button>
        ) : null}
      </div>
    </div>
  );
}

function PayrollLineDrawer({ row }: { row: PayrollLineRowModel }) {
  const days = lineDays(row.line);

  return (
    <div className="space-y-5">
      <SheetHeader>
        <SheetTitle className="pr-8">{row.employeeName}</SheetTitle>
        <SheetDescription>
          {row.line.role || "Роль не задана"} · {formatHours(row.hours)}
        </SheetDescription>
      </SheetHeader>

      <section className="grid gap-3 sm:grid-cols-2">
        <ComponentValue label="Оклад" value={formatMoney(row.line.base_pay)} />
        <ComponentValue label="Премия" value={formatMoney(row.line.premium)} />
        <ComponentValue label="Процент" value={formatMoney(row.line.percent_pay)} />
        <ComponentValue label="Депозиты" value={formatMoney(row.line.deduction)} />
        <ComponentValue label="Фонд" value={formatMoney(row.line.fund_accrual)} />
        <ComponentValue label="К выплате" value={formatMoney(row.line.total_payable)} strong />
      </section>

      <section className="space-y-3">
        <div className="text-sm font-semibold">Смены и компоненты</div>
        {days.length > 0 ? (
          <div className="grid gap-2">
            {days.map((day) => (
              <div className="rounded-lg border bg-card p-3" key={`${day.date}-${day.role}`}>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <div className="font-medium">{formatDate(day.date)}</div>
                    <div className="text-sm text-muted-foreground">
                      {day.role} · {formatHours(day.hours)}
                    </div>
                  </div>
                  {day.category ? (
                    <Badge className="rounded-md border-border bg-background text-foreground shadow-none">
                      {day.category}
                    </Badge>
                  ) : null}
                </div>
                <div className="mt-3 grid gap-2 text-sm sm:grid-cols-3">
                  <ComponentValue label="Оклад" value={formatMoney(day.basePay)} dense />
                  <ComponentValue label="%" value={formatMoney(day.percentPay)} dense />
                  <ComponentValue label="Фонд" value={formatMoney(day.fundAccrual)} dense />
                </div>
                {day.dailyRevenue > 0 ? (
                  <div className="mt-2 text-sm text-muted-foreground">
                    Выручка дня {formatMoney(day.dailyRevenue)}
                  </div>
                ) : null}
              </div>
            ))}
          </div>
        ) : (
          <EmptyState
            title="Детализация смен не загружена"
            description="В строке есть итоговые суммы, но нет дневных компонентов."
          />
        )}
      </section>
    </div>
  );
}

function ComponentValue({
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

function runMeta(run: { started_at: string; finished_at: string | null; period: { finalized_at: string | null } | null }) {
  const parts = [`Создан ${formatDateTime(run.started_at)}`];
  if (run.finished_at) {
    parts.push(`посчитан ${formatDateTime(run.finished_at)}`);
  }
  if (run.period?.finalized_at) {
    parts.push(`финализирован ${formatDateTime(run.period.finalized_at)}`);
  }
  return parts.join(" · ");
}

function compareRows(
  left: PayrollLineRowModel,
  right: PayrollLineRowModel,
  sortKey: SortKey,
  direction: SortDirection,
) {
  const modifier = direction === "asc" ? 1 : -1;
  if (sortKey === "hours") {
    return (left.hours - right.hours) * modifier;
  }
  if (sortKey === "total") {
    return (left.line.total_payable - right.line.total_payable) * modifier;
  }
  if (sortKey === "role") {
    return left.line.role.localeCompare(right.line.role, "ru") * modifier;
  }
  return left.employeeName.localeCompare(right.employeeName, "ru") * modifier;
}

function lineHours(line: PayrollLine) {
  return lineDays(line).reduce((sum, day) => sum + day.hours, 0);
}

type DayComponent = {
  date: string;
  role: string;
  category: string;
  hours: number;
  basePay: number;
  percentPay: number;
  fundAccrual: number;
  dailyRevenue: number;
};

function lineDays(line: PayrollLine): DayComponent[] {
  const days = Array.isArray(line.components.days) ? line.components.days : [];
  return days.filter(isRecord).map((day) => ({
    date: String(day.date ?? ""),
    role: String(day.role ?? line.role),
    category: String(day.category ?? ""),
    hours: Number(day.hours ?? 0),
    basePay: Number(day.base_pay ?? 0),
    percentPay: Number(day.percent_pay ?? 0),
    fundAccrual: Number(day.fund_accrual ?? 0),
    dailyRevenue: Number(day.daily_revenue ?? 0),
  }));
}

function canFinalizeByRole() {
  const user = getAuthSnapshot().user;
  if (!user) {
    return true;
  }
  return user.roles.some((role) => ["finance_manager", "owner", "admin"].includes(role));
}

function isFinalStatus(status: string) {
  return status === "finalized" || status === "final";
}

function getTargetFotRatio(settings: AppSetting[] | undefined) {
  const setting = settings?.find((item) => item.key === "schedule.target_payroll_revenue_ratio");
  const value = Number(setting?.value ?? 0.28);
  return Number.isFinite(value) ? value : 0.28;
}

function readableEmployeeName(issue: Record<string, unknown>) {
  const value = issue.employee_name ?? issue.full_name ?? issue.name;
  return typeof value === "string" ? value : "";
}

function issueTitle(type: string) {
  const labels: Record<string, string> = {
    needs_setup: "Сотрудник требует настройки",
    unknown_employee: "Неизвестный сотрудник в iiko-явках",
    missing_payroll_role: "Не указана роль для расчёта",
    missing_category: "Не указана категория сотрудника",
    missing_rate: "Не настроена ставка",
    attendance_quality_review: "Явка требует проверки",
    post_termination_attendance: "Явка после увольнения",
    missing_attendance: "Нет явок за период",
  };
  return labels[type] ?? "Блокер расчёта";
}

function shouldOpenStaff(type: string) {
  return (
    type.includes("employee") ||
    type.includes("category") ||
    type.includes("rate") ||
    type === "needs_setup"
  );
}

function shouldOpenShift(type: string) {
  return type.includes("attendance") || type.includes("shift");
}

function formatHours(value: number) {
  return `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format(value)} ч`;
}

function pluralizeIssue(count: number) {
  const last = count % 10;
  const lastTwo = count % 100;
  if (last === 1 && lastTwo !== 11) {
    return "блокер";
  }
  if (last >= 2 && last <= 4 && (lastTwo < 12 || lastTwo > 14)) {
    return "блокера";
  }
  return "блокеров";
}

function pluralizeEmployeeLine(count: number) {
  const last = count % 10;
  const lastTwo = count % 100;
  if (last === 1 && lastTwo !== 11) {
    return "строка";
  }
  if (last >= 2 && last <= 4 && (lastTwo < 12 || lastTwo > 14)) {
    return "строки";
  }
  return "строк";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
