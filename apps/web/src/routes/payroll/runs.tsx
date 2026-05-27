import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Banknote, CalendarClock, Eye, LoaderCircle, Play, RefreshCw } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import { EmptyState } from "@/components/ui-app/EmptyState";
import { PageHeader } from "@/components/ui-app/PageHeader";
import { StatusBadge } from "@/components/ui-app/StatusBadge";
import {
  autoCreateNextPayrollPeriod,
  createPayrollRun,
  getPayrollRunLines,
  getPayrollRuns,
  getSettings,
  type AppSetting,
  type PayrollLine,
  type PayrollPeriod,
  type PayrollRun,
} from "@/lib/api";
import { cn } from "@/lib/utils";

type PayrollRunsRouteProps = {
  onNavigate: (path: string) => void;
};

export function PayrollRunsRoute({ onNavigate }: PayrollRunsRouteProps) {
  const queryClient = useQueryClient();
  const runsQuery = useQuery({ queryKey: ["payroll-runs"], queryFn: getPayrollRuns });
  const settingsQuery = useQuery({
    queryKey: ["settings", "payroll-target-ratio"],
    queryFn: () => getSettings(),
  });

  const runs = [...(runsQuery.data ?? [])].sort(compareRunsDesc);
  const lineQueries = useQueries({
    queries: runs.map((run) => ({
      queryKey: ["payroll-run-lines", run.id],
      queryFn: () => getPayrollRunLines(run.id),
      enabled: runs.length > 0,
    })),
  });
  const linesByRunId = new Map<string, PayrollLine[]>();
  runs.forEach((run, index) => {
    linesByRunId.set(run.id, lineQueries[index]?.data ?? []);
  });

  const targetRatio = getTargetFotRatio(settingsQuery.data);
  const currentWindow = getPayrollWindow();
  const currentRun = runs.find((run) => isSamePeriod(run.period, currentWindow));
  const previousRun = getPreviousRun(runs, currentWindow.start_date);
  const monthRuns = getMonthRuns(runs, new Date());
  const currentLines = currentRun ? linesByRunId.get(currentRun.id) ?? [] : [];
  const previousLines = previousRun ? linesByRunId.get(previousRun.id) ?? [] : [];
  const monthTotal = monthRuns.reduce((sum, run) => sum + runTotal(run), 0);
  const monthRatios = monthRuns
    .map((run) => runPayrollRatio(run, linesByRunId.get(run.id) ?? []))
    .filter((ratio): ratio is number => ratio !== null);
  const monthAverageRatio =
    monthRatios.length > 0
      ? monthRatios.reduce((sum, ratio) => sum + ratio, 0) / monthRatios.length
      : null;
  const currentRatio = currentRun ? runPayrollRatio(currentRun, currentLines) : null;
  const previousRatio = previousRun ? runPayrollRatio(previousRun, previousLines) : null;
  const unfinishedCurrentRun =
    currentRun && !isFinalStatus(currentRun.status) ? currentRun : undefined;

  const runMutation = useMutation({
    mutationFn: async () => {
      const period = await autoCreateNextPayrollPeriod();
      return createPayrollRun(period.id);
    },
    onSuccess: async (run) => {
      await queryClient.invalidateQueries({ queryKey: ["payroll-runs"] });
      onNavigate(`/payroll/runs/${run.id}`);
    },
  });

  const tableColumns: Array<DataTableColumn<PayrollRun>> = [
    {
      key: "period",
      header: "Период",
      cell: (run) => (
        <div className="min-w-[180px]">
          <div className="font-medium">{formatPeriodRange(run.period)}</div>
          <div className="text-xs text-muted-foreground">{periodWeekLabel(run.period)}</div>
        </div>
      ),
    },
    {
      key: "payroll_date",
      header: "Дата выплаты",
      cell: (run) => (run.period ? formatDate(run.period.payroll_date) : "—"),
    },
    {
      key: "employees",
      header: "Сотрудников",
      cell: (run) => runEmployeeCount(run, linesByRunId.get(run.id) ?? []),
      className: "tabular-nums",
    },
    {
      key: "total",
      header: "ФОТ итого",
      cell: (run) => formatMoney(runTotal(run)),
      className: "font-medium tabular-nums",
    },
    {
      key: "ratio",
      header: "% от выручки",
      cell: (run) => formatRatio(runPayrollRatio(run, linesByRunId.get(run.id) ?? [])),
      className: "tabular-nums",
    },
    {
      key: "status",
      header: "Статус",
      cell: (run) => <StatusBadge status={run.status} />,
    },
    {
      key: "actions",
      header: "Действия",
      className: "text-right",
      cell: (run) => (
        <Button
          onClick={(event) => {
            event.stopPropagation();
            onNavigate(`/payroll/runs/${run.id}`);
          }}
          size="sm"
          variant="outline"
        >
          <Eye size={16} aria-hidden="true" />
          Открыть
        </Button>
      ),
    },
  ];

  return (
    <div className="space-y-5">
      <PageHeader
        title="Расчёты ЗП"
        description="Еженедельные расчёты вторник-понедельник, выплаты по вторникам."
        action={
          <Button
            onClick={() => void queryClient.invalidateQueries({ queryKey: ["payroll-runs"] })}
            title="Обновить"
            variant="outline"
          >
            <RefreshCw size={16} aria-hidden="true" />
            Обновить
          </Button>
        }
      />

      <section className="grid gap-3 lg:grid-cols-3">
        <PayrollMetric
          title="Эта неделя"
          value={currentRun ? formatMoney(runTotal(currentRun)) : "Не рассчитан"}
          description={
            currentRun
              ? `Выплата ${currentRun.period ? formatDate(currentRun.period.payroll_date) : "—"}`
              : `Выплата ${formatDate(currentWindow.payroll_date)}`
          }
          icon={<CalendarClock size={18} aria-hidden="true" />}
          footer={
            <div className="flex items-center gap-2">
              <StatusBadge status={currentRun ? normalizeRunStatus(currentRun.status) : "open"} />
              <span className="text-muted-foreground">{formatRatio(currentRatio)}</span>
            </div>
          }
        />
        <PayrollMetric
          title="Прошлая неделя"
          value={previousRun ? formatMoney(runTotal(previousRun)) : "—"}
          description={
            previousRun
              ? `${formatRatio(previousRatio)} от выручки, цель ${formatRatio(targetRatio)}`
              : `Цель ${formatRatio(targetRatio)}`
          }
          icon={<Banknote size={18} aria-hidden="true" />}
          footer={
            previousRatio === null ? (
              <span className="text-muted-foreground">Выручка не загружена</span>
            ) : (
              <RatioDelta ratio={previousRatio} target={targetRatio} />
            )
          }
        />
        <PayrollMetric
          title="За месяц"
          value={formatMoney(monthTotal)}
          description={`${monthRuns.length} ${pluralizeRun(monthRuns.length)}`}
          icon={<Banknote size={18} aria-hidden="true" />}
          footer={
            <span className="text-muted-foreground">
              Средний ФОТ {formatRatio(monthAverageRatio)}
            </span>
          }
        />
      </section>

      <section className="grid gap-4 rounded-lg border bg-card p-4 md:grid-cols-[1fr_auto] md:items-center">
        {unfinishedCurrentRun ? (
          <>
            <div>
              <div className="font-semibold">Расчёт за текущий период уже создан</div>
              <div className="mt-1 text-sm text-muted-foreground">
                {formatPeriodRange(unfinishedCurrentRun.period)} ·{" "}
                <StatusBadge status={unfinishedCurrentRun.status} />
              </div>
            </div>
            <Button onClick={() => onNavigate(`/payroll/runs/${unfinishedCurrentRun.id}`)}>
              Открыть детали
              <ArrowRight size={16} aria-hidden="true" />
            </Button>
          </>
        ) : !currentRun ? (
          <>
            <div>
              <div className="font-semibold">Готов к запуску новый расчёт</div>
              <div className="mt-1 text-sm text-muted-foreground">
                За неделю {formatPeriodRange(currentWindow)}
              </div>
            </div>
            <Button onClick={() => runMutation.mutate()} disabled={runMutation.isPending}>
              {runMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <Play size={16} aria-hidden="true" />
              )}
              Запустить расчёт за неделю {formatShortRange(currentWindow)}
            </Button>
          </>
        ) : (
          <>
            <div>
              <div className="font-semibold">Текущий период закрыт</div>
              <div className="mt-1 text-sm text-muted-foreground">
                {formatPeriodRange(currentRun.period)}
              </div>
            </div>
            <Button onClick={() => onNavigate(`/payroll/runs/${currentRun.id}`)} variant="outline">
              Открыть детали
              <ArrowRight size={16} aria-hidden="true" />
            </Button>
          </>
        )}
      </section>

      {runMutation.isError ? (
        <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {(runMutation.error as Error).message}
        </div>
      ) : null}

      {(runsQuery.data ?? []).length === 0 && !runsQuery.isLoading ? (
        <EmptyState
          icon={<Play className="h-5 w-5" aria-hidden="true" />}
          title="Расчётов пока нет"
          description="Запустите первый недельный расчёт, чтобы увидеть ведомость и KPI."
          action={
            <Button onClick={() => runMutation.mutate()} disabled={runMutation.isPending}>
              {runMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <Play size={16} aria-hidden="true" />
              )}
              Запустить расчёт за неделю {formatShortRange(currentWindow)}
            </Button>
          }
        />
      ) : (
        <DataTable
          columns={tableColumns}
          rows={runs}
          isLoading={runsQuery.isLoading}
          getRowKey={(run) => run.id}
          onRowClick={(run) => onNavigate(`/payroll/runs/${run.id}`)}
          emptyMessage="Расчётов пока нет"
        />
      )}
    </div>
  );
}

