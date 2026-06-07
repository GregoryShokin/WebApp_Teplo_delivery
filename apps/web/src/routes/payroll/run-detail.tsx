import axios from "axios";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeft,
  ArrowUpDown,
  Banknote,
  ChevronDown,
  ChevronRight,
  CheckCircle2,
  ExternalLink,
  Landmark,
  LoaderCircle,
  RefreshCw,
  Undo2,
  Search,
  Split,
} from "lucide-react";
import { type ReactNode, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
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
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import { EmptyState } from "@/components/ui-app/EmptyState";
import { PageHeader } from "@/components/ui-app/PageHeader";
import { StatusBadge } from "@/components/ui-app/StatusBadge";
import {
  apiErrorMessage,
  applyPayoutDeltas,
  createPayrollRun,
  createPayoutDrafts,
  finalizePayrollRun,
  getEmployees,
  getPayoutDeltas,
  getPayrollRun,
  getPayrollRunLines,
  getSettings,
  markAllPayrollPayments,
  markPayrollPayment,
  patchPayrollLineDepositOverride,
  setPayoutSplit,
  unmarkPayrollPayment,
  unfinalizePayrollRun,
  type AppSetting,
  type Employee,
  type PayrollLine,
  type PayrollPaymentMethod,
  type PayrollPaymentPayload,
  type PayrollPayoutDelta,
} from "@/lib/api";
import { usePermissions } from "@/lib/permissions";
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

type SortKey =
  | "name"
  | "hours"
  | "penalties_total"
  | "deposit_withholding"
  | "deposit_payout"
  | "fund_accrual"
  | "ndfl_deduction"
  | "total";
type SortDirection = "asc" | "desc";

type PayrollLineRowModel = {
  line: PayrollLine;
  employee?: Employee;
  employeeName: string;
  hours: number;
};

const LEGACY_RECALC_MESSAGE = "Это импортированный период — пересчёт затрёт исторические данные";
const PAYMENT_METHOD_OPTIONS: Array<{ value: PayrollPaymentMethod; label: string }> = [
  { value: "business_card", label: "Бизнес-карта" },
  { value: "cash", label: "Наличные" },
  { value: "transfer", label: "Перевод" },
  { value: "other", label: "Другое" },
];

export function PayrollRunDetailRoute({ runId, onNavigate }: PayrollRunDetailRouteProps) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const canRecalculate = permissions.canPerformAction("payroll.runs.recalculate");
  const canFinalizeRuns = permissions.canPerformAction("payroll.runs.finalize");
  const canReopenRuns = permissions.canPerformAction("payroll.runs.reopen");
  const canMarkPaid = permissions.canPerformAction("payroll.runs.mark_paid");
  const canBankDraft = permissions.canPerformAction("payroll.runs.bank_draft");
  const [isRecalculateDialogOpen, setIsRecalculateDialogOpen] = useState(false);
  const [isCreateDraftsDialogOpen, setIsCreateDraftsDialogOpen] = useState(false);
  const [isUnfinalizeDialogOpen, setIsUnfinalizeDialogOpen] = useState(false);
  const [unfinalizeReason, setUnfinalizeReason] = useState("");

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

  const run = runQuery.data;
  const lines = linesQuery.data ?? [];
  const targetRatio = getTargetFotRatio(settingsQuery.data);
  const totalPayable = Number(run?.summary.total_payable ?? 0);
  const totalRevenue = runRevenue(lines);
  const payrollRatio = totalRevenue > 0 ? totalPayable / totalRevenue : null;
  const employeeCount = new Set(lines.map((line) => line.employee_id)).size;
  const totalHours = lines.reduce((sum, line) => sum + lineHours(line), 0);
  const blockers = run?.blocking_issues ?? [];
  const isLegacyRun = Boolean(run?.is_imported_legacy);
  const isFinal = run ? isFinalStatus(run.status) : false;
  const canFinalize =
    Boolean(run) &&
    run?.status === "completed" &&
    blockers.length === 0 &&
    !isFinal &&
    canFinalizeRuns;
  const canUnfinalize = Boolean(run) && isFinal && canReopenRuns && !isLegacyRun;
  const canManagePayments =
    Boolean(run) && run?.status === "finalized" && canMarkPaid && !isLegacyRun;
  const canManageBankDraft =
    Boolean(run) && run?.status === "finalized" && canBankDraft && !isLegacyRun;
  const canSubmitUnfinalize = unfinalizeReason.trim().length > 0;

  const payoutDeltasQuery = useQuery({
    queryKey: ["payroll-payout-deltas", runId],
    queryFn: () => getPayoutDeltas(runId),
    enabled: canManageBankDraft,
  });

  useEffect(() => {
    if (payoutDeltasQuery.isError) {
      toast.error(apiErrorMessage(payoutDeltasQuery.error, "Не удалось загрузить дельты выплат"));
    }
  }, [payoutDeltasQuery.error, payoutDeltasQuery.isError]);

  const finalizeMutation = useMutation({
    mutationFn: () => finalizePayrollRun(runId),
    onSuccess: async () => {
      toast.success("Расчёт финализирован");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["payroll-run", runId] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-runs"] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-payout-deltas", runId] }),
      ]);
    },
    onError: (mutationError) => toast.error((mutationError as Error).message),
  });

  const unfinalizeMutation = useMutation({
    mutationFn: (reason: string) => unfinalizePayrollRun(runId, reason),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["payroll-run", runId] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-runs"] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-payout-deltas", runId] }),
      ]);
      setIsUnfinalizeDialogOpen(false);
      setUnfinalizeReason("");
      toast.success("Ведомость возвращена в работу");
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось вернуть ведомость в работу"));
    },
  });

  const recalculateMutation = useMutation({
    mutationFn: (periodId: string) => createPayrollRun(periodId),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["payroll-run", runId] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-run-lines", runId] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-runs"] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-payout-deltas", runId] }),
      ]);
      setIsRecalculateDialogOpen(false);
      toast.success("Расчёт обновлён");
    },
    onError: (error) => {
      setIsRecalculateDialogOpen(false);
      toast.error(payrollRecalculateErrorMessage(error));
    },
  });

  const markAllPaymentsMutation = useMutation({
    mutationFn: (payload: PayrollPaymentPayload) => markAllPayrollPayments(runId, payload),
    onSuccess: async (response) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["payroll-run", runId] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-run-lines", runId] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-payout-deltas", runId] }),
      ]);
      toast.success(
        response.marked_count > 0
          ? `Отмечено выплат: ${response.marked_count}`
          : "Все выплаты уже отмечены",
      );
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось отметить выплаты"));
    },
  });

  const createDraftsMutation = useMutation({
    mutationFn: () => createPayoutDrafts(runId),
    onSuccess: async (response) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["payroll-run", runId] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-run-lines", runId] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-payout-deltas", runId] }),
      ]);
      setIsCreateDraftsDialogOpen(false);
      toast.success(`Создано черновиков: ${response.drafts_count}`);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось сформировать черновики"));
    },
  });

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

  return (
    <div className="space-y-5">
      <PageHeader
        title={run ? formatPeriodRange(run.period) : "Расчёт ЗП"}
        titleAccessory={
          isLegacyRun ? (
            <Badge className="rounded-md border-sky-200 bg-sky-50 text-sm text-sky-800 shadow-none">
              Импортирован из выгрузки
            </Badge>
          ) : null
        }
        description={run ? runMeta(run) : "Загрузка расчёта"}
        action={
          <>
            {run ? <StatusBadge status={run.status} /> : null}
            <Button onClick={() => onNavigate("/payroll")} title="Назад" variant="outline">
              <ArrowLeft size={16} aria-hidden="true" />
              Назад
            </Button>
            {canRecalculate && isLegacyRun ? (
              <Button
                aria-disabled="true"
                className="cursor-not-allowed opacity-50"
                onClick={() => window.alert(LEGACY_RECALC_MESSAGE)}
                title={LEGACY_RECALC_MESSAGE}
                variant="outline"
              >
                <RefreshCw size={16} aria-hidden="true" />
                Пересчитать
              </Button>
            ) : canRecalculate ? (
              <AlertDialog
                open={isRecalculateDialogOpen}
                onOpenChange={(open) => {
                  if (!recalculateMutation.isPending) {
                    setIsRecalculateDialogOpen(open);
                  }
                }}
              >
                <AlertDialogTrigger asChild>
                  <Button
                    disabled={!run || recalculateMutation.isPending}
                    title="Пересчитать"
                    variant="outline"
                  >
                    {recalculateMutation.isPending ? (
                      <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                    ) : (
                      <RefreshCw size={16} aria-hidden="true" />
                    )}
                    Пересчитать
                  </Button>
                </AlertDialogTrigger>
                <AlertDialogContent>
                  <AlertDialogHeader>
                    <AlertDialogTitle>
                      Пересчитать расчёт за {run ? formatPeriodRange(run.period) : "период"}?
                    </AlertDialogTitle>
                    <AlertDialogDescription>
                      Текущие линии расчёта будут пересозданы, ручные корректировки в журнале смен
                      НЕ потеряются.
                    </AlertDialogDescription>
                  </AlertDialogHeader>
                  <AlertDialogFooter>
                    <AlertDialogCancel
                      disabled={recalculateMutation.isPending}
                      onClick={() => setIsRecalculateDialogOpen(false)}
                      type="button"
                    >
                      Отмена
                    </AlertDialogCancel>
                    <AlertDialogAction
                      disabled={!run || recalculateMutation.isPending}
                      onClick={() => {
                        if (run) {
                          recalculateMutation.mutate(run.period_id);
                        }
                      }}
                      type="button"
                    >
                      {recalculateMutation.isPending ? (
                        <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                      ) : (
                        <RefreshCw size={16} aria-hidden="true" />
                      )}
                      Пересчитать
                    </AlertDialogAction>
                  </AlertDialogFooter>
                </AlertDialogContent>
              </AlertDialog>
            ) : null}
            {canFinalizeRuns ? (
              <Button onClick={finalize} disabled={!canFinalize || finalizeMutation.isPending}>
                {finalizeMutation.isPending ? (
                  <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                ) : (
                  <CheckCircle2 size={16} aria-hidden="true" />
                )}
                Финализировать
              </Button>
            ) : null}
            {canManageBankDraft ? (
              <AlertDialog
                open={isCreateDraftsDialogOpen}
                onOpenChange={(open) => {
                  if (!createDraftsMutation.isPending) {
                    setIsCreateDraftsDialogOpen(open);
                  }
                }}
              >
                <AlertDialogTrigger asChild>
                  <Button disabled={createDraftsMutation.isPending} title="Сформировать черновики">
                    {createDraftsMutation.isPending ? (
                      <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                    ) : (
                      <Landmark size={16} aria-hidden="true" />
                    )}
                    Сформировать черновики в банке
                  </Button>
                </AlertDialogTrigger>
                <AlertDialogContent>
                  <AlertDialogHeader>
                    <AlertDialogTitle>Сформировать черновики в банке?</AlertDialogTitle>
                    <AlertDialogDescription>
                      Будут созданы mock-черновики T-Bank по суммам с РС. Реальные деньги не
                      списываются.
                    </AlertDialogDescription>
                  </AlertDialogHeader>
                  <AlertDialogFooter>
                    <AlertDialogCancel disabled={createDraftsMutation.isPending} type="button">
                      Отмена
                    </AlertDialogCancel>
                    <AlertDialogAction
                      disabled={createDraftsMutation.isPending}
                      onClick={async (event) => {
                        event.preventDefault();
                        try {
                          await createDraftsMutation.mutateAsync();
                        } catch {
                          // Toast is handled by the mutation's onError.
                        }
                      }}
                      type="button"
                    >
                      {createDraftsMutation.isPending ? (
                        <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                      ) : (
                        <Landmark size={16} aria-hidden="true" />
                      )}
                      Сформировать
                    </AlertDialogAction>
                  </AlertDialogFooter>
                </AlertDialogContent>
              </AlertDialog>
            ) : null}
            {canManagePayments ? (
              <PaymentMarkDialog
                confirmLabel="Отметить все"
                description="Дата и способ будут применены ко всем неоплаченным сотрудникам ведомости."
                isPending={markAllPaymentsMutation.isPending}
                onSubmit={async (payload) => {
                  await markAllPaymentsMutation.mutateAsync(payload);
                }}
                title="Отметить все выплаты?"
                trigger={
                  <Button disabled={markAllPaymentsMutation.isPending} title="Отметить все выплаченными">
                    {markAllPaymentsMutation.isPending ? (
                      <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                    ) : (
                      <CheckCircle2 size={16} aria-hidden="true" />
                    )}
                    Отметить все выплаченными
                  </Button>
                }
              />
            ) : null}
            {canUnfinalize ? (
              <AlertDialog
                open={isUnfinalizeDialogOpen}
                onOpenChange={(open) => {
                  if (!unfinalizeMutation.isPending) {
                    setIsUnfinalizeDialogOpen(open);
                    if (!open) {
                      setUnfinalizeReason("");
                    }
                  }
                }}
              >
                <AlertDialogTrigger asChild>
                  <Button disabled={unfinalizeMutation.isPending} title="Вернуть в работу">
                    {unfinalizeMutation.isPending ? (
                      <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                    ) : (
                      <Undo2 size={16} aria-hidden="true" />
                    )}
                    Вернуть в работу
                  </Button>
                </AlertDialogTrigger>
                <AlertDialogContent>
                  <AlertDialogHeader>
                    <AlertDialogTitle>Вернуть ведомость в работу?</AlertDialogTitle>
                    <AlertDialogDescription>
                      Откат разблокирует ведомость для пересчёта. Укажите причину.
                    </AlertDialogDescription>
                  </AlertDialogHeader>
                  <div className="space-y-2">
                    <Label htmlFor="payroll-unfinalize-reason">Причина</Label>
                    <Textarea
                      disabled={unfinalizeMutation.isPending}
                      id="payroll-unfinalize-reason"
                      onChange={(event) => setUnfinalizeReason(event.target.value)}
                      placeholder="Например: забыли премию за смену"
                      value={unfinalizeReason}
                    />
                  </div>
                  <AlertDialogFooter>
                    <AlertDialogCancel disabled={unfinalizeMutation.isPending} type="button">
                      Отмена
                    </AlertDialogCancel>
                    <AlertDialogAction
                      disabled={!canSubmitUnfinalize || unfinalizeMutation.isPending}
                      onClick={(event) => {
                        event.preventDefault();
                        if (canSubmitUnfinalize) {
                          unfinalizeMutation.mutate(unfinalizeReason.trim());
                        }
                      }}
                      type="button"
                    >
                      {unfinalizeMutation.isPending ? (
                        <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                      ) : (
                        <Undo2 size={16} aria-hidden="true" />
                      )}
                      Вернуть в работу
                    </AlertDialogAction>
                  </AlertDialogFooter>
                </AlertDialogContent>
              </AlertDialog>
            ) : null}
          </>
        }
      />

      {blockers.length > 0 ? (
        <section className="rounded-lg border border-amber-200 bg-amber-50 p-4 text-amber-900">
          <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0" aria-hidden="true" />
            <div className="min-w-0 flex-1">
              <div className="font-semibold">
                Невозможно финализировать: {blockers.length} {pluralizeIssue(blockers.length)} в
                расчёте
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
        <KpiCard
          title="Часов отработано"
          value={formatHours(totalHours)}
          description="По iiko-явкам"
        />
        <KpiCard title="ФОТ итого" value={formatMoney(totalPayable)} description="К выплате" />
        <KpiCard
          title="% от выручки"
          value={formatRatio(payrollRatio)}
          description={`Порог ${formatRatio(targetRatio)}`}
          tone={payrollRatio !== null && payrollRatio > targetRatio ? "warning" : "default"}
        />
      </section>

      {runQuery.isError ? (
        <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {(runQuery.error as Error).message}
        </div>
      ) : null}

      <PayoutDeltasPanel
        canManageBankDraft={canManageBankDraft}
        deltas={payoutDeltasQuery.data ?? []}
        employeesById={employeesById}
        runId={runId}
      />

      <PayrollByEmployeeTab
        canManageBankDraft={canManageBankDraft}
        canManagePayments={canManagePayments}
        employeesById={employeesById}
        isLoading={linesQuery.isLoading || runQuery.isLoading}
        lines={lines}
        runStatus={run?.status ?? ""}
      />
    </div>
  );
}

function PayrollByEmployeeTab({
  canManageBankDraft,
  canManagePayments,
  employeesById,
  isLoading,
  lines,
  runStatus,
}: {
  canManageBankDraft: boolean;
  canManagePayments: boolean;
  employeesById: Map<string, Employee>;
  isLoading: boolean;
  lines: PayrollLine[];
  runStatus: string;
}) {
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("name");
  const [sortDirection, setSortDirection] = useState<SortDirection>("asc");
  const [selectedLineId, setSelectedLineId] = useState<string | null>(null);

  const rows = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const prepared = lines.map((line) => {
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
        return row.employeeName.toLowerCase().includes(needle);
      })
      .sort((left, right) => compareRows(left, right, sortKey, sortDirection));
  }, [employeesById, lines, search, sortDirection, sortKey]);

  const selectedLine = rows.find((row) => row.line.id === selectedLineId) ?? null;

  function setSort(nextKey: SortKey) {
    if (nextKey === sortKey) {
      setSortDirection(sortDirection === "asc" ? "desc" : "asc");
      return;
    }
    setSortKey(nextKey);
    setSortDirection("asc");
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
      key: "hours",
      header: (
        <SortButton active={sortKey === "hours"} onClick={() => setSort("hours")}>
          Часов
        </SortButton>
      ),
      cell: (row) => formatHours(row.hours),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "base_pay",
      header: "Оклад",
      cell: (row) => formatMoney(row.line.base_pay),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "premium",
      header: "Премия",
      cell: (row) => formatMoney(row.line.premium),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "percent_pay",
      header: "%",
      cell: (row) => formatMoney(row.line.percent_pay),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "vacation_pay",
      header: "Отпуск",
      cell: (row) => formatMoney(row.line.vacation_pay),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "penalties_total",
      header: (
        <SortButton
          active={sortKey === "penalties_total"}
          onClick={() => setSort("penalties_total")}
        >
          Штрафы
        </SortButton>
      ),
      cell: (row) => {
        const total = linePenaltyTotal(row.line);
        if (total <= 0) {
          return <span className="text-muted-foreground">0 ₽</span>;
        }
        return <span className="text-rose-700">−{formatMoney(total)}</span>;
      },
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "deposit_withholding",
      header: (
        <SortButton
          active={sortKey === "deposit_withholding"}
          onClick={() => setSort("deposit_withholding")}
        >
          Удержание депозита
        </SortButton>
      ),
      cell: (row) => formatMoney(row.line.deposit_withholding),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "deposit_payout",
      header: (
        <SortButton active={sortKey === "deposit_payout"} onClick={() => setSort("deposit_payout")}>
          Выдача депозита
        </SortButton>
      ),
      cell: (row) => formatMoney(row.line.deposit_payout),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "fund_accrual",
      header: (
        <SortButton active={sortKey === "fund_accrual"} onClick={() => setSort("fund_accrual")}>
          Нак. фонд
        </SortButton>
      ),
      cell: (row) => formatMoney(row.line.fund_accrual),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "ndfl_deduction",
      header: (
        <SortButton active={sortKey === "ndfl_deduction"} onClick={() => setSort("ndfl_deduction")}>
          НДФЛ
        </SortButton>
      ),
      cell: (row) => formatMoney(row.line.ndfl_withheld),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "total",
      header: (
        <SortButton active={sortKey === "total"} onClick={() => setSort("total")}>
          К выплате
        </SortButton>
      ),
      cell: (row) => formatMoney(row.line.total_payable),
      className: "text-right font-semibold tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "payout_split",
      header: "Нал / РС",
      cell: (row) => (
        <PayoutSplitCell
          canManageBankDraft={canManageBankDraft}
          employeeName={row.employeeName}
          line={row.line}
        />
      ),
      className: "min-w-[220px]",
    },
    {
      key: "payment",
      header: "Выплата",
      cell: (row) => (
        <PaymentCell
          canManagePayments={canManagePayments}
          employeeName={row.employeeName}
          line={row.line}
        />
      ),
      className: "min-w-[210px]",
    },
  ];

  return (
    <div className="space-y-4">
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

      {lines.length === 0 && !isLoading ? (
        <EmptyState
          icon={<AlertTriangle className="h-5 w-5" aria-hidden="true" />}
          title="Строк расчёта нет"
          description="После успешного запуска здесь появятся сотрудники и суммы к выплате."
        />
      ) : (
        <DataTable
          columns={tableColumns}
          rows={rows}
          isLoading={isLoading}
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
          {selectedLine ? <PayrollLineDrawer row={selectedLine} runStatus={runStatus} /> : null}
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
      className={cn("shadow-none", tone === "warning" ? "border-amber-200 bg-amber-50" : undefined)}
    >
      <CardContent className="p-4">
        <div className="text-sm text-muted-foreground">{title}</div>
        <div className="mt-2 text-2xl font-semibold tabular-nums">{value}</div>
        <div className="mt-2 text-sm text-muted-foreground">{description}</div>
      </CardContent>
    </Card>
  );
}

function PayoutDeltasPanel({
  canManageBankDraft,
  deltas,
  employeesById,
  runId,
}: {
  canManageBankDraft: boolean;
  deltas: PayrollPayoutDelta[];
  employeesById: Map<string, Employee>;
  runId: string;
}) {
  const queryClient = useQueryClient();
  const [isOpen, setIsOpen] = useState(true);
  const [isApplyDialogOpen, setIsApplyDialogOpen] = useState(false);
  const changedDeltas = useMemo(
    () => deltas.filter((delta) => delta.classification !== "unchanged"),
    [deltas],
  );
  const applyMutation = useMutation({
    mutationFn: () => applyPayoutDeltas(runId),
    onSuccess: async (response) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["payroll-run", runId] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-run-lines", runId] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-payout-deltas", runId] }),
      ]);
      setIsApplyDialogOpen(false);
      toast.success(`Применено: ${response.applied_count}`);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось применить дельты"));
    },
  });

  if (changedDeltas.length === 0) {
    return null;
  }

  return (
    <section className="rounded-lg border bg-card">
      <div className="flex flex-col gap-3 p-4 md:flex-row md:items-start md:justify-between">
        <button
          className="flex min-w-0 items-start gap-2 text-left"
          onClick={() => setIsOpen((current) => !current)}
          type="button"
        >
          {isOpen ? (
            <ChevronDown className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          ) : (
            <ChevronRight className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          )}
          <span className="min-w-0">
            <span className="block font-semibold">Дельты выплат</span>
            <span className="mt-1 block text-sm text-muted-foreground">
              Доплаты уйдут отдельными черновиками; излишки фиксируются на бизнес-карте.
            </span>
          </span>
        </button>
        {canManageBankDraft ? (
          <AlertDialog
            open={isApplyDialogOpen}
            onOpenChange={(open) => {
              if (!applyMutation.isPending) {
                setIsApplyDialogOpen(open);
              }
            }}
          >
            <AlertDialogTrigger asChild>
              <Button disabled={applyMutation.isPending} type="button">
                {applyMutation.isPending ? (
                  <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                ) : (
                  <Banknote size={16} aria-hidden="true" />
                )}
                Применить дельты
              </Button>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Применить дельты выплат?</AlertDialogTitle>
                <AlertDialogDescription>
                  Доплаты будут оформлены отдельными mock-черновиками, а излишки останутся
                  зафиксированы на бизнес-карте.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel disabled={applyMutation.isPending} type="button">
                  Отмена
                </AlertDialogCancel>
                <AlertDialogAction
                  disabled={applyMutation.isPending}
                  onClick={async (event) => {
                    event.preventDefault();
                    try {
                      await applyMutation.mutateAsync();
                    } catch {
                      // Toast is handled by the mutation's onError.
                    }
                  }}
                  type="button"
                >
                  {applyMutation.isPending ? (
                    <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                  ) : (
                    <Banknote size={16} aria-hidden="true" />
                  )}
                  Применить
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        ) : null}
      </div>
      {isOpen ? (
        <div className="overflow-x-auto border-t">
          <table className="w-full min-w-[680px] text-sm">
            <thead className="bg-muted/40 text-xs uppercase text-muted-foreground">
              <tr>
                <th className="px-4 py-3 text-left font-semibold">Сотрудник</th>
                <th className="px-4 py-3 text-right font-semibold">Было</th>
                <th className="px-4 py-3 text-right font-semibold">Стало</th>
                <th className="px-4 py-3 text-right font-semibold">Дельта</th>
                <th className="px-4 py-3 text-left font-semibold">Тип</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {changedDeltas.map((delta) => {
                const deltaValue = moneyValue(delta.delta);
                const employeeName =
                  employeesById.get(delta.employee_id)?.full_name ?? delta.employee_id;
                return (
                  <tr key={delta.employee_id}>
                    <td className="px-4 py-3 font-medium">{employeeName}</td>
                    <td className="px-4 py-3 text-right tabular-nums">
                      {formatMoney(moneyValue(delta.previous_amount))}
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums">
                      {formatMoney(moneyValue(delta.new_amount))}
                    </td>
                    <td
                      className={cn(
                        "px-4 py-3 text-right font-medium tabular-nums",
                        deltaValue >= 0 ? "text-emerald-700" : "text-amber-700",
                      )}
                    >
                      {formatSignedMoney(deltaValue)}
                    </td>
                    <td className="px-4 py-3">
                      <PayoutDeltaBadge classification={delta.classification} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}

function PayoutSplitCell({
  canManageBankDraft,
  employeeName,
  line,
}: {
  canManageBankDraft: boolean;
  employeeName: string;
  line: PayrollLine;
}) {
  const overpaidAmount = moneyValue(line.overpaid_amount);
  const canEditSplit = canManageBankDraft && line.payout_status !== "paid";

  return (
    <div className="flex min-w-[200px] flex-col items-start gap-2">
      <div className="grid w-full gap-1 text-xs">
        <div className="flex items-center justify-between gap-3">
          <span className="text-muted-foreground">Нал</span>
          <span className="font-medium tabular-nums">{formatMoney(moneyValue(line.amount_cash))}</span>
        </div>
        <div className="flex items-center justify-between gap-3">
          <span className="text-muted-foreground">РС</span>
          <span className="font-medium tabular-nums">
            {formatMoney(moneyValue(line.amount_account))}
          </span>
        </div>
      </div>
      <div className="flex flex-wrap gap-1">
        <PayoutStatusBadge line={line} />
        {overpaidAmount > 0 ? (
          <InlineTooltip content="остаётся на бизнес-карте">
            <Badge className="rounded-md border-amber-200 bg-amber-50 text-amber-800 shadow-none">
              Излишек {formatMoney(overpaidAmount)}
            </Badge>
          </InlineTooltip>
        ) : null}
      </div>
      {canEditSplit ? <PayoutSplitDialog employeeName={employeeName} line={line} /> : null}
    </div>
  );
}

function PayoutSplitDialog({ employeeName, line }: { employeeName: string; line: PayrollLine }) {
  const queryClient = useQueryClient();
  const total = moneyValue(line.total_payable);
  const [open, setOpen] = useState(false);
  const [cashValue, setCashValue] = useState(moneyInputValue(line.amount_cash));
  const cashAmount = parseMoneyInput(cashValue);
  const canSubmit = cashAmount !== null && cashAmount >= 0 && cashAmount <= total;
  const accountAmount = canSubmit && cashAmount !== null ? total - cashAmount : null;
  const inputId = `payout-split-cash-${line.id}`;

  useEffect(() => {
    if (open) {
      setCashValue(moneyInputValue(line.amount_cash));
    }
  }, [line.amount_cash, line.id, open]);

  const mutation = useMutation({
    mutationFn: (amountCash: number) =>
      setPayoutSplit(line.run_id, line.employee_id, amountCash),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["payroll-run", line.run_id] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-run-lines", line.run_id] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-payout-deltas", line.run_id] }),
      ]);
      setOpen(false);
      toast.success("Нал / РС сохранены");
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось сохранить нал / РС"));
    },
  });

  return (
    <AlertDialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (mutation.isPending) {
          return;
        }
        setOpen(nextOpen);
        if (nextOpen) {
          setCashValue(moneyInputValue(line.amount_cash));
        }
      }}
    >
      <AlertDialogTrigger asChild>
        <Button
          disabled={mutation.isPending}
          onClick={(event) => event.stopPropagation()}
          size="sm"
          type="button"
          variant="outline"
        >
          {mutation.isPending ? (
            <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
          ) : (
            <Split size={15} aria-hidden="true" />
          )}
          Указать нал/РС
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent onClick={(event) => event.stopPropagation()}>
        <AlertDialogHeader>
          <AlertDialogTitle>Нал / РС: {employeeName}</AlertDialogTitle>
          <AlertDialogDescription>
            Укажите наличную часть выплаты. Сумма с РС пересчитается автоматически.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <div className="space-y-3">
          <div className="text-sm text-muted-foreground">
            К выплате: <span className="font-medium text-foreground">{formatMoney(total)}</span>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor={inputId}>Наличными</Label>
              <Input
                disabled={mutation.isPending}
                id={inputId}
                inputMode="decimal"
                max={total}
                min={0}
                onChange={(event) => setCashValue(event.target.value)}
                step="0.01"
                type="number"
                value={cashValue}
              />
            </div>
            <div className="rounded-md border bg-muted/30 p-3">
              <div className="text-xs text-muted-foreground">С РС</div>
              <div className="mt-1 font-semibold tabular-nums">
                {accountAmount === null ? "—" : formatMoney(accountAmount)}
              </div>
            </div>
          </div>
          {!canSubmit ? (
            <div className="text-xs text-destructive">
              Введите сумму наличными от 0 до {formatMoney(total)}.
            </div>
          ) : null}
        </div>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={mutation.isPending} type="button">
            Отмена
          </AlertDialogCancel>
          <AlertDialogAction
            disabled={!canSubmit || mutation.isPending}
            onClick={async (event) => {
              event.preventDefault();
              event.stopPropagation();
              if (!canSubmit || cashAmount === null) {
                return;
              }
              try {
                await mutation.mutateAsync(normalizeMoney(cashAmount));
              } catch {
                // Toast is handled by the mutation's onError.
              }
            }}
            type="button"
          >
            {mutation.isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : (
              <CheckCircle2 size={16} aria-hidden="true" />
            )}
            Сохранить
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

