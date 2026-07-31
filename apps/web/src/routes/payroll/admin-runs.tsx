import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye, LoaderCircle, Play, RefreshCw } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import { EmptyState } from "@/components/ui-app/EmptyState";
import { StatusBadge } from "@/components/ui-app/StatusBadge";
import {
  apiErrorMessage,
  createAdminPayrollRun,
  getAdminPayrollRunLines,
  getAdminPayrollRuns,
  type PayrollLine,
  type PayrollRun,
} from "@/lib/api";
import { usePermissions } from "@/lib/permissions";

import { formatDate, formatMoney, formatPeriodRange } from "./runs";

type PayrollAdminRunsTabProps = {
  onNavigate: (path: string) => void;
};

export function PayrollAdminRunsTab({ onNavigate }: PayrollAdminRunsTabProps) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const canStartRuns = permissions.canPerformAction("payroll.runs.admin.start");
  const canRecalculateRuns = permissions.canPerformAction("payroll.runs.admin.recalculate");
  const [recalculatingRunIds, setRecalculatingRunIds] = useState<string[]>([]);

  const runsQuery = useQuery({
    queryKey: ["payroll-admin-runs"],
    queryFn: getAdminPayrollRuns,
  });

  const runs = [...(runsQuery.data ?? [])].sort(compareRunsDesc);
  const lineQueries = useQueries({
    queries: runs.map((run) => ({
      queryKey: ["payroll-admin-run-lines", run.id],
      queryFn: () => getAdminPayrollRunLines(run.id),
      enabled: runs.length > 0,
    })),
  });
  const linesByRunId = new Map<string, PayrollLine[]>();
  runs.forEach((run, index) => {
    linesByRunId.set(run.id, lineQueries[index]?.data ?? []);
  });

  const createMutation = useMutation({
    mutationFn: () => createAdminPayrollRun(),
    onSuccess: async (run) => {
      await queryClient.invalidateQueries({ queryKey: ["payroll-admin-runs"] });
      onNavigate(`/payroll/admin/runs/${run.id}`);
    },
  });

  // Пересчёт КОНКРЕТНОГО полумесячного периода — только администрация
  // (`/payroll/admin/runs` → `run_admin_payroll`). Производственный `POST /payroll/runs`
  // здесь не используется: он пересобрал бы строки из явок и затёр оклады.
  const recalculateRunMutation = useMutation({
    mutationFn: (run: PayrollRun) =>
      createAdminPayrollRun({ periodId: run.period_id, forceRefresh: true }),
    onMutate: (run) => {
      setRecalculatingRunIds((ids) => (ids.includes(run.id) ? ids : [...ids, run.id]));
    },
    onSuccess: async (updatedRun, run) => {
      const runIds = new Set([run.id, updatedRun.id]);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["payroll-admin-runs"] }),
        ...[...runIds].flatMap((id) => [
          queryClient.invalidateQueries({ queryKey: ["payroll-admin-run", id] }),
          queryClient.invalidateQueries({ queryKey: ["payroll-admin-run-lines", id] }),
        ]),
      ]);
      toast.success("Ведомость обновлена");
    },
    onError: (error) =>
      toast.error(apiErrorMessage(error, "Не удалось пересчитать, попробуйте ещё раз")),
    onSettled: (_updatedRun, _error, run) => {
      if (!run) {
        return;
      }
      setRecalculatingRunIds((ids) => ids.filter((id) => id !== run.id));
    },
  });

  const tableColumns: Array<DataTableColumn<PayrollRun>> = [
    {
      key: "period",
      header: "Период",
      cell: (run) => (
        <div className="min-w-[180px]">
          <div className="font-medium">{formatPeriodRange(run.period)}</div>
          <div className="text-xs text-muted-foreground">
            {run.period ? `Выплата ${formatDate(run.period.payroll_date)}` : "Период не задан"}
          </div>
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
      header: "Итого к выплате",
      cell: (run) => formatMoney(runTotal(run)),
      className: "font-medium tabular-nums",
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
      cell: (run) => {
        const isRecalculating = recalculatingRunIds.includes(run.id);
        return (
          <div className="flex flex-wrap justify-end gap-2">
            {canRecalculateRuns ? (
              <Button
                disabled={isRecalculating}
                onClick={(event) => {
                  event.stopPropagation();
                  recalculateRunMutation.mutate(run);
                }}
                size="sm"
                title="Пересчитать ЗП администрации за этот период"
                variant="outline"
              >
                {isRecalculating ? (
                  <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                ) : (
                  <RefreshCw size={16} aria-hidden="true" />
                )}
                Пересчитать
              </Button>
            ) : null}
            <Button
              onClick={(event) => {
                event.stopPropagation();
                onNavigate(`/payroll/admin/runs/${run.id}`);
              }}
              size="sm"
              variant="outline"
            >
              <Eye size={16} aria-hidden="true" />
              Открыть
            </Button>
          </div>
        );
      },
    },
  ];

  const createButton = canStartRuns ? (
    <Button onClick={() => createMutation.mutate()} disabled={createMutation.isPending}>
      {createMutation.isPending ? (
        <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
      ) : (
        <Play size={16} aria-hidden="true" />
      )}
      Создать/обновить ведомость
    </Button>
  ) : null;

  return (
    <div className="space-y-5">
      <section className="grid gap-4 rounded-lg border bg-card p-4 md:grid-cols-[1fr_auto] md:items-center">
        <div>
          <div className="font-semibold">Ведомости администрации</div>
          <div className="mt-1 text-sm text-muted-foreground">
            Полумесячные оклады управляющих, менеджеров и системных администраторов.
          </div>
        </div>
        {createButton}
      </section>

      {createMutation.isError ? (
        <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {(createMutation.error as Error).message}
        </div>
      ) : null}

      {(runsQuery.data ?? []).length === 0 && !runsQuery.isLoading ? (
        <EmptyState
          icon={<Play className="h-5 w-5" aria-hidden="true" />}
          title="Ведомостей администрации пока нет"
          description="Создайте первую полумесячную ведомость, чтобы увидеть оклады к выплате."
          action={createButton ?? undefined}
        />
      ) : (
        <DataTable
          columns={tableColumns}
          rows={runs}
          isLoading={runsQuery.isLoading}
          getRowKey={(run) => run.id}
          onRowClick={(run) => onNavigate(`/payroll/admin/runs/${run.id}`)}
          emptyMessage="Ведомостей администрации пока нет"
        />
      )}
    </div>
  );
}

function compareRunsDesc(a: PayrollRun, b: PayrollRun) {
  const left = a.period?.start_date ?? a.started_at;
  const right = b.period?.start_date ?? b.started_at;
  return Date.parse(right) - Date.parse(left);
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