function PayrollMetric({
  description,
  footer,
  icon,
  title,
  value,
}: {
  description: string;
  footer: ReactNode;
  icon: ReactNode;
  title: string;
  value: string;
}) {
  return (
    <Card className="shadow-none">
      <CardContent className="grid gap-3 p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="text-sm text-muted-foreground">{title}</div>
            <div className="mt-2 truncate text-2xl font-semibold tabular-nums">{value}</div>
          </div>
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-accent text-accent-foreground">
            {icon}
          </div>
        </div>
        <div className="text-sm text-muted-foreground">{description}</div>
        <div className="min-h-6 text-sm">{footer}</div>
      </CardContent>
    </Card>
  );
}

function RatioDelta({ ratio, target }: { ratio: number; target: number }) {
  const isHigh = ratio > target;
  return (
    <span
      className={cn(
        "font-medium",
        isHigh ? "text-amber-700" : "text-emerald-700",
      )}
    >
      {isHigh ? "Выше цели" : "В пределах цели"}
    </span>
  );
}

function getPreviousRun(runs: PayrollRun[], currentStartDate: string) {
  return runs.find((run) => run.period && run.period.end_date < currentStartDate) ?? null;
}

function getMonthRuns(runs: PayrollRun[], date: Date) {
  const year = date.getFullYear();
  const month = date.getMonth();
  return runs.filter((run) => {
    const payrollDate = run.period?.payroll_date ?? run.finished_at ?? run.started_at;
    const value = new Date(payrollDate);
    return value.getFullYear() === year && value.getMonth() === month;
  });
}