function PayoutStatusBadge({ line }: { line: PayrollLine }) {
  if (line.payout_status === "paid") {
    return (
      <Badge className="rounded-md border-emerald-200 bg-emerald-50 text-emerald-800 shadow-none">
        Выплачено
      </Badge>
    );
  }
  if (line.payout_status === "draft_created" || line.draft_status) {
    return (
      <Badge
        className="rounded-md border-emerald-200 bg-emerald-50 text-emerald-800 shadow-none"
        title={line.draft_status === "updated" ? "Черновик обновлён" : undefined}
      >
        Черновик создан
      </Badge>
    );
  }
  if (line.payout_status === "planned") {
    return (
      <Badge className="rounded-md border-sky-200 bg-sky-50 text-sky-800 shadow-none">
        Запланировано
      </Badge>
    );
  }
  return (
    <Badge className="rounded-md border-border bg-muted text-muted-foreground shadow-none">—</Badge>
  );
}

function PayoutDeltaBadge({
  classification,
}: {
  classification: PayrollPayoutDelta["classification"];
}) {
  if (classification === "topup") {
    return (
      <Badge className="rounded-md border-emerald-200 bg-emerald-50 text-emerald-800 shadow-none">
        Доплата
      </Badge>
    );
  }
  if (classification === "overpay") {
    return (
      <Badge className="rounded-md border-amber-200 bg-amber-50 text-amber-800 shadow-none">
        Излишек
      </Badge>
    );
  }
  return (
    <Badge className="rounded-md border-border bg-muted text-muted-foreground shadow-none">—</Badge>
  );
}

