import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Banknote, RefreshCw, Settings, UserRoundCheck } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/ui-app/EmptyState";
import { PageHeader } from "@/components/ui-app/PageHeader";
import { getEmployees, getPayrollRuns, getSettings, type PayrollRun } from "@/lib/api";

type DashboardPageProps = {
  onNavigate: (path: string) => void;
};

export function DashboardPage({ onNavigate }: DashboardPageProps) {
  const queryClient = useQueryClient();
  const payrollRunsQuery = useQuery({ queryKey: ["payroll-runs"], queryFn: getPayrollRuns });
  const employeesQuery = useQuery({
    queryKey: ["employees", "dashboard-active"],
    queryFn: () => getEmployees({ status: "active" }),
  });
  const settingsQuery = useQuery({
    queryKey: ["settings", "dashboard-count"],
    queryFn: () => getSettings(),
  });

  const weeklyRun = getCurrentWeekRun(payrollRunsQuery.data ?? []);
  const weeklyTotal = Number(weeklyRun?.summary.total_payable ?? 0);
  const activeEmployees = employeesQuery.data?.length ?? 0;
  const activeSettings = settingsQuery.data?.length ?? 0;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Главная"
        description="Ключевые показатели для зарплаты, штата и настроек."
        action={
          <Button
            onClick={() => {
              void queryClient.invalidateQueries({ queryKey: ["payroll-runs"] });
              void queryClient.invalidateQueries({ queryKey: ["employees"] });
              void queryClient.invalidateQueries({ queryKey: ["settings"] });
            }}
            variant="outline"
          >
            <RefreshCw aria-hidden="true" />
            Обновить
          </Button>
        }
      />

      <section className="grid gap-4 md:grid-cols-3">
        {payrollRunsQuery.isLoading ? (
          <MetricSkeleton title="Зарплата за эту неделю" />
        ) : weeklyRun ? (
          <MetricCard
            description={weeklyRun.period ? weekLabel(weeklyRun) : "Последний доступный расчёт"}
            icon={<Banknote className="h-5 w-5" aria-hidden="true" />}
            title="Зарплата за эту неделю"
            value={formatMoney(weeklyTotal)}
          />
        ) : (
          <EmptyState
            icon={<Banknote className="h-5 w-5" aria-hidden="true" />}
            title="Зарплата за эту неделю"
            description="Расчётов пока нет."
            action={<Button onClick={() => onNavigate("/payroll/runs")}>Перейти к расчётам</Button>}
          />
        )}

        {employeesQuery.isLoading ? (
          <MetricSkeleton title="Сотрудников активно" />
        ) : activeEmployees > 0 ? (
          <MetricCard
            description="Сотрудники со статусом «Активен»"
            icon={<UserRoundCheck className="h-5 w-5" aria-hidden="true" />}
            title="Сотрудников активно"
            value={String(activeEmployees)}
          />
        ) : (
          <EmptyState
            icon={<UserRoundCheck className="h-5 w-5" aria-hidden="true" />}
            title="Сотрудников активно"
            description="Активных сотрудников пока нет."
            action={<Button onClick={() => onNavigate("/staff")}>Открыть штат</Button>}
          />
        )}

        {settingsQuery.isLoading ? (
          <MetricSkeleton title="Настроек активно" />
        ) : activeSettings > 0 ? (
          <MetricCard
            description="Параметры, доступные в рабочем контуре"
            icon={<Settings className="h-5 w-5" aria-hidden="true" />}
            title="Настроек активно"
            value={String(activeSettings)}
          />
        ) : (
          <EmptyState
            icon={<Settings className="h-5 w-5" aria-hidden="true" />}
            title="Настроек активно"
            description="Настройки ещё не загружены."
            action={<Button onClick={() => onNavigate("/settings")}>Открыть настройки</Button>}
          />
        )}
      </section>
    </div>
  );
}

function MetricCard({
  description,
  icon,
  title,
  value,
}: {
  description: string;
  icon: ReactNode;
  title: string;
  value: string;
}) {
  return (
    <Card className="shadow-none">
      <CardHeader className="space-y-0 p-4 pb-2">
        <div className="flex items-start justify-between gap-3">
          <div>
            <CardDescription>{title}</CardDescription>
            <CardTitle className="mt-3 text-2xl tabular-nums">{value}</CardTitle>
          </div>
          <div className="flex h-10 w-10 items-center justify-center rounded-md bg-accent text-accent-foreground">
            {icon}
          </div>
        </div>
      </CardHeader>
      <CardContent className="px-4 pb-4 pt-0 text-sm text-muted-foreground">
        {description}
      </CardContent>
    </Card>
  );
}

function MetricSkeleton({ title }: { title: string }) {
  return (
    <Card className="shadow-none">
      <CardHeader className="p-4 pb-2">
        <CardDescription>{title}</CardDescription>
        <Skeleton className="mt-3 h-8 w-28" />
      </CardHeader>
      <CardContent className="px-4 pb-4 pt-0">
        <Skeleton className="h-5 w-44" />
      </CardContent>
    </Card>
  );
}

function getCurrentWeekRun(runs: PayrollRun[]) {
  if (runs.length === 0) {
    return null;
  }

  const now = new Date();
  const runInCurrentWeek = runs.find((run) => {
    if (!run.period) {
      return false;
    }
    const startDate = new Date(run.period.start_date);
    const endDate = new Date(run.period.end_date);
    return startDate <= now && now <= endDate;
  });

  return (
    runInCurrentWeek ??
    [...runs].sort((a, b) => Date.parse(b.started_at) - Date.parse(a.started_at))[0]
  );
}

function weekLabel(run: PayrollRun) {
  if (!run.period) {
    return "Последний доступный расчёт";
  }

  return `${formatDate(run.period.start_date)} - ${formatDate(run.period.end_date)}`;
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("ru-RU", { timeZone: "Europe/Moscow" }).format(new Date(value));
}

function formatMoney(value: number) {
  return new Intl.NumberFormat("ru-RU", {
    maximumFractionDigits: 0,
    style: "currency",
    currency: "RUB",
  }).format(value);
}
