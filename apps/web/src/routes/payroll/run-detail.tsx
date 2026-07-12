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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { navigateTo } from "@/router";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
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
  applyRunPayoutDelta,
  bulkMarkPayrollPayments,
  cancelScheduledDepositPayout,
  createPayrollRun,
  createRunBankDraft,
  finalizePayrollRun,
  getCashWallets,
  getEmployees,
  getPayrollRun,
  getPayrollRunLines,
  getRunBankDraft,
  getRunPayoutAllocation,
  getRunPayoutDelta,
  getSettings,
  markPartialPayrollPayment,
  patchPayrollLineDepositOverride,
  setRunPayoutCash,
  unmarkPayrollPayment,
  unfinalizePayrollRun,
  type AppSetting,
  type CashWallet,
  type Employee,
  type PayrollBankDraft,
  type PayrollLine,
  type PayrollPaymentMethod,
  type RunPayoutDelta,
} from "@/lib/api";
import { usePermissions } from "@/lib/permissions";
import { PAYROLL_ROLE_LABELS } from "@/lib/i18n/employee";
import { roleColorClasses } from "@/lib/role-colors";
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
  | "advance_issued"
  | "fund_accrual"
  | "ndfl_deduction"
  | "total";
type SortDirection = "asc" | "desc";