function SortButton({
  active,
  children,
  onClick,
}: {
  active: boolean;
  children: ReactNode;
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

function PaymentCell({
  canManagePayments,
  employeeName,
  line,
}: {
  canManagePayments: boolean;
  employeeName: string;
  line: PayrollLine;
}) {
  const queryClient = useQueryClient();
  const isPaid = line.payment_status === "paid";
  const markMutation = useMutation({
    mutationFn: (payload: PayrollPaymentPayload) =>
      markPayrollPayment(line.run_id, {
        employee_id: line.employee_id,
        ...payload,
      }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["payroll-run", line.run_id] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-run-lines", line.run_id] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-payout-deltas", line.run_id] }),
      ]);
      toast.success("Выплата отмечена");
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось отметить выплату"));
    },
  });
  const unmarkMutation = useMutation({
    mutationFn: () => unmarkPayrollPayment(line.run_id, line.employee_id),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["payroll-run", line.run_id] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-run-lines", line.run_id] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-payout-deltas", line.run_id] }),
      ]);
      toast.success("Отметка выплаты отменена");
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось отменить отметку"));
    },
  });
  const isPending = markMutation.isPending || unmarkMutation.isPending;

  return (
    <div className="flex min-w-[190px] flex-col items-start gap-2">
      {isPaid ? (
        <Badge className="rounded-md border-emerald-200 bg-emerald-50 text-emerald-800 shadow-none">
          Выплачено {line.paid_at ? formatDate(line.paid_at) : ""}
        </Badge>
      ) : (
        <Badge className="rounded-md border-border bg-muted text-muted-foreground shadow-none">
          Ожидает
        </Badge>
      )}
      {isPaid && line.paid_method ? (
        <span className="text-xs text-muted-foreground">
          {paymentMethodLabel(line.paid_method)}
          {line.paid_amount !== null ? ` · ${formatMoney(line.paid_amount)}` : ""}
        </span>
      ) : null}
      {canManagePayments ? (
        isPaid ? (
          <Button
            disabled={isPending}
            onClick={(event) => {
              event.stopPropagation();
              unmarkMutation.mutate();
            }}
            size="sm"
            type="button"
            variant="outline"
          >
            {unmarkMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
            ) : (
              <Undo2 size={15} aria-hidden="true" />
            )}
            Отменить отметку
          </Button>
        ) : (
          <PaymentMarkDialog
            confirmLabel="Отметить"
            description="Зафиксируется дата, способ и текущая сумма к выплате по сотруднику."
            isPending={markMutation.isPending}
            onSubmit={async (payload) => {
              await markMutation.mutateAsync(payload);
            }}
            title={`Отметить выплату: ${employeeName}`}
            trigger={
              <Button
                disabled={isPending}
                onClick={(event) => event.stopPropagation()}
                size="sm"
                type="button"
                variant="outline"
              >
                {markMutation.isPending ? (
                  <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
                ) : (
                  <CheckCircle2 size={15} aria-hidden="true" />
                )}
                Выплачено
              </Button>
            }
          />
        )
      ) : null}
    </div>
  );
}

