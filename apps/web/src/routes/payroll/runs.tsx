import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye, Play, RefreshCw } from "lucide-react";

import { Button } from "../../components/ui/button";
import { PageHeader } from "../../components/ui-app/PageHeader";
import { StatusBadge } from "../../components/ui-app/StatusBadge";
import {
  autoCreateNextPayrollPeriod,
  createPayrollRun,
  getPayrollRuns,
  type PayrollRun,
} from "../../lib/api";

type PayrollRunsRouteProps = {
  onNavigate: (path: string) => void;
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
    <div className="space-y-5">
      <PageHeader
        title="Зарплата"
        description="Еженедельные расчеты вторник-понедельник."
        action={
          <>
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
          </>
        }
      />

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
          <div className="px-4 py-10 text-center text-sm text-muted-foreground">
            Расчетов пока нет
          </div>
        ) : null}
      </section>
    </div>
  );
}

function PayrollRunRow({
  run,
  onNavigate,
}: {
  run: PayrollRun;
  onNavigate: (path: string) => void;
}) {
  const period = run.period;
  return (
    <tr className="align-middle hover:bg-muted/40">
      <td className="px-4 py-3 font-medium">
        {period
          ? `${formatDate(period.start_date)} - ${formatDate(period.end_date)}`
          : run.period_id}
      </td>
      <td className="px-3 py-3">{period ? formatDate(period.payroll_date) : "-"}</td>
      <td className="px-3 py-3">
        <StatusBadge status={run.status} />
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

// eslint-disable-next-line react-refresh/only-export-components
export function formatDate(value: string) {
  return new Intl.DateTimeFormat("ru-RU", { timeZone: "Europe/Moscow" }).format(new Date(value));
}

// eslint-disable-next-line react-refresh/only-export-components
export function formatDateTime(value: string) {
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