type PayrollLineRowModel = {
  // Для двуролевого повара `line` — синтетическая объединённая строка (суммы сложены,
  // per-employee поля взяты один раз). `roles` — производственные роли для чипов.
  line: PayrollLine;
  employee?: Employee;
  employeeName: string;
  hours: number;
  roles: string[];
  // id всех физических роль-строк группы (deposit_excluded — поле per-role, исключение
  // депозита из дровера нужно применять КО ВСЕМ строкам сотрудника, не только к первой).
  sourceLineIds: string[];
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
  const canEditDeposits = permissions.hasPermission("payroll.production_deposits.edit");
  const canFinalizeRuns = permissions.canPerformAction("payroll.runs.finalize");
  const canReopenRuns = permissions.canPerformAction("payroll.runs.reopen");
  const canMarkPaid = permissions.canPerformAction("payroll.runs.mark_paid");
  const canBankDraft = permissions.canPerformAction("payroll.runs.bank_draft");
  // Права на каналы выплаты ЗП (Сейф / ТК Черникова / банк-черновик).
  const payoutChannelPerms = {
    safe: permissions.hasPermission("finance.payout_channel.safe"),
    cash_tk: permissions.hasPermission("finance.payout_channel.cash_tk"),
    bank_draft: permissions.hasPermission("finance.payout_channel.bank_draft"),
  };
  const [isRecalculateDialogOpen, setIsRecalculateDialogOpen] = useState(false);
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
  // «На руки» = ФОТ (total_payable) + запланированная выдача депозита. В total_payable депозит
  // намеренно не приплюсован (это поле переиспользуется для гашения авансов и базы черновика),
  // поэтому сворачиваем его в итог только для отображения и наличного/банковского сплита —
  // ровно как backend считает grand_total (payroll_payouts._run_payout_grand_total).
  // deposit_payout — поле per-employee (сериализатор дублирует его на каждой роль-строке
  // сотрудника), поэтому берём по одному разу на сотрудника, иначе у двуролевого повара
  // выдача депозита задвоится (бэкенд _run_deposit_payout_total считает её один раз).
  const depositPayoutTotal = Array.from(
    new Map(lines.map((line) => [line.employee_id, moneyValue(line.deposit_payout)])).values(),
  ).reduce((sum, value) => sum + value, 0);
  const grandTotal = normalizeMoney(totalPayable + depositPayoutTotal);
  const totalRevenue = runRevenue(lines);
  const payrollRatio = totalRevenue > 0 ? totalPayable / totalRevenue : null;
  const employeeCount = new Set(lines.map((line) => line.employee_id)).size;
  const totalHours = lines.reduce((sum, line) => sum + lineHours(line), 0);
  const payoutCashTotal = Math.min(moneyValue(run?.payout_cash_total ?? 0), grandTotal);
  const totalAccountAmount = normalizeMoney(Math.max(0, grandTotal - payoutCashTotal));
  const blockers = run?.blocking_issues ?? [];
  const attendanceWarnings = run?.summary.attendance_warnings ?? [];
  const isLegacyRun = Boolean(run?.is_imported_legacy);
  const isFinal = run ? isFinalStatus(run.status) : false;
  // После расчёта появились авансы/займы, не учтённые в итоге (в т.ч. выданные задним
  // числом) — финализация заблокирована до пересчёта.
  const needsRecalc = run?.status === "completed" && Boolean(run?.needs_recalc);
  const canFinalize =
    Boolean(run) &&
    run?.status === "completed" &&
    blockers.length === 0 &&
    !needsRecalc &&
    !isFinal &&
    canFinalizeRuns;
  const canUnfinalize = Boolean(run) && isFinal && canReopenRuns && !isLegacyRun;
  const canManagePayments =
    Boolean(run) && run?.status === "finalized" && canMarkPaid && !isLegacyRun;
  const canManageBankDraft =
    Boolean(run) && run?.status === "finalized" && canBankDraft && !isLegacyRun;
  const canSubmitUnfinalize = unfinalizeReason.trim().length > 0;

  const bankDraftQuery = useQuery({
    queryKey: ["run-bank-draft", runId],
    queryFn: () => loadRunBankDraft(runId),
    enabled: canManageBankDraft,
  });

  const runPayoutDeltaQuery = useQuery({
    queryKey: ["run-payout-delta", runId],
    queryFn: () => getRunPayoutDelta(runId),
    enabled: canManageBankDraft && Boolean(bankDraftQuery.data),
  });

  useEffect(() => {
    if (bankDraftQuery.isError) {
      toast.error(apiErrorMessage(bankDraftQuery.error, "Не удалось загрузить черновик выплаты"));
    }
  }, [bankDraftQuery.error, bankDraftQuery.isError]);

  useEffect(() => {
    if (runPayoutDeltaQuery.isError) {
      toast.error(
        apiErrorMessage(runPayoutDeltaQuery.error, "Не удалось загрузить дельту выплаты"),
      );
    }
  }, [runPayoutDeltaQuery.error, runPayoutDeltaQuery.isError]);

  const finalizeMutation = useMutation({
    mutationFn: () => finalizePayrollRun(runId),
    onSuccess: async () => {
      toast.success("Расчёт финализирован");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["payroll-run", runId] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-run-lines", runId] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-runs"] }),
        queryClient.invalidateQueries({ queryKey: ["run-bank-draft", runId] }),
        queryClient.invalidateQueries({ queryKey: ["run-payout-delta", runId] }),
      ]);
    },
    onError: (mutationError) => toast.error((mutationError as Error).message),
  });

  const unfinalizeMutation = useMutation({
    mutationFn: (reason: string) => unfinalizePayrollRun(runId, reason),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["payroll-run", runId] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-run-lines", runId] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-runs"] }),
        queryClient.invalidateQueries({ queryKey: ["run-bank-draft", runId] }),
        queryClient.invalidateQueries({ queryKey: ["run-payout-delta", runId] }),
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
        queryClient.invalidateQueries({ queryKey: ["run-bank-draft", runId] }),
        queryClient.invalidateQueries({ queryKey: ["run-payout-delta", runId] }),
      ]);
      setIsRecalculateDialogOpen(false);
      toast.success("Расчёт обновлён");
    },
    onError: (error) => {
      setIsRecalculateDialogOpen(false);
      toast.error(payrollRecalculateErrorMessage(error));
    },
  });

  // Отмена запланированной выдачи депозита прямо из ведомости (сотрудник передумал
  // увольняться): снимаем pending-план и пересчитываем прогон, чтобы убрать столбец «Выдача».
  const cancelDepositPayoutMutation = useMutation({
    mutationFn: (employeeId: string) => cancelScheduledDepositPayout(employeeId),
    onSuccess: async () => {
      toast.success("Запланированная выдача отменена");
      if (run && run.status === "completed") {
        await recalculateMutation.mutateAsync(run.period_id);
      } else {
        await queryClient.invalidateQueries({ queryKey: ["payroll-run-lines", runId] });
      }
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось отменить выдачу")),
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

      {needsRecalc ? (
        <section className="rounded-lg border border-amber-200 bg-amber-50 p-4 text-amber-900">
          <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0" aria-hidden="true" />
            <div className="min-w-0 flex-1">
              <div className="font-semibold">Ведомость устарела — пересчитайте</div>
              <div className="mt-1 text-sm">
                После расчёта появились авансы или займы (например, проведённые задним числом),
                ещё не учтённые в удержаниях. Нажмите «Пересчитать», чтобы обновить итоги —
                финализация заблокирована до пересчёта.
              </div>
            </div>
          </div>
        </section>
      ) : null}

      {attendanceWarnings.length > 0 ? (
        <section className="rounded-lg border border-yellow-200 bg-yellow-50 p-4 text-yellow-900">
          <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0" aria-hidden="true" />
            <div className="min-w-0 flex-1">
              <div className="font-semibold">Требует проверки (не блокирует финализацию)</div>
              <div className="mt-3 grid gap-2">
                {attendanceWarnings.map((warning, index) => (
                  <div
                    className="rounded-md border border-yellow-200 bg-white/70 px-3 py-2 text-sm"
                    key={`${warning.employee_id}-${warning.work_date}-${index}`}
                  >
                    Смена через полночь: {warning.employee_name}, {warning.work_date}
                  </div>
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
        <KpiCard
          title="К выплате"
          value={formatMoney(grandTotal)}
          description={
            depositPayoutTotal > 0
              ? `ФОТ ${formatMoney(totalPayable)} + депозит ${formatMoney(depositPayoutTotal)}`
              : "ФОТ итого"
          }
        />
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

      {canManageBankDraft ? (
        <RunBankDraftCard
          channelPerms={payoutChannelPerms}
          draft={bankDraftQuery.data ?? null}
          isLoading={bankDraftQuery.isLoading}
          payoutCashTotal={payoutCashTotal}
          runId={runId}
          savedWalletId={run?.payout_cash_wallet_id ?? null}
          totalAccountAmount={totalAccountAmount}
          totalPayable={totalPayable}
          grandTotal={grandTotal}
          depositPayoutTotal={depositPayoutTotal}
        />
      ) : null}

      <PayoutDeltasPanel
        canManageBankDraft={canManageBankDraft}
        delta={runPayoutDeltaQuery.data ?? null}
        isLoading={runPayoutDeltaQuery.isLoading}
        runId={runId}
      />

      <PayrollByEmployeeTab
        canManagePayments={canManagePayments}
        canEditDeposits={canEditDeposits}
        cancelDepositPayoutPending={
          cancelDepositPayoutMutation.isPending || recalculateMutation.isPending
        }
        employeesById={employeesById}
        isLoading={linesQuery.isLoading || runQuery.isLoading}
        lines={lines}
        onCancelDepositPayout={(employeeId) => cancelDepositPayoutMutation.mutate(employeeId)}
        runId={runId}
        runStatus={run?.status ?? ""}
      />
    </div>
  );
}

function PayrollByEmployeeTab({
  canManagePayments,
  canEditDeposits,
  cancelDepositPayoutPending,
  employeesById,
  isLoading,
  lines,
  onCancelDepositPayout,
  runId,
  runStatus,
}: {
  canManagePayments: boolean;
  canEditDeposits: boolean;
  cancelDepositPayoutPending: boolean;
  employeesById: Map<string, Employee>;
  isLoading: boolean;
  lines: PayrollLine[];
  onCancelDepositPayout: (employeeId: string) => void;
  runId: string;
  runStatus: string;
}) {
  const runIsFinal = isFinalStatus(runStatus);
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("name");
  const [sortDirection, setSortDirection] = useState<SortDirection>("asc");
  const [selectedLineId, setSelectedLineId] = useState<string | null>(null);
  const [selectedEmployeeIds, setSelectedEmployeeIds] = useState<Set<string>>(new Set());

  const unpaidEmployeeIds = useMemo(
    () =>
      Array.from(
        new Set(
          lines
            .filter((line) => line.payment_status !== "paid")
            .map((line) => line.employee_id),
        ),
      ),
    [lines],
  );

  // Drop selections that are no longer payable (e.g. after a mark / refetch).
  useEffect(() => {
    setSelectedEmployeeIds((current) => {
      const allowed = new Set(unpaidEmployeeIds);
      const next = new Set([...current].filter((id) => allowed.has(id)));
      return next.size === current.size ? current : next;
    });
  }, [unpaidEmployeeIds]);

  const selectedCount = selectedEmployeeIds.size;
  const allUnpaidSelected =
    unpaidEmployeeIds.length > 0 && unpaidEmployeeIds.every((id) => selectedEmployeeIds.has(id));
  const someUnpaidSelected = selectedCount > 0 && !allUnpaidSelected;

  function toggleEmployee(employeeId: string, checked: boolean) {
    setSelectedEmployeeIds((current) => {
      const next = new Set(current);
      if (checked) {
        next.add(employeeId);
      } else {
        next.delete(employeeId);
      }
      return next;
    });
  }

  function toggleAll(checked: boolean) {
    setSelectedEmployeeIds(checked ? new Set(unpaidEmployeeIds) : new Set());
  }

  const bulkMarkMutation = useMutation({
    mutationFn: (employeeIds: string[]) =>
      bulkMarkPayrollPayments(runId, employeeIds, todayDateInputValue()),
    onSuccess: async (response) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["payroll-run", runId] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-run-lines", runId] }),
        queryClient.invalidateQueries({ queryKey: ["run-bank-draft", runId] }),
        queryClient.invalidateQueries({ queryKey: ["run-payout-delta", runId] }),
      ]);
      setSelectedEmployeeIds(new Set());
      toast.success(
        response.marked_count > 0
          ? `Отмечено выплат: ${response.marked_count}`
          : "Нет сотрудников для отметки",
      );
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось отметить выплаты"));
    },
  });

  const rows = useMemo(() => {
    const needle = search.trim().toLowerCase();
    // Группируем строки по сотруднику: производственные роли (пиццерист+сушист) сливаем в
    // ОДНУ строку, замещающую окладную (кассир→помощник менеджера) держим отдельной.
    // Порядок групп — по первому появлению строки (стабильно к сортировке ниже).
    const groups = new Map<string, PayrollLine[]>();
    const order: string[] = [];
    for (const line of lines) {
      const employee = employeesById.get(line.employee_id);
      const groupKey = lineIsSubstituteOklad(line, employee)
        ? `sub:${line.id}`
        : `prod:${line.employee_id}`;
      const bucket = groups.get(groupKey);
      if (bucket) {
        bucket.push(line);
      } else {
        groups.set(groupKey, [line]);
        order.push(groupKey);
      }
    }

    const prepared = order.map((groupKey) => {
      const groupLines = groups.get(groupKey) ?? [];
      const line = mergeEmployeeLines(groupLines);
      const employee = employeesById.get(line.employee_id);
      const roles = Array.from(new Set(groupLines.map((item) => item.role).filter(Boolean)));
      return {
        line,
        employee,
        employeeName: employee?.full_name ?? "Сотрудник требует настройки",
        hours: lineHours(line),
        roles,
        sourceLineIds: groupLines.map((item) => item.id),
      };
    });

    return prepared
      .filter((row) => {
        if (!needle) {
          return true;
        }
        const roleText = row.roles.map((role) => payrollRoleLabel(role)).join(" ").toLowerCase();
        return row.employeeName.toLowerCase().includes(needle) || roleText.includes(needle);
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
    ...(canManagePayments
      ? [
          {
            key: "select",
            header: (
              <Checkbox
                aria-label="Выбрать всех"
                checked={allUnpaidSelected}
                disabled={unpaidEmployeeIds.length === 0}
                onChange={(event) => toggleAll(event.target.checked)}
                ref={(el) => {
                  if (el) {
                    el.indeterminate = someUnpaidSelected;
                  }
                }}
              />
            ),
            cell: (row: PayrollLineRowModel) =>
              row.line.payment_status === "paid" ? null : (
                <Checkbox
                  aria-label={`Выбрать ${row.employeeName}`}
                  checked={selectedEmployeeIds.has(row.line.employee_id)}
                  onChange={(event) => toggleEmployee(row.line.employee_id, event.target.checked)}
                  onClick={(event) => event.stopPropagation()}
                />
              ),
            className: "w-10",
            headerClassName: "w-10",
            pinned: "left",
            width: 48,
          } satisfies DataTableColumn<PayrollLineRowModel>,
        ]
      : []),
    {
      key: "name",
      header: (
        <SortButton active={sortKey === "name"} onClick={() => setSort("name")}>
          Имя
        </SortButton>
      ),
      cell: (row) => (
        <div>
          <div className="font-medium">{row.employeeName}</div>
          <div className="text-xs text-muted-foreground">
            {row.employee?.position || "Роль из явок"}
          </div>
          <RoleChips roles={row.roles} />
          {row.employee?.requires_position_review ? (
            <button
              className="mt-1 inline-flex items-center gap-1 rounded-md border border-amber-300 bg-amber-50 px-1.5 py-0.5 text-xs text-amber-800 hover:bg-amber-100"
              onClick={(event) => {
                event.stopPropagation();
                navigateTo(`/staff?employee=${row.line.employee_id}`);
              }}
              title="Должность требует проверки — открыть карточку сотрудника"
              type="button"
            >
              <AlertTriangle className="h-3 w-3" aria-hidden="true" />
              должность на проверке
            </button>
          ) : null}
        </div>
      ),
      pinned: "left",
      width: 240,
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
      cell: (row) => {
        const amount = row.line.deposit_payout;
        if (amount <= 0) {
          return formatMoney(amount);
        }
        // Запланированную выдачу можно отменить, пока ведомость не финализирована.
        return (
          <div className="flex flex-col items-end gap-0.5">
            <span>{formatMoney(amount)}</span>
            {!runIsFinal && canEditDeposits ? (
              <button
                className="text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline disabled:opacity-50"
                disabled={cancelDepositPayoutPending}
                onClick={() => onCancelDepositPayout(row.line.employee_id)}
                type="button"
              >
                Отменить выдачу
              </button>
            ) : null}
          </div>
        );
      },
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "advance_issued",
      header: (
        <SortButton
          active={sortKey === "advance_issued"}
          onClick={() => setSort("advance_issued")}
        >
          Авансы/займы
        </SortButton>
      ),
      cell: (row) => {
        const amount = row.line.advance_issued;
        if (amount <= 0) {
          return formatMoney(amount);
        }
        return <span className="text-emerald-700">+{formatMoney(amount)}</span>;
      },
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
      cell: (row) => formatMoney(lineOnHand(row.line)),
      className: "text-right font-semibold tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "payment",
      header: "Выплата",
      cell: (row) => (
        <PaymentCell canManagePayments={canManagePayments} line={row.line} />
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
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="text-sm text-muted-foreground">
            {rows.length} {pluralizeEmployeeLine(rows.length)}
          </div>
          {canManagePayments ? (
            <Button
              disabled={selectedCount === 0 || bulkMarkMutation.isPending}
              onClick={() => bulkMarkMutation.mutate(Array.from(selectedEmployeeIds))}
              type="button"
            >
              {bulkMarkMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <CheckCircle2 size={16} aria-hidden="true" />
              )}
              Выплатить{selectedCount > 0 ? ` (${selectedCount})` : ""}
            </Button>
          ) : null}
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

function RunBankDraftCard({
  channelPerms,
  draft,
  isLoading,
  payoutCashTotal,
  runId,
  savedWalletId,
  totalAccountAmount,
  totalPayable,
  grandTotal,
  depositPayoutTotal,
}: {
  channelPerms: { safe: boolean; cash_tk: boolean; bank_draft: boolean };
  draft: PayrollBankDraft | null;
  isLoading: boolean;
  payoutCashTotal: number;
  runId: string;
  savedWalletId: string | null;
  totalAccountAmount: number;
  totalPayable: number;
  grandTotal: number;
  depositPayoutTotal: number;
}) {
  const queryClient = useQueryClient();
  const [isDialogOpen, setIsDialogOpen] = useState(false);
  const [cashValue, setCashValue] = useState(moneyInputValue(payoutCashTotal));
  const [walletCode, setWalletCode] = useState<string>("");
  // Банк, в котором формируется черновик выплаты (через Сейф). По умолчанию Тинькофф.
  const [bankProvider, setBankProvider] = useState<"tbank" | "sber">("tbank");
  const draftAmount = moneyValue(draft?.amount ?? totalAccountAmount);
  const hasDraft = Boolean(draft);

  const cashWalletsQuery = useQuery({
    queryKey: ["payroll-cash-wallets"],
    queryFn: () => getCashWallets(),
  });
  const cashWallets = useMemo<CashWallet[]>(
    () =>
      // Показываем только счета, выдача с которых разрешена правами на канал.
      (cashWalletsQuery.data ?? []).filter((wallet) => {
        if (wallet.code === "cash_safe") {
          return channelPerms.safe;
        }
        if (wallet.code === "tk_chernikova") {
          return channelPerms.cash_tk;
        }
        return true;
      }),
    [cashWalletsQuery.data, channelPerms.safe, channelPerms.cash_tk],
  );
  const allocationQuery = useQuery({
    queryKey: ["run-payout-allocation", runId],
    queryFn: () => getRunPayoutAllocation(runId),
  });

  useEffect(() => {
    setCashValue(moneyInputValue(payoutCashTotal));
  }, [payoutCashTotal]);

  useEffect(() => {
    if (!cashWallets.length) {
      return;
    }
    const current = savedWalletId
      ? cashWallets.find((wallet) => wallet.id === savedWalletId)
      : undefined;
    setWalletCode((prev) => (prev ? prev : (current?.code ?? "")));
  }, [savedWalletId, cashWallets]);

  const cashAmount = parseMoneyInput(cashValue);
  const cashValid = cashAmount !== null && cashAmount >= 0 && cashAmount <= grandTotal;
  const needsWallet = cashValid && cashAmount !== null && cashAmount > 0;
  const walletValid = !needsWallet || walletCode !== "";
  const previewAccount =
    cashValid && cashAmount !== null
      ? normalizeMoney(Math.max(0, grandTotal - cashAmount))
      : null;
  const currentWalletId = cashWallets.find((wallet) => wallet.code === walletCode)?.id ?? null;
  const cashDirty =
    cashAmount === null ||
    normalizeMoney(cashAmount) !== normalizeMoney(payoutCashTotal) ||
    (needsWallet && currentWalletId !== savedWalletId);

  // Ориентир разнесения ЗП по статьям ДДС (фактически проводится по «Выплатить»).
  const previewBuckets = useMemo(
    () =>
      (allocationQuery.data?.buckets ?? []).map((bucket) => ({
        code: bucket.article_code,
        name: bucket.article_name,
        total: moneyValue(bucket.total),
      })),
    [allocationQuery.data?.buckets],
  );

  const invalidatePayoutQueries = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ["payroll-run", runId] }),
      queryClient.invalidateQueries({ queryKey: ["payroll-run-lines", runId] }),
      queryClient.invalidateQueries({ queryKey: ["run-bank-draft", runId] }),
      queryClient.invalidateQueries({ queryKey: ["run-payout-delta", runId] }),
      queryClient.invalidateQueries({ queryKey: ["run-payout-allocation", runId] }),
    ]);

  const cashMutation = useMutation({
    mutationFn: (amountCash: number) =>
      setRunPayoutCash(runId, amountCash, needsWallet ? walletCode : null),
    onSuccess: async () => {
      await invalidatePayoutQueries();
      toast.success("Наличная сумма сохранена");
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось сохранить наличную сумму"));
    },
  });

  const mutation = useMutation({
    mutationFn: () => createRunBankDraft(runId, bankProvider),
    onSuccess: async (nextDraft) => {
      await invalidatePayoutQueries();
      setIsDialogOpen(false);
      toast.success(
        nextDraft?.status === "updated" || hasDraft ? "Черновик обновлён" : "Черновик сформирован",
      );
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось сформировать черновик выплаты"));
    },
  });
  const buttonLabel = hasDraft ? "Обновить черновик" : "Сформировать черновик";
  const actionVerb = hasDraft ? "Обновит" : "Создаст";

  return (
    <section className="rounded-lg border bg-card p-4 shadow-sm">
      <div className="flex items-center gap-2">
        <Landmark className="h-5 w-5 text-muted-foreground" aria-hidden="true" />
        <h2 className="text-base font-semibold tracking-normal">Черновик выплаты в банк</h2>
      </div>
      <p className="mt-1 text-sm text-muted-foreground">
        Укажите наличную сумму и счёт, с которого выдаются наличные. Безналичный остаток уходит
        одним черновиком на счёт ИП — после оплаты в банке деньги автоматически переводятся в Сейф.
        В ДДС зарплата проводится по статьям по факту «Выплатить»: безналичная часть списывается с
        Сейфа, наличная — с выбранного счёта.
      </p>

      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div className="rounded-md border bg-background p-3">
          <div className="text-xs text-muted-foreground">К выплате</div>
          <div className="mt-1 font-semibold tabular-nums">{formatMoney(grandTotal)}</div>
          {depositPayoutTotal > 0 ? (
            <div className="mt-1 text-xs text-muted-foreground">
              ФОТ {formatMoney(totalPayable)} + депозит {formatMoney(depositPayoutTotal)}
            </div>
          ) : null}
        </div>
        <div className="rounded-md border bg-background p-3">
          <Label className="text-xs text-muted-foreground" htmlFor="run-payout-cash">
            Наличными итого
          </Label>
          <Input
            className="mt-1"
            disabled={cashMutation.isPending}
            id="run-payout-cash"
            inputMode="decimal"
            max={grandTotal}
            min={0}
            onChange={(event) => setCashValue(event.target.value)}
            step="0.01"
            type="number"
            value={cashValue}
          />
        </div>
        <div className="rounded-md border bg-background p-3">
          <Label className="text-xs text-muted-foreground" htmlFor="run-payout-wallet">
            Наличный счёт
          </Label>
          <select
            className="mt-1 h-9 w-full rounded-md border border-input bg-background px-2 text-sm disabled:opacity-50"
            disabled={!needsWallet || cashMutation.isPending}
            id="run-payout-wallet"
            onChange={(event) => setWalletCode(event.target.value)}
            value={walletCode}
          >
            <option value="">— выберите счёт —</option>
            {cashWallets.map((wallet) => (
              <option key={wallet.id} value={wallet.code}>
                {wallet.name}
              </option>
            ))}
          </select>
        </div>
        <div className="rounded-md border bg-muted/30 p-3">
          <div className="text-xs text-muted-foreground">На счёт ИП (черновик)</div>
          <div className="mt-1 font-semibold tabular-nums">
            {previewAccount === null ? "—" : formatMoney(previewAccount)}
          </div>
        </div>
      </div>

      {!cashValid ? (
        <div className="mt-2 text-xs text-destructive">
          Введите наличную сумму от 0 до {formatMoney(grandTotal)}.
        </div>
      ) : null}
      {cashValid && needsWallet && !walletValid ? (
        <div className="mt-2 text-xs text-destructive">
          Выберите наличный счёт (Сейф или Торговая касса Черникова).
        </div>
      ) : null}

      {previewBuckets.length > 0 ? (
        <div className="mt-4 overflow-hidden rounded-md border">
          <div className="border-b bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
            Разнесение ЗП по статьям ДДС (проводится по факту «Выплатить»)
          </div>
          <table className="w-full text-sm">
            <thead className="bg-muted/40 text-xs text-muted-foreground">
              <tr>
                <th className="px-3 py-2 text-left font-medium">Статья ДДС</th>
                <th className="px-3 py-2 text-right font-medium">Сумма</th>
              </tr>
            </thead>
            <tbody>
              {previewBuckets.map((bucket) => (
                <tr key={bucket.code} className="border-t">
                  <td className="px-3 py-2">{bucket.name}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{formatMoney(bucket.total)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button
          disabled={!cashValid || !walletValid || !cashDirty || cashMutation.isPending}
          onClick={async () => {
            if (cashAmount === null) {
              return;
            }
            try {
              await cashMutation.mutateAsync(normalizeMoney(cashAmount));
            } catch {
              // Toast is handled by the mutation's onError.
            }
          }}
          size="sm"
          type="button"
          variant="outline"
        >
          {cashMutation.isPending ? (
            <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
          ) : (
            <CheckCircle2 size={15} aria-hidden="true" />
          )}
          Сохранить наличные
        </Button>

        <select
          aria-label="Банк черновика"
          className="h-9 rounded-md border border-input bg-background px-2 text-sm disabled:opacity-50"
          disabled={
            isLoading || mutation.isPending || cashDirty || !channelPerms.bank_draft
          }
          onChange={(event) => setBankProvider(event.target.value as "tbank" | "sber")}
          title={
            channelPerms.bank_draft ? undefined : "Нет права на формирование банк-черновиков"
          }
          value={bankProvider}
        >
          <option value="tbank">Тинькофф</option>
          <option value="sber">Сбербанк</option>
        </select>

        <AlertDialog
          open={isDialogOpen}
          onOpenChange={(open) => {
            if (!mutation.isPending) {
              setIsDialogOpen(open);
            }
          }}
        >
          <AlertDialogTrigger asChild>
            <Button
              disabled={
                isLoading ||
                mutation.isPending ||
                cashDirty ||
                !cashValid ||
                !walletValid ||
                !channelPerms.bank_draft
              }
              title={
                channelPerms.bank_draft ? undefined : "Нет права на формирование банк-черновиков"
              }
              type="button"
            >
              {mutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <Landmark size={16} aria-hidden="true" />
              )}
              {buttonLabel}
            </Button>
          </AlertDialogTrigger>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>{buttonLabel}?</AlertDialogTitle>
              <AlertDialogDescription>
                {actionVerb} черновик выплаты на {formatMoney(totalAccountAmount)} на счёт ИП.
                Подписать нужно будет в приложении банка.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel disabled={mutation.isPending} type="button">
                Отмена
              </AlertDialogCancel>
              <AlertDialogAction
                disabled={mutation.isPending}
                onClick={async (event) => {
                  event.preventDefault();
                  try {
                    await mutation.mutateAsync();
                  } catch {
                    // Toast is handled by the mutation's onError.
                  }
                }}
                type="button"
              >
                {mutation.isPending ? (
                  <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                ) : (
                  <Landmark size={16} aria-hidden="true" />
                )}
                {hasDraft ? "Обновить" : "Сформировать"}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>

        {cashDirty ? (
          <span className="text-xs text-muted-foreground">
            Сначала сохраните наличную сумму.
          </span>
        ) : null}
      </div>

      <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <BankDraftValue label="Сумма" value={formatMoney(draftAmount)} />
        <BankDraftValue
          label="Статус"
          value={
            isLoading ? (
              <Badge className="rounded-md border-border bg-muted text-muted-foreground shadow-none">
                Загрузка
              </Badge>
            ) : draft ? (
              <BankDraftStatusBadge status={draft.status} />
            ) : (
              <Badge className="rounded-md border-border bg-muted text-muted-foreground shadow-none">
                Не создан
              </Badge>
            )
          }
        />
        <BankDraftValue label="Документ" value={draft?.document_id ?? "—"} />
        <BankDraftValue
          label="Синхронизация"
          value={draft?.synced_at ? formatDateTime(draft.synced_at) : "—"}
        />
      </div>

      {draft?.last_error ? (
        <div className="mt-3 rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {draft.last_error}
        </div>
      ) : null}
    </section>
  );
}

function BankDraftValue({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="rounded-md border bg-background p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-1 break-words font-medium tabular-nums">{value}</div>
    </div>
  );
}

function BankDraftStatusBadge({ status }: { status: PayrollBankDraft["status"] }) {
  const className =
    status === "failed"
      ? "border-rose-200 bg-rose-50 text-rose-800"
      : status === "paid"
        ? "border-emerald-200 bg-emerald-50 text-emerald-800"
        : "border-sky-200 bg-sky-50 text-sky-800";
  return (
    <Badge className={cn("rounded-md shadow-none", className)}>
      {bankDraftStatusLabel(status)}
    </Badge>
  );
}

function PayoutDeltasPanel({
  canManageBankDraft,
  delta,
  isLoading,
  runId,
}: {
  canManageBankDraft: boolean;
  delta: RunPayoutDelta | null;
  isLoading: boolean;
  runId: string;
}) {
  const queryClient = useQueryClient();
  const [isOpen, setIsOpen] = useState(true);
  const [isApplyDialogOpen, setIsApplyDialogOpen] = useState(false);
  const applyMutation = useMutation({
    mutationFn: () => applyRunPayoutDelta(runId),
    onSuccess: async (response) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["payroll-run", runId] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-run-lines", runId] }),
        queryClient.invalidateQueries({ queryKey: ["run-bank-draft", runId] }),
        queryClient.invalidateQueries({ queryKey: ["run-payout-delta", runId] }),
      ]);
      setIsApplyDialogOpen(false);
      toast.success(
        response.applied_count > 0 ? "Дельта применена" : "Дельта не изменилась",
      );
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось применить дельту"));
    },
  });

  if (!canManageBankDraft) {
    return null;
  }

  if (isLoading) {
    return (
      <section className="rounded-lg border bg-card p-4 text-sm text-muted-foreground">
        Загрузка дельты выплаты...
      </section>
    );
  }

  if (!delta || delta.classification === "unchanged") {
    return null;
  }

  const deltaValue = moneyValue(delta.delta);
  const applyDescription =
    delta.classification === "topup"
      ? "Доплата будет оформлена отдельным черновиком в банке."
      : "Излишек будет зафиксирован как остаток на бизнес-карте.";

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
            <span className="block font-semibold">Дельта выплаты</span>
            <span className="mt-1 block text-sm text-muted-foreground">
              {applyDescription}
            </span>
          </span>
        </button>
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
              Применить дельту
            </Button>
          </AlertDialogTrigger>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Применить дельту выплаты?</AlertDialogTitle>
              <AlertDialogDescription>{applyDescription}</AlertDialogDescription>
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
      </div>
      {isOpen ? (
        <div className="overflow-x-auto border-t">
          <table className="w-full min-w-[560px] text-sm">
            <thead className="bg-muted/40 text-xs uppercase text-muted-foreground">
              <tr>
                <th className="px-4 py-3 text-right font-semibold">Было</th>
                <th className="px-4 py-3 text-right font-semibold">Стало</th>
                <th className="px-4 py-3 text-right font-semibold">Дельта</th>
                <th className="px-4 py-3 text-left font-semibold">Тип</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              <tr>
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
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}

function PayoutDeltaBadge({
  classification,
}: {
  classification: RunPayoutDelta["classification"];
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
  line,
}: {
  canManagePayments: boolean;
  line: PayrollLine;
}) {
  const queryClient = useQueryClient();
  const isPaid = line.payment_status === "paid";
  const isPartial = line.payment_status === "partially_paid";
  const accrued = line.total_payable;
  const paid = line.paid_amount ?? 0;
  const remaining = Math.max(0, Math.round((accrued - paid) * 100) / 100);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [amountInput, setAmountInput] = useState("");
  const [comment, setComment] = useState("");

  const invalidate = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ["payroll-run", line.run_id] }),
      queryClient.invalidateQueries({ queryKey: ["payroll-run-lines", line.run_id] }),
      queryClient.invalidateQueries({ queryKey: ["run-bank-draft", line.run_id] }),
      queryClient.invalidateQueries({ queryKey: ["run-payout-delta", line.run_id] }),
    ]);

  const unmarkMutation = useMutation({
    mutationFn: () => unmarkPayrollPayment(line.run_id, line.employee_id),
    onSuccess: async () => {
      await invalidate();
      toast.success("Отметка выплаты отменена");
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось отменить отметку"));
    },
  });

  const partialMutation = useMutation({
    mutationFn: (payload: { amount: number | null; comment: string | null }) =>
      markPartialPayrollPayment(line.run_id, {
        employee_id: line.employee_id,
        amount: payload.amount,
        paid_at: todayDateInputValue(),
        comment: payload.comment,
      }),
    onSuccess: async () => {
      await invalidate();
      setDialogOpen(false);
      setAmountInput("");
      setComment("");
      toast.success("Выплата отмечена");
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось отметить выплату"));
    },
  });

  function openDialog() {
    setAmountInput(remaining > 0 ? String(remaining) : "");
    setComment("");
    setDialogOpen(true);
  }

  function submitPartial() {
    const raw = amountInput.trim().replace(",", ".");
    const parsed = raw === "" ? null : Number(raw);
    if (parsed !== null && (!Number.isFinite(parsed) || parsed <= 0)) {
      toast.error("Введите сумму больше нуля");
      return;
    }
    if (parsed !== null && parsed > remaining + 0.001) {
      toast.error(`Сумма превышает остаток ${formatMoney(remaining)}`);
      return;
    }
    partialMutation.mutate({
      amount: parsed,
      comment: comment.trim() ? comment.trim() : null,
    });
  }

  return (
    <div className="flex min-w-[190px] flex-col items-start gap-2">
      {isPaid ? (
        <Badge className="rounded-md border-emerald-200 bg-emerald-50 text-emerald-800 shadow-none">
          Выплачено {line.paid_at ? formatDate(line.paid_at) : ""}
        </Badge>
      ) : isPartial ? (
        <Badge className="rounded-md border-amber-200 bg-amber-50 text-amber-800 shadow-none">
          Выплачено частично
        </Badge>
      ) : (
        <Badge className="rounded-md border-border bg-muted text-muted-foreground shadow-none">
          Ожидает
        </Badge>
      )}
      {isPartial ? (
        <span className="text-xs text-amber-700">
          {formatMoney(paid)} из {formatMoney(accrued)} · остаток {formatMoney(remaining)}
        </span>
      ) : null}
      {isPaid && line.paid_method ? (
        <span className="text-xs text-muted-foreground">
          {paymentMethodLabel(line.paid_method)}
          {line.paid_amount !== null ? ` · ${formatMoney(line.paid_amount)}` : ""}
        </span>
      ) : null}
      {isPartial && line.payment_comment ? (
        <span className="text-xs text-muted-foreground">{line.payment_comment}</span>
      ) : null}
      {canManagePayments && !isPaid ? (
        <Button
          className={isPartial ? "bg-amber-500 text-white hover:bg-amber-600" : undefined}
          onClick={(event) => {
            event.stopPropagation();
            openDialog();
          }}
          size="sm"
          type="button"
          variant={isPartial ? "default" : "outline"}
        >
          <Banknote size={15} aria-hidden="true" />
          {isPartial ? "Доплатить" : "Выплатить частично"}
        </Button>
      ) : null}
      {canManagePayments && (isPaid || isPartial) ? (
        <Button
          disabled={unmarkMutation.isPending}
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
      ) : null}

      <Dialog onOpenChange={setDialogOpen} open={dialogOpen}>
        <DialogContent onClick={(event) => event.stopPropagation()}>
          <DialogHeader>
            <DialogTitle>{isPartial ? "Доплатить остаток" : "Частичная выплата"}</DialogTitle>
            <DialogDescription>
              Начислено {formatMoney(accrued)} · выплачено {formatMoney(paid)} · остаток{" "}
              {formatMoney(remaining)}.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1.5">
              <Label htmlFor="partial-amount">Сумма к выплате</Label>
              <Input
                autoFocus
                id="partial-amount"
                inputMode="decimal"
                onChange={(event) => setAmountInput(event.target.value)}
                placeholder={String(remaining)}
                value={amountInput}
              />
              <p className="text-xs text-muted-foreground">
                Пусто = выплатить весь остаток {formatMoney(remaining)}.
              </p>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="partial-comment">Причина недоплаты (необязательно)</Label>
              <Textarea
                id="partial-comment"
                onChange={(event) => setComment(event.target.value)}
                placeholder="Нехватка налички, спор, удержание…"
                rows={2}
                value={comment}
              />
            </div>
          </div>
          <DialogFooter>
            <Button onClick={() => setDialogOpen(false)} type="button" variant="outline">
              Отмена
            </Button>
            <Button disabled={partialMutation.isPending} onClick={submitPartial} type="button">
              {partialMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
              ) : null}
              {isPartial ? "Доплатить" : "Выплатить"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
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
          {row.roles.length > 0
            ? row.roles.map((role) => payrollRoleLabel(role)).join(", ")
            : row.line.role || "Роль не задана"}{" "}
          · {formatHours(row.hours)}
        </SheetDescription>
      </SheetHeader>

      <section className="grid gap-3 sm:grid-cols-2">
        <ComponentValue label="Оклад" value={formatMoney(row.line.base_pay)} />
        <ComponentValue label="Премия" value={formatMoney(row.line.premium)} />
        <ComponentValue label="Процент" value={formatMoney(row.line.percent_pay)} />
        <ComponentValue label="Отпускные" value={formatMoney(row.line.vacation_pay)} />
        <ComponentValue label="Всего удержано" value={formatMoney(row.line.deduction)} />
        <ComponentValue label="Фонд" value={formatMoney(row.line.fund_accrual)} />
        {moneyValue(row.line.deposit_payout) > 0 ? (
          <ComponentValue label="Выдача депозита" value={formatMoney(row.line.deposit_payout)} />
        ) : null}
        {moneyValue(row.line.advance_issued) > 0 ? (
          <ComponentValue label="Авансы/займы" value={formatMoney(row.line.advance_issued)} />
        ) : null}
        <ComponentValue label="К выплате" value={formatMoney(lineOnHand(row.line))} strong />
      </section>

      <DepositOverrideControl
        line={row.line}
        lineIds={row.sourceLineIds}
        runStatus={runStatus}
      />

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
                      {payrollRoleLabel(day.role)} · {formatHours(day.hours)}
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

function DepositOverrideControl({
  line,
  lineIds,
  runStatus,
}: {
  line: PayrollLine;
  lineIds: string[];
  runStatus: string;
}) {
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
    // Удержание депозита у двуролевого повара сидит на КАЖДОЙ роль-строке (running-баланс
    // переносится между ролями), а override хранится per-(employee, role). Поэтому исключение
    // из объединённой строки применяем ко ВСЕМ её физическим строкам, иначе гасится только
    // первая роль и повар недополучает.
    mutationFn: async (payload: {
      deposit_excluded_for_run: boolean;
      deposit_exclusion_reason?: string | null;
    }) => {
      const targetIds = lineIds.length > 0 ? lineIds : [line.id];
      await Promise.all(targetIds.map((id) => patchPayrollLineDepositOverride(id, payload)));
    },
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

async function loadRunBankDraft(runId: string) {
  try {
    return await getRunBankDraft(runId);
  } catch (error) {
    if (isNotFoundError(error)) {
      return null;
    }
    throw error;
  }
}

function isNotFoundError(error: unknown) {
  return axios.isAxiosError(error) && error.response?.status === 404;
}

function bankDraftStatusLabel(status: string) {
  const labels: Record<string, string> = {
    created: "Создан",
    updated: "Обновлён",
    paid: "Оплачен",
    failed: "Ошибка",
  };
  return labels[status] ?? status;
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

function payrollRoleLabel(role: string | null | undefined): string {
  if (!role) {
    return "—";
  }
  return PAYROLL_ROLE_LABELS[role as keyof typeof PAYROLL_ROLE_LABELS] ?? role;
}

// Чипы ролей объединённой расчётки: каждая роль — своим цветом (единая палитра ролей).
function RoleChips({ roles }: { roles: string[] }) {
  if (roles.length === 0) {
    return null;
  }
  return (
    <span className="mt-1 flex flex-wrap gap-1">
      {roles.map((role) => {
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

// Замещающая окладная строка (кассир, исполняющий помощника менеджера) НЕ сливается с
// производственной: оклад по чужой должности идёт отдельной строкой (как в персональном
// отчёте). Признак — components.kind == "admin_oklad" и роль ≠ основной должности.
function lineIsSubstituteOklad(line: PayrollLine, employee?: Employee): boolean {
  const kind = isRecord(line.components) ? line.components.kind : undefined;
  return kind === "admin_oklad" && (line.role ?? "") !== (employee?.position ?? "");
}

function mergeComponentArray(
  lines: PayrollLine[],
  picker: (components: Record<string, unknown>) => unknown,
): unknown[] {
  return lines.flatMap((line) => {
    const value = isRecord(line.components) ? picker(line.components) : undefined;
    return Array.isArray(value) ? value : [];
  });
}

// Объединяем производственные строки одного сотрудника (пиццерист+сушист) в ОДНУ.
// per-role суммы складываем; per-employee поля (deposit_payout, amount_cash/account,
// payment_status, on_demand*, paid_*, deposit_excluded_*) берём ОДИН раз из первой строки —
// они идентичны у всех строк сотрудника (см. serialize_payroll_line на бэке). Смены и
// корректировки конкатенируем: аванс/штрафы/премии висят только на первой строке, задвоения
// нет; роль каждого дня хранится в самом дне (для дровера и подсветки).
function mergeEmployeeLines(lines: PayrollLine[]): PayrollLine {
  const [first, ...rest] = lines;
  if (rest.length === 0) {
    return first;
  }
  const sum = (pick: (line: PayrollLine) => number) =>
    lines.reduce((acc, line) => acc + moneyValue(pick(line)), 0);
  const firstAdjustments =
    isRecord(first.components) && isRecord(first.components.adjustments)
      ? first.components.adjustments
      : {};
  return {
    ...first,
    base_pay: sum((line) => line.base_pay),
    premium: sum((line) => line.premium),
    percent_pay: sum((line) => line.percent_pay),
    vacation_pay: sum((line) => line.vacation_pay),
    deduction: sum((line) => line.deduction),
    fund_accrual: sum((line) => line.fund_accrual),
    ndfl_withheld: sum((line) => line.ndfl_withheld),
    ndfl_deduction: sum((line) => line.ndfl_deduction),
    total_payable: sum((line) => line.total_payable),
    deposit_withholding: sum((line) => line.deposit_withholding),
    advance_issued: sum((line) => line.advance_issued),
    components: {
      ...(isRecord(first.components) ? first.components : {}),
      days: mergeComponentArray(lines, (components) => components.days),
      adjustments: {
        ...firstAdjustments,
        bonuses: mergeComponentArray(lines, (components) =>
          isRecord(components.adjustments) ? components.adjustments.bonuses : undefined,
        ),
        penalties: mergeComponentArray(lines, (components) =>
          isRecord(components.adjustments) ? components.adjustments.penalties : undefined,
        ),
      },
    },
  };
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
  if (sortKey === "advance_issued") {
    return (left.line.advance_issued - right.line.advance_issued) * modifier;
  }
  if (sortKey === "fund_accrual") {
    return (left.line.fund_accrual - right.line.fund_accrual) * modifier;
  }
  if (sortKey === "ndfl_deduction") {
    return (left.line.ndfl_withheld - right.line.ndfl_withheld) * modifier;
  }
  if (sortKey === "total") {
    return (lineOnHand(left.line) - lineOnHand(right.line)) * modifier;
  }
  return left.employeeName.localeCompare(right.employeeName, "ru") * modifier;
}

// «На руки» по строке = сумма ведомости (total_payable, включая аванс/заём через ведомость)
// + выдача депозита (хранится отдельно и в total_payable не входит).
function lineOnHand(line: PayrollLine) {
  return moneyValue(line.total_payable) + moneyValue(line.deposit_payout);
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
