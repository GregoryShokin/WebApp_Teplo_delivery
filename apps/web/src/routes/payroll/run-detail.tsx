import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowLeft, CheckCircle2, ExternalLink, RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";

import { Button } from "../../components/ui/button";
import { PageHeader } from "../../components/ui-app/PageHeader";
import { StatusBadge } from "../../components/ui-app/StatusBadge";
import {
  finalizePayrollRun,
  getEmployees,
  getPayrollRun,
  getPayrollRunLines,
  type Employee,
  type PayrollLine,
} from "../../lib/api";
import { formatDate, formatDateTime, formatMoney } from "./runs";

type PayrollRunDetailRouteProps = {
  runId: string;
  onNavigate: (path: string) => void;
};

export function PayrollRunDetailRoute({ runId, onNavigate }: PayrollRunDetailRouteProps) {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
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

  const employeesById = useMemo(() => {
    const map = new Map<string, Employee>();
    for (const employee of employeesQuery.data ?? []) {
      map.set(employee.id, employee);
    }
    return map;
  }, [employeesQuery.data]);

  const finalizeMutation = useMutation({
    mutationFn: () => finalizePayrollRun(runId),
    onSuccess: async () => {
      setError(null);
      await queryClient.invalidateQueries({ queryKey: ["payroll-run", runId] });
      await queryClient.invalidateQueries({ queryKey: ["payroll-runs"] });
    },
    onError: (mutationError) => setError((mutationError as Error).message),
  });

  const run = runQuery.data;
  const period = run?.period;
  const canFinalize = run?.status === "completed" && (run.blocking_issues ?? []).length === 0;

  function finalize() {
    if (
      !window.confirm("Финализировать расчет? После закрытия повторный расчет будет заблокирован.")
    ) {
      return;
    }
    finalizeMutation.mutate();
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="Детали расчета"
        description={
          period
            ? `${formatDate(period.start_date)} - ${formatDate(period.end_date)} · выплата ${formatDate(period.payroll_date)}`
            : runId
        }
        action={
          <>
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
              <CheckCircle2 size={16} aria-hidden="true" />
              Финализировать
            </Button>
          </>
        }
      />

      {run ? (
        <div className="mt-5 grid gap-3 md:grid-cols-4">
          <Metric label="Статус" value={run.status} status={run.status} />
          <Metric label="К выплате" value={formatMoney(Number(run.summary.total_payable ?? 0))} />
          <Metric label="Фонд" value={formatMoney(Number(run.summary.fund_accrual ?? 0))} />
          <Metric label="Запущен" value={formatDateTime(run.started_at)} />
        </div>
      ) : null}

      {error ? (
        <div className="mt-4 rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      ) : null}

      {run && run.blocking_issues.length > 0 ? (
        <section className="mt-5 rounded-lg border border-destructive/30 bg-white">
          <div className="flex items-center gap-2 border-b border-border px-4 py-3 text-sm font-semibold">
            <AlertTriangle size={16} className="text-destructive" aria-hidden="true" />
            Блокеры
          </div>
          <div className="divide-y divide-border">
            {run.blocking_issues.map((issue, index) => (
              <BlockingIssue issue={issue} key={index} onNavigate={onNavigate} />
            ))}
          </div>
        </section>
      ) : null}

      <section className="mt-5 overflow-hidden rounded-lg border border-border bg-white">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[980px] border-collapse text-sm">
            <thead className="bg-muted/70 text-left text-xs uppercase text-muted-foreground">
              <tr>
                <th className="px-4 py-3 font-semibold">Сотрудник</th>
                <th className="px-3 py-3 font-semibold">Роль</th>
                <th className="px-3 py-3 font-semibold">Оклад</th>
                <th className="px-3 py-3 font-semibold">Процент</th>
                <th className="px-3 py-3 font-semibold">Фонд</th>
                <th className="px-3 py-3 font-semibold">Удержания</th>
                <th className="px-3 py-3 font-semibold">К выплате</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {(linesQuery.data ?? []).map((line) => (
                <PayrollLineRow
                  employee={employeesById.get(line.employee_id)}
                  key={line.id}
                  line={line}
                />
              ))}
            </tbody>
          </table>
        </div>
        {linesQuery.isLoading || runQuery.isLoading ? (
          <div className="px-4 py-10 text-center text-sm text-muted-foreground">Загрузка</div>
        ) : null}
        {!linesQuery.isLoading && (linesQuery.data ?? []).length === 0 ? (
          <div className="px-4 py-10 text-center text-sm text-muted-foreground">
            Строк расчета нет
          </div>
        ) : null}
      </section>
    </div>
  );
}

function Metric({ label, value, status }: { label: string; value: string; status?: string }) {
  return (
    <div className="rounded-lg border border-border bg-white px-4 py-3">
      <div className="text-xs font-medium uppercase text-muted-foreground">{label}</div>
      <div className="mt-2 text-lg font-semibold">
        {status ? <StatusBadge status={status} /> : value}
      </div>
    </div>
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
  const employeeName = String(issue.employee_name ?? issue.employee_iiko_id ?? "");
  const href = type === "needs_setup" ? "/staff" : null;
  return (
    <div className="grid gap-3 px-4 py-3 md:grid-cols-[1fr_auto] md:items-center">
      <div>
        <div className="text-sm font-medium">{issueTitle(type)}</div>
        <pre className="mt-2 max-h-32 overflow-auto rounded-md bg-muted px-3 py-2 text-xs leading-5">
          {JSON.stringify(issue, null, 2)}
        </pre>
        {employeeName ? (
          <div className="mt-2 text-sm text-muted-foreground">{employeeName}</div>
        ) : null}
      </div>
      {href ? (
        <Button onClick={() => onNavigate(href)} title="Исправить" variant="outline">
          <ExternalLink size={16} aria-hidden="true" />
          Исправить
        </Button>
      ) : null}
    </div>
  );
}

function PayrollLineRow({ line, employee }: { line: PayrollLine; employee?: Employee }) {
  return (
    <tr className="align-middle hover:bg-muted/40">
      <td className="px-4 py-3 font-medium">{employee?.full_name ?? line.employee_id}</td>
      <td className="px-3 py-3">{line.role}</td>
      <td className="px-3 py-3">{formatMoney(line.base_pay)}</td>
      <td className="px-3 py-3">{formatMoney(line.percent_pay)}</td>
      <td className="px-3 py-3">{formatMoney(line.fund_accrual)}</td>
      <td className="px-3 py-3">{formatMoney(line.deduction)}</td>
      <td className="px-3 py-3 font-semibold">{formatMoney(line.total_payable)}</td>
    </tr>
  );
}

function issueTitle(type: string) {
  const labels: Record<string, string> = {
    needs_setup: "Сотрудник требует настройки",
    attendance_quality_review: "Явка требует проверки",
    post_termination_attendance: "Явка после увольнения",
    missing_attendance: "Нет явок за период",
  };
  return labels[type] ?? type;
}
