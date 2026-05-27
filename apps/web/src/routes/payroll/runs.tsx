import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Banknote, Eye, Play, RefreshCw } from "lucide-react";

import { Button } from "../../components/ui/button";
import {
  autoCreateNextPayrollPeriod,
  createPayrollRun,
  getPayrollRuns,
  type PayrollRun,
} from "../../lib/api";

type PayrollRunsRouteProps = {
  onNavigate: (path: string) => void;
};

const statusLabels: Record<string, string> = {
  running: "Считается",
  blocked: "Блокеры",
  completed: "Готов",
  failed: "Ошибка",
  finalized: "Закрыт",
};

export function PayrollRunsRoute({ onNavigate }: PayrollRunsRouteProps) {
  const queryClient = useQueryClient();
  const runsQuery = useQuery({ queryKey: ["payroll-runs"], queryFn: getPayrollRuns });

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

  return (
    <main className="min-h-screen bg-background">
      <div className="mx-auto grid min-h-screen max-w-7xl grid-cols-1 lg:grid-cols-[240px_1fr]">
        <PayrollSidebar active="Зарплата" />

        <section className="px-5 py-5 sm:px-8">
          <header className="flex flex-col gap-4 border-b border-border pb-5 xl:flex-row xl:items-center xl:justify-between">
            <div>
              <h1 className="text-2xl font-semibold tracking-normal">Зарплата</h1>
              <p className="mt-1 text-sm text-muted-foreground">Еженедельные расчеты вторник-понедельник.</p>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button
                onClick={() => void queryClient.invalidateQueries({ queryKey: ["payroll-runs"] })}
                title="Обновить"
                variant="outline"
              >
                <RefreshCw size={16} aria-hidden="true" />
                Обновить
              </Button>
              <Button onClick={() => runMutation.mutate()} disabled={runMutation.isPending}>
                <Play size={16} aria-hidden="true" />
                Запустить за следующую неделю
              </Button>
            </div>
          </header>

          {runMutation.isError ? (
            <div className="mt-4 rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {(runMutation.error as Error).message}
            </div>
          ) : null}

          <section className="mt-5 overflow-hidden rounded-lg border border-border bg-white">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[900px] border-collapse text-sm">
                <thead className="bg-muted/70 text-left text-xs uppercase text-muted-foreground">
                  <tr>
                    <th className="px-4 py-3 font-semibold">Период</th>
                    <th className="px-3 py-3 font-semibold">Дата выплаты</th>
                    <th className="px-3 py-3 font-semibold">Статус</th>
                    <th className="px-3 py-3 font-semibold">К выплате</th>
                    <th className="px-3 py-3 font-semibold">Запущен</th>
                    <th className="w-[80px] px-3 py-3 text-right font-semibold"> </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {(runsQuery.data ?? []).map((run) => (
                    <PayrollRunRow key={run.id} run={run} onNavigate={onNavigate} />
                  ))}
                </tbody>
              </table>
            </div>
            {runsQuery.isLoading ? (
              <div className="px-4 py-10 text-center text-sm text-muted-foreground">Загрузка</div>
            ) : null}
            {!runsQuery.isLoading && (runsQuery.data ?? []).length === 0 ? (
              <div className="px-4 py-10 text-center text-sm text-muted-foreground">Расчетов пока нет</div>
            ) : null}
          </section>
        </section>
      </div>
    </main>
  );
}

function PayrollRunRow({ run, onNavigate }: { run: PayrollRun; onNavigate: (path: string) => void }) {
  const period = run.period;
  return (
    <tr className="align-middle hover:bg-muted/40">
      <td className="px-4 py-3 font-medium">
        {period ? `${formatDate(period.start_date)} - ${formatDate(period.end_date)}` : run.period_id}
      </td>
      <td className="px-3 py-3">{period ? formatDate(period.payroll_date) : "-"}</td>
      <td className="px-3 py-3">
        <span className={`inline-flex h-7 items-center rounded-md px-2 text-xs font-medium ${statusClass(run.status)}`}>
          {statusLabels[run.status] ?? run.status}
        </span>
      </td>
      <td className="px-3 py-3">{formatMoney(Number(run.summary.total_payable ?? 0))}</td>
      <td className="px-3 py-3 text-muted-foreground">{formatDateTime(run.started_at)}</td>
      <td className="px-3 py-3 text-right">
        <Button
          onClick={() => onNavigate(`/payroll/runs/${run.id}`)}
          size="icon"
          title="Открыть"
          variant="outline"
        >
          <Eye size={16} aria-hidden="true" />
        </Button>
      </td>
    </tr>
  );
}

export function PayrollSidebar({ active }: { active: string }) {
  return (
    <aside className="border-b border-border bg-white px-5 py-5 lg:border-b-0 lg:border-r">
      <div className="flex items-center gap-3">
        <div className="flex h-9 w-9 items-center justify-center rounded-md bg-primary text-primary-foreground">
          <Banknote size={18} aria-hidden="true" />
        </div>
        <div>
          <div className="text-base font-semibold">Тепло</div>
          <div className="text-xs text-muted-foreground">payroll</div>
        </div>
      </div>
      <nav className="mt-8 grid gap-1 text-sm">
        {[
          ["Обзор", "/"],
          ["Штат", "/staff"],
          ["График", "/"],
          ["Зарплата", "/payroll/runs"],
          ["ДДС", "/"],
          ["ОПиУ", "/"],
          ["Интеграции", "/"],
        ].map(([item, href]) => (
          <a
            className={`rounded-md px-3 py-2 text-left ${
              item === active
                ? "bg-muted font-medium text-foreground"
                : "text-muted-foreground hover:bg-muted hover:text-foreground"
            }`}
            href={href}
            key={item}
          >
            {item}
          </a>
        ))}
      </nav>
    </aside>
  );
}

export function statusClass(status: string) {
  if (status === "blocked" || status === "failed") {
    return "bg-destructive/10 text-destructive";
  }
  if (status === "finalized") {
    return "bg-primary/10 text-primary";
  }
  if (status === "completed") {
    return "bg-accent/20 text-accent-foreground";
  }
  return "bg-muted text-muted-foreground";
}

export function formatDate(value: string) {
  return new Intl.DateTimeFormat("ru-RU", { timeZone: "Europe/Moscow" }).format(new Date(value));
}

export function formatDateTime(value: string) {
  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "short",
    timeStyle: "short",
    timeZone: "Europe/Moscow",
  }).format(new Date(value));
}

export function formatMoney(value: number) {
  return new Intl.NumberFormat("ru-RU", {
    maximumFractionDigits: 0,
    style: "currency",
    currency: "RUB",
  }).format(value);
}