function compareRunsDesc(a: PayrollRun, b: PayrollRun) {
  const left = a.period?.start_date ?? a.started_at;
  const right = b.period?.start_date ?? b.started_at;
  return Date.parse(right) - Date.parse(left);
}

function normalizeRunStatus(status: string) {
  if (status === "running") {
    return "in_progress";
  }
  return status;
}

function isFinalStatus(status: string) {
  return status === "finalized" || status === "final";
}

function isSamePeriod(period: PayrollPeriod | null, window: PayrollWindow) {
  return period?.start_date === window.start_date && period.end_date === window.end_date;
}

function runTotal(run: PayrollRun) {
  return Number(run.summary.total_payable ?? 0);
}

function runEmployeeCount(run: PayrollRun, lines: PayrollLine[]) {
  if (lines.length > 0) {
    return new Set(lines.map((line) => line.employee_id)).size;
  }
  return Number(run.summary.line_count ?? 0);
}

function runPayrollRatio(run: PayrollRun, lines: PayrollLine[]) {
  const revenue = runRevenue(lines);
  if (!revenue) {
    return null;
  }
  return runTotal(run) / revenue;
}

// eslint-disable-next-line react-refresh/only-export-components
export function runRevenue(lines: PayrollLine[]) {
  const revenueByDate = new Map<string, number>();
  for (const line of lines) {
    const days = Array.isArray(line.components.days) ? line.components.days : [];
    for (const day of days) {
      if (!isRecord(day)) {
        continue;
      }
      const date = String(day.date ?? "");
      const revenue = Number(day.daily_revenue ?? 0);
      if (date && revenue > 0 && !revenueByDate.has(date)) {
        revenueByDate.set(date, revenue);
      }
    }
  }
  return [...revenueByDate.values()].reduce((sum, value) => sum + value, 0);
}