function PaymentMarkDialog({
  confirmLabel,
  description,
  isPending,
  onSubmit,
  title,
  trigger,
}: {
  confirmLabel: string;
  description: string;
  isPending: boolean;
  onSubmit: (payload: PayrollPaymentPayload) => Promise<void>;
  title: string;
  trigger: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const [paidAt, setPaidAt] = useState(todayDateInputValue);
  const [method, setMethod] = useState<PayrollPaymentMethod>("business_card");
  const canSubmit = paidAt.trim().length > 0;

  return (
    <AlertDialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (isPending) {
          return;
        }
        setOpen(nextOpen);
        if (nextOpen) {
          setPaidAt(todayDateInputValue());
          setMethod("business_card");
        }
      }}
    >
      <AlertDialogTrigger asChild>{trigger}</AlertDialogTrigger>
      <AlertDialogContent onClick={(event) => event.stopPropagation()}>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription>{description}</AlertDialogDescription>
        </AlertDialogHeader>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-2">
            <Label htmlFor="payroll-payment-paid-at">Дата выплаты</Label>
            <Input
              disabled={isPending}
              id="payroll-payment-paid-at"
              onChange={(event) => setPaidAt(event.target.value)}
              type="date"
              value={paidAt}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="payroll-payment-method">Способ</Label>
            <Select
              disabled={isPending}
              onValueChange={(value) => setMethod(value as PayrollPaymentMethod)}
              value={method}
            >
              <SelectTrigger id="payroll-payment-method">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {PAYMENT_METHOD_OPTIONS.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={isPending} type="button">
            Отмена
          </AlertDialogCancel>
          <AlertDialogAction
            disabled={!canSubmit || isPending}
            onClick={async (event) => {
              event.preventDefault();
              event.stopPropagation();
              if (!canSubmit) {
                return;
              }
              try {
                await onSubmit({ paid_at: paidAt, method });
                setOpen(false);
              } catch {
                // Toast is handled by the mutation's onError.
              }
            }}
            type="button"
          >
            {isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : (
              <CheckCircle2 size={16} aria-hidden="true" />
            )}
            {confirmLabel}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
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

function PayrollLineDrawer({ row, runStatus }: { row: PayrollLineRowModel; runStatus: string }) {
  const days = lineDays(row.line);
  const weekdayPremiumTotal = days.reduce((sum, day) => sum + day.weekdayPremium, 0);
  const adjustments = lineAdjustments(row.line);

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
        <ComponentValue label="Отпускные" value={formatMoney(row.line.vacation_pay)} />
        <ComponentValue label="Всего удержано" value={formatMoney(row.line.deduction)} />
        <ComponentValue label="Фонд" value={formatMoney(row.line.fund_accrual)} />
        <ComponentValue label="К выплате" value={formatMoney(row.line.total_payable)} strong />
      </section>

      <DepositOverrideControl line={row.line} runStatus={runStatus} />

      {weekdayPremiumTotal > 0 ? (
        <section className="rounded-md border bg-muted/30 px-3 py-2 text-sm">
          В окладе: надбавка пт/сб {formatMoney(weekdayPremiumTotal)} за{" "}
          {days.filter((day) => day.weekdayPremium > 0).length} дн.
        </section>
      ) : null}

      {adjustments.bonuses.length > 0 ? (
        <AdjustmentList title="Ручные премии" items={adjustments.bonuses} />
      ) : null}

      {adjustments.penalties.length > 0 ? (
        <AdjustmentList title="Штрафы и удержания" items={adjustments.penalties} />
      ) : null}

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
                <div className="mt-3 grid gap-2 text-sm sm:grid-cols-4">
                  <ComponentValue label="Оклад" value={formatMoney(day.basePay)} dense />
                  <ComponentValue label="%" value={formatMoney(day.percentPay)} dense />
                  <ComponentValue label="Отпуск" value={formatMoney(day.vacationPay)} dense />
                  <ComponentValue label="Фонд" value={formatMoney(day.fundAccrual)} dense />
                </div>
                {day.weekdayPremium > 0 ? (
                  <div className="mt-2 text-sm text-muted-foreground">
                    В т.ч. надбавка пт/сб: {formatMoney(day.weekdayPremium)}
                  </div>
                ) : null}
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

function DepositOverrideControl({ line, runStatus }: { line: PayrollLine; runStatus: string }) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const canEditDeposit = permissions.hasPermission("payroll.production_deposits.edit");
  const [reason, setReason] = useState(line.deposit_exclusion_reason ?? "");
  const disabledReason = "Ведомость зафинализирована, изменения невозможны";
  const isFinal = isFinalStatus(runStatus);

  useEffect(() => {
    setReason(line.deposit_exclusion_reason ?? "");
  }, [line.deposit_exclusion_reason, line.id]);

  const mutation = useMutation({
    mutationFn: (payload: {
      deposit_excluded_for_run: boolean;
      deposit_exclusion_reason?: string | null;
    }) => patchPayrollLineDepositOverride(line.id, payload),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["payroll-run", line.run_id] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-run-lines", line.run_id] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-runs"] }),
      ]);
      toast.success("Настройка депозита сохранена");
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось сохранить настройку депозита"));
    },
  });

  const saveReason = () => {
    mutation.mutate({
      deposit_excluded_for_run: line.deposit_excluded_for_run,
      deposit_exclusion_reason: cleanOptionalText(reason),
    });
  };

  return (
    <section className="space-y-3 rounded-md border bg-background p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm font-semibold">Удержания</div>
          <div className="mt-1 text-sm text-muted-foreground">
            Депозит: {formatMoney(line.deposit_withholding)}
          </div>
        </div>
        <span title={isFinal ? disabledReason : undefined}>
          <Switch
            checked={line.deposit_excluded_for_run}
            disabled={!canEditDeposit || isFinal || mutation.isPending}
            onCheckedChange={(checked) => {
              mutation.mutate({
                deposit_excluded_for_run: checked,
                deposit_exclusion_reason: checked ? cleanOptionalText(reason) : null,
              });
            }}
          />
        </span>
      </div>
      <Label className="flex items-center gap-2 text-sm">
        <span>Исключить депозит из этой ведомости</span>
      </Label>
      <div className="text-xs leading-relaxed text-muted-foreground">
        Скипнуть удержание депозита только для этой ведомости. Изменение применится после пересчёта.
        Настройки сотрудника не затрагиваются.
      </div>
      {line.deposit_excluded_for_run ? (
        <div className="space-y-2">
          <Label htmlFor={`deposit-exclusion-reason-${line.id}`}>Причина</Label>
          <Textarea
            disabled={!canEditDeposit || isFinal || mutation.isPending}
            id={`deposit-exclusion-reason-${line.id}`}
            onChange={(event) => setReason(event.target.value)}
            placeholder="Необязательно"
            value={reason}
          />
          <Button
            disabled={!canEditDeposit || isFinal || mutation.isPending}
            onClick={saveReason}
            size="sm"
            type="button"
            variant="outline"
          >
            {mutation.isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : null}
            Сохранить причину
          </Button>
        </div>
      ) : null}
    </section>
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

function AdjustmentList({ items, title }: { items: AdjustmentComponent[]; title: string }) {
  return (
    <section className="space-y-2">
      <div className="text-sm font-semibold">{title}</div>
      <div className="grid gap-2">
        {items.map((item) => (
          <div
            className="grid gap-2 rounded-md border bg-card p-3 text-sm sm:grid-cols-[90px_1fr_auto] sm:items-center"
            key={item.id}
          >
            <span className="text-muted-foreground">{formatDate(item.workDate)}</span>
            <span className="min-w-0 truncate">{item.category}</span>
            <span className="font-medium tabular-nums">{formatMoney(item.amount)}</span>
            {item.comment ? (
              <span className="text-muted-foreground sm:col-span-3">{item.comment}</span>
            ) : null}
          </div>
        ))}
      </div>
    </section>
  );
}

function cleanOptionalText(value: string) {
  const cleaned = value.trim();
  return cleaned ? cleaned : null;
}

function paymentMethodLabel(method: PayrollPaymentMethod) {
  return PAYMENT_METHOD_OPTIONS.find((option) => option.value === method)?.label ?? "Другое";
}

function InlineTooltip({ children, content }: { children: ReactNode; content: string }) {
  return (
    <span className="group relative inline-flex" title={content}>
      {children}
      <span className="pointer-events-none absolute left-0 top-full z-50 mt-2 hidden w-56 rounded-md border bg-popover px-3 py-2 text-left text-xs leading-5 text-popover-foreground shadow-lg group-hover:block">
        {content}
      </span>
    </span>
  );
}

function moneyValue(value: number | string | null | undefined) {
  const numeric = Number(value ?? 0);
  return Number.isFinite(numeric) ? numeric : 0;
}

function moneyInputValue(value: number | string | null | undefined) {
  return String(moneyValue(value));
}

function parseMoneyInput(value: string) {
  const normalized = value.trim().replace(",", ".");
  if (!normalized) {
    return null;
  }
  const numeric = Number(normalized);
  return Number.isFinite(numeric) ? numeric : null;
}

function normalizeMoney(value: number) {
  return Math.round(value * 100) / 100;
}

function formatSignedMoney(value: number) {
  if (value > 0) {
    return `+${formatMoney(value)}`;
  }
  if (value < 0) {
    return `−${formatMoney(Math.abs(value))}`;
  }
  return formatMoney(0);
}

function todayDateInputValue() {
  const value = new Date();
  value.setMinutes(value.getMinutes() - value.getTimezoneOffset());
  return value.toISOString().slice(0, 10);
}

function runMeta(run: {
  started_at: string;
  finished_at: string | null;
  period: { finalized_at: string | null } | null;
}) {
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
  if (sortKey === "penalties_total") {
    return (linePenaltyTotal(left.line) - linePenaltyTotal(right.line)) * modifier;
  }
  if (sortKey === "deposit_withholding") {
    return (left.line.deposit_withholding - right.line.deposit_withholding) * modifier;
  }
  if (sortKey === "deposit_payout") {
    return (left.line.deposit_payout - right.line.deposit_payout) * modifier;
  }
  if (sortKey === "fund_accrual") {
    return (left.line.fund_accrual - right.line.fund_accrual) * modifier;
  }
  if (sortKey === "ndfl_deduction") {
    return (left.line.ndfl_withheld - right.line.ndfl_withheld) * modifier;
  }
  if (sortKey === "total") {
    return (left.line.total_payable - right.line.total_payable) * modifier;
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
  basePayShift: number;
  seniorityAllowancePay: number;
  weekdayPremium: number;
  percentPay: number;
  vacationPay: number;
  fundAccrual: number;
  dailyRevenue: number;
};

type AdjustmentComponent = {
  id: string;
  workDate: string;
  category: string;
  amount: number;
  comment: string | null;
};

function lineDays(line: PayrollLine): DayComponent[] {
  const days = Array.isArray(line.components.days) ? line.components.days : [];
  return days.filter(isRecord).map((day) => ({
    date: String(day.date ?? ""),
    role: String(day.role ?? line.role),
    category: String(day.category ?? ""),
    hours: Number(day.hours ?? 0),
    basePay: Number(day.base_pay ?? 0),
    basePayShift: Number(day.base_pay_shift ?? day.base_pay ?? 0),
    seniorityAllowancePay: Number(day.seniority_allowance_pay ?? 0),
    weekdayPremium: Number(day.weekday_premium ?? 0),
    percentPay: Number(day.percent_pay ?? 0),
    vacationPay: Number(day.vacation_pay ?? 0),
    fundAccrual: Number(day.fund_accrual ?? 0),
    dailyRevenue: Number(day.daily_revenue ?? 0),
  }));
}

function lineAdjustments(line: PayrollLine) {
  const adjustments = isRecord(line.components.adjustments) ? line.components.adjustments : {};
  return {
    bonuses: adjustmentItems(adjustments.bonuses),
    penalties: adjustmentItems(adjustments.penalties),
  };
}

function linePenaltyTotal(line: PayrollLine) {
  return lineAdjustments(line).penalties.reduce((sum, item) => sum + Math.max(item.amount, 0), 0);
}

function adjustmentItems(value: unknown): AdjustmentComponent[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter(isRecord).map((item) => ({
    id: String(item.id ?? ""),
    workDate: String(item.work_date ?? ""),
    category: String(item.category ?? "Корректировка"),
    amount: Number(item.amount ?? 0),
    comment: typeof item.comment === "string" && item.comment ? item.comment : null,
  }));
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

function payrollRecalculateErrorMessage(error: unknown) {
  if (axios.isAxiosError(error)) {
    if (error.response?.status === 409) {
      return apiErrorMessage(error, "Payroll run is finalized");
    }
    if (!error.response) {
      return "Не удалось пересчитать, попробуйте ещё раз";
    }
  }
  return apiErrorMessage(error, "Не удалось пересчитать, попробуйте ещё раз");
}