function getTargetFotRatio(settings: AppSetting[] | undefined) {
  const setting = settings?.find((item) => item.key === "schedule.target_payroll_revenue_ratio");
  const value = Number(setting?.value ?? 0.28);
  return Number.isFinite(value) ? value : 0.28;
}

type PayrollWindow = Pick<PayrollPeriod, "start_date" | "end_date" | "payroll_date">;

function getPayrollWindow(today = new Date()): PayrollWindow {
  const payday = new Date(today);
  payday.setHours(12, 0, 0, 0);
  const daysSinceTuesday = (payday.getDay() - 2 + 7) % 7;
  payday.setDate(payday.getDate() - daysSinceTuesday);

  const startDate = new Date(payday);
  startDate.setDate(payday.getDate() - 7);
  const endDate = new Date(payday);
  endDate.setDate(payday.getDate() - 1);

  return {
    start_date: toDateKey(startDate),
    end_date: toDateKey(endDate),
    payroll_date: toDateKey(payday),
  };
}

function toDateKey(value: Date) {
  const year = value.getFullYear();
  const month = `${value.getMonth() + 1}`.padStart(2, "0");
  const day = `${value.getDate()}`.padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function periodWeekLabel(period: PayrollPeriod | null) {
  if (!period) {
    return "Период не задан";
  }
  return `Выплата ${formatDate(period.payroll_date)}`;
}

function formatShortRange(period: PayrollWindow | PayrollPeriod | null) {
  if (!period) {
    return "—";
  }
  return `${formatDay(period.start_date)}–${formatDay(period.end_date)}`;
}

// eslint-disable-next-line react-refresh/only-export-components
export function formatPeriodRange(period: PayrollWindow | PayrollPeriod | null) {
  if (!period) {
    return "Период не задан";
  }
  const start = new Date(period.start_date);
  const end = new Date(period.end_date);
  const sameMonth = start.getMonth() === end.getMonth() && start.getFullYear() === end.getFullYear();
  const endFormatter = new Intl.DateTimeFormat("ru-RU", {
    day: "numeric",
    month: "long",
    timeZone: "Europe/Moscow",
  });
  if (sameMonth) {
    return `${formatDay(period.start_date)}–${endFormatter.format(end)}`;
  }
  return `${endFormatter.format(start)} – ${endFormatter.format(end)}`;
}

function formatDay(value: string) {
  return new Intl.DateTimeFormat("ru-RU", {
    day: "numeric",
    timeZone: "Europe/Moscow",
  }).format(new Date(value));
}

// eslint-disable-next-line react-refresh/only-export-components
export function formatDate(value: string) {
  return new Intl.DateTimeFormat("ru-RU", { timeZone: "Europe/Moscow" }).format(new Date(value));
}

// eslint-disable-next-line react-refresh/only-export-components
export function formatDateTime(value: string | null) {
  if (!value) {
    return "—";
  }
  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "short",
    timeStyle: "short",
    timeZone: "Europe/Moscow",
  }).format(new Date(value));
}

// eslint-disable-next-line react-refresh/only-export-components
export function formatMoney(value: number) {
  return new Intl.NumberFormat("ru-RU", {
    maximumFractionDigits: 0,
    style: "currency",
    currency: "RUB",
  }).format(value);
}

// eslint-disable-next-line react-refresh/only-export-components
export function formatRatio(value: number | null) {
  if (value === null || !Number.isFinite(value)) {
    return "—";
  }
  return new Intl.NumberFormat("ru-RU", {
    maximumFractionDigits: 1,
    style: "percent",
  }).format(value);
}

function pluralizeRun(count: number) {
  const last = count % 10;
  const lastTwo = count % 100;
  if (last === 1 && lastTwo !== 11) {
    return "расчёт";
  }
  if (last >= 2 && last <= 4 && (lastTwo < 12 || lastTwo > 14)) {
    return "расчёта";
  }
  return "расчётов";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
