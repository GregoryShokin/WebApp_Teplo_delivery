import axios from "axios";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeft,
  Banknote,
  ChevronDown,
  ChevronRight,
  CheckCircle2,
  ExternalLink,
  Landmark,
  LoaderCircle,
  RefreshCw,
  Undo2,
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
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
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
  getEmployees,
  getPayrollRun,
  getPayrollRunLines,
  getRunBankDraft,
  getRunFundingSources,
  getRunPayoutAllocation,
  getRunPayoutDelta,
  getSettings,
  markPartialPayrollPayment,
  patchPayrollLineDepositOverride,
  setRunPayoutCash,
  unmarkPayrollPayment,
  unfinalizePayrollRun,
  type AppSetting,
  type Employee,
  type PayrollBankDraft,
  type PayrollCashWalletCode,
  type PayrollFundingSource,
  type PayrollLine,
  type PayrollPaymentMethod,
  type RunPayoutDelta,
} from "@/lib/api";
import { usePermissions } from "@/lib/permissions";
import { EMPLOYEE_CATEGORY_LABELS, PAYROLL_ROLE_LABELS } from "@/lib/i18n/employee";
import { cn } from "@/lib/utils";
import {
  formatDate,
  formatDateTime,
  formatMoney,
  formatPeriodRange,
  formatRatio,
  runRevenue,
} from "./runs";
import { PayrollPayoutWalletCorrectionButton } from "./payout-wallet-correction-dialog";
import { CashPayoutSourcePicker, type PayrollCashChannelPerms } from "./cash-payout-source-picker";
import { extractPayrollRounding } from "./admin-payslip-utils";

type PayrollRunDetailRouteProps = {
  runId: string;
  onNavigate: (path: string) => void;
};

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
  const [splitOpen, setSplitOpen] = useState(false);

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
  // Прогресс выплаты: paid_amount хранит только зарплатную часть. Выдача депозита проводится
  // отдельной корзиной, но при статусе paid тоже считается выплаченной. Значения берём по одному
  // разу на сотрудника (сериализатор дублирует их на каждой роль-строке двуролевого).
  const paidByEmployee = new Map<string, number>();
  const partialEmployeeIds = new Set<string>();
  for (const line of lines) {
    if (line.payment_status === "paid" || line.payment_status === "partially_paid") {
      paidByEmployee.set(line.employee_id, linePaidOnHand(line));
    }
    if (line.payment_status === "partially_paid") {
      partialEmployeeIds.add(line.employee_id);
    }
  }
  const paidTotal = normalizeMoney(
    Array.from(paidByEmployee.values()).reduce((sum, value) => sum + value, 0),
  );
  const payoutRemaining = normalizeMoney(Math.max(0, grandTotal - paidTotal));
  const underpaidCount = partialEmployeeIds.size;
  const payoutPct = grandTotal > 0 ? Math.min(100, Math.round((paidTotal / grandTotal) * 100)) : 0;
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
        queryClient.invalidateQueries({ queryKey: ["run-funding-sources", runId] }),
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
                После расчёта появились авансы или займы (например, проведённые задним числом), ещё
                не учтённые в удержаниях. Нажмите «Пересчитать», чтобы обновить итоги — финализация
                заблокирована до пересчёта.
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

      {isFinal ? (
        <section className="rounded-lg border bg-card p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm">
              Выплачено <span className="font-semibold tabular-nums">{formatMoney(paidTotal)}</span>{" "}
              <span className="text-muted-foreground">из {formatMoney(grandTotal)}</span>
            </div>
            {payoutRemaining > 0 ? (
              <span className="text-sm font-medium text-amber-700">
                остаток {formatMoney(payoutRemaining)}
                {underpaidCount > 0 ? ` · ${underpaidCount} из ${employeeCount} недополучили` : ""}
              </span>
            ) : (
              <span className="text-sm font-medium text-emerald-700">выплачено полностью</span>
            )}
          </div>
          <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-muted">
            <div
              aria-hidden="true"
              className={cn("h-full", payoutRemaining > 0 ? "bg-amber-400" : "bg-emerald-500")}
              style={{ width: `${payoutPct}%` }}
            />
          </div>
        </section>
      ) : null}

      <section className="space-y-3">
        <PayrollPaymentsTable
          channelPerms={payoutChannelPerms}
          canManagePayments={canManagePayments}
          canEditDeposits={canEditDeposits}
          cancelDepositPayoutPending={
            cancelDepositPayoutMutation.isPending || recalculateMutation.isPending
          }
          employeesById={employeesById}
          isLoading={linesQuery.isLoading || runQuery.isLoading}
          lines={lines}
          onCancelDepositPayout={(employeeId) => cancelDepositPayoutMutation.mutate(employeeId)}
          periodLabel={run?.period ? formatPeriodRange(run.period) : ""}
          runId={runId}
          runStatus={run?.status ?? ""}
        />
      </section>

      {canManageBankDraft ? (
        <>
          <button
            className="flex w-full items-center justify-between gap-2 rounded-lg border bg-card p-3 text-left hover:bg-muted/40"
            onClick={() => setSplitOpen(true)}
            type="button"
          >
            <span className="flex items-center gap-2 text-sm font-medium">
              <Landmark className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
              Разбивка нал/безнал и черновик в банк
            </span>
            <span className="flex items-center gap-2 text-xs text-muted-foreground">
              наличными {formatMoney(payoutCashTotal)} · безнал {formatMoney(totalAccountAmount)} ·{" "}
              {bankDraftQuery.data ? "черновик создан" : "не создан"}
              <ChevronRight className="h-4 w-4" aria-hidden="true" />
            </span>
          </button>
          <PayoutSplitDialog
            channelPerms={payoutChannelPerms}
            draft={bankDraftQuery.data ?? null}
            grandTotal={grandTotal}
            onOpenChange={setSplitOpen}
            open={splitOpen}
            payoutCashTotal={payoutCashTotal}
            runId={runId}
            savedWalletId={run?.payout_cash_wallet_id ?? null}
            totalAccountAmount={totalAccountAmount}
          />
        </>
      ) : null}

      <PayoutDeltasPanel
        canManageBankDraft={canManageBankDraft}
        delta={runPayoutDeltaQuery.data ?? null}
        isLoading={runPayoutDeltaQuery.isLoading}
        runId={runId}
      />
    </div>
  );
}

function PayoutSplitDialog({
  open,
  onOpenChange,
  runId,
  grandTotal,
  totalAccountAmount,
  payoutCashTotal,
  savedWalletId,
  channelPerms,
  draft,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  runId: string;
  grandTotal: number;
  totalAccountAmount: number;
  payoutCashTotal: number;
  savedWalletId: string | null;
  channelPerms: { safe: boolean; cash_tk: boolean; bank_draft: boolean };
  draft: PayrollBankDraft | null;
}) {
  const queryClient = useQueryClient();
  const [cashValue, setCashValue] = useState(moneyInputValue(payoutCashTotal));
  const [walletCode, setWalletCode] = useState<string>("");
  const [bankProvider, setBankProvider] = useState<"tbank" | "sber">("tbank");
  const hasDraft = Boolean(draft);

  const fundingQuery = useQuery({
    queryKey: ["run-funding-sources", runId],
    queryFn: () => getRunFundingSources(runId),
  });
  const cashWallets = useMemo<PayrollFundingSource[]>(
    () =>
      (fundingQuery.data?.cash_sources ?? []).filter((wallet) =>
        wallet.code === "cash_safe"
          ? channelPerms.safe
          : wallet.code === "tk_chernikova"
            ? channelPerms.cash_tk
            : true,
      ),
    [fundingQuery.data?.cash_sources, channelPerms.safe, channelPerms.cash_tk],
  );

  useEffect(() => {
    setCashValue(moneyInputValue(payoutCashTotal));
  }, [payoutCashTotal]);
  useEffect(() => {
    if (!cashWallets.length) return;
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
    cashValid && cashAmount !== null ? normalizeMoney(Math.max(0, grandTotal - cashAmount)) : null;
  const selectedCashSource = fundingQuery.data?.cash_sources.find(
    (wallet) => wallet.code === walletCode,
  );
  const selectedBankSource = fundingQuery.data?.bank_sources.find(
    (source) => source.provider === bankProvider,
  );
  const cashFundsValid =
    !needsWallet ||
    !fundingQuery.isSuccess ||
    (selectedCashSource !== undefined &&
      cashAmount !== null &&
      cashAmount <= moneyValue(selectedCashSource.available));
  const bankFundsValid =
    previewAccount === null ||
    previewAccount <= 0 ||
    !fundingQuery.isSuccess ||
    (selectedBankSource?.is_configured === true &&
      previewAccount <= moneyValue(selectedBankSource.available));
  const currentWalletId = selectedCashSource?.id ?? null;
  const cashDirty =
    cashAmount === null ||
    normalizeMoney(cashAmount) !== normalizeMoney(payoutCashTotal) ||
    (needsWallet && currentWalletId !== savedWalletId);

  const submitMutation = useMutation({
    mutationFn: async () => {
      if (cashDirty && cashAmount !== null) {
        await setRunPayoutCash(
          runId,
          normalizeMoney(cashAmount),
          needsWallet ? walletCode : null,
          bankProvider,
        );
      }
      return createRunBankDraft(runId, bankProvider);
    },
    onSuccess: async (nextDraft) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["payroll-run", runId] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-run-lines", runId] }),
        queryClient.invalidateQueries({ queryKey: ["run-bank-draft", runId] }),
        queryClient.invalidateQueries({ queryKey: ["run-payout-delta", runId] }),
        queryClient.invalidateQueries({ queryKey: ["run-payout-allocation", runId] }),
        queryClient.invalidateQueries({ queryKey: ["run-funding-sources", runId] }),
      ]);
      onOpenChange(false);
      toast.success(
        hasDraft || nextDraft?.status === "updated" ? "Черновик обновлён" : "Черновик сформирован",
      );
    },
    onError: (error) =>
      toast.error(apiErrorMessage(error, "Не удалось сформировать черновик выплаты")),
  });

  const canSubmit =
    cashValid &&
    walletValid &&
    cashFundsValid &&
    bankFundsValid &&
    fundingQuery.isSuccess &&
    channelPerms.bank_draft;
  const chipCls = (active: boolean) =>
    cn(
      "rounded-full border px-3 py-1 text-xs",
      active
        ? "border-emerald-300 bg-emerald-50 text-emerald-800"
        : "border-border text-muted-foreground hover:bg-muted",
    );

  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Разбивка выплаты и черновик</DialogTitle>
          <DialogDescription>
            К выплате {formatMoney(grandTotal)}. Наличная часть — с выбранного счёта, остаток уходит
            черновиком на счёт ИП.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="split-cash">Наличными</Label>
            <Input
              className={cn(!cashFundsValid && "border-destructive focus-visible:ring-destructive")}
              id="split-cash"
              inputMode="decimal"
              onChange={(event) => setCashValue(event.target.value)}
              placeholder="0"
              value={cashValue}
            />
            {needsWallet ? (
              <div className="flex flex-wrap gap-2 pt-1">
                <span className="self-center text-xs text-muted-foreground">Откуда:</span>
                {cashWallets.map((wallet) => (
                  <button
                    className={chipCls(walletCode === wallet.code)}
                    key={wallet.id}
                    onClick={() => setWalletCode(wallet.code)}
                    type="button"
                  >
                    {wallet.name} · доступно {formatMoney(moneyValue(wallet.available))}
                  </button>
                ))}
              </div>
            ) : null}
            {!cashValid ? (
              <p className="text-xs text-destructive">
                Введите сумму от 0 до {formatMoney(grandTotal)}.
              </p>
            ) : needsWallet && !walletValid ? (
              <p className="text-xs text-destructive">Выберите наличный счёт.</p>
            ) : !cashFundsValid && selectedCashSource ? (
              <p className="text-xs text-destructive">
                На счёте доступно {formatMoney(moneyValue(selectedCashSource.available))}. Уменьшите
                наличную часть.
              </p>
            ) : null}
          </div>

          <div
            className={cn(
              "flex items-center justify-between rounded-md border bg-muted/30 px-3 py-2 text-sm",
              !bankFundsValid && "border-destructive bg-destructive/5",
            )}
          >
            <span className="text-muted-foreground">Безналичный остаток → черновик на счёт ИП</span>
            <span className="font-medium tabular-nums">
              {previewAccount === null ? "—" : formatMoney(previewAccount)}
            </span>
          </div>

          <div className="space-y-2">
            <Label>Банк для черновика</Label>
            <div className="flex flex-wrap gap-2">
              <button
                className={chipCls(bankProvider === "tbank")}
                onClick={() => setBankProvider("tbank")}
                type="button"
              >
                Тинькофф
              </button>
              <button
                className={chipCls(bankProvider === "sber")}
                onClick={() => setBankProvider("sber")}
                type="button"
              >
                Сбербанк
              </button>
            </div>
            {selectedBankSource ? (
              <p
                className={cn(
                  "text-xs text-muted-foreground",
                  !bankFundsValid && "text-destructive",
                )}
              >
                Доступно на счёте: {formatMoney(moneyValue(selectedBankSource.available))}
                {!selectedBankSource.is_configured ? " · счёт не настроен" : ""}
              </p>
            ) : null}
          </div>
        </div>

        <DialogFooter>
          <Button onClick={() => onOpenChange(false)} type="button" variant="outline">
            Отмена
          </Button>
          <Button
            disabled={!canSubmit || submitMutation.isPending}
            onClick={() => submitMutation.mutate()}
            title={
              channelPerms.bank_draft ? undefined : "Нет права на формирование банк-черновиков"
            }
            type="button"
          >
            {submitMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
            ) : (
              <Landmark size={15} aria-hidden="true" />
            )}
            {hasDraft ? "Обновить черновик" : "Сформировать черновик"} ·{" "}
            {formatMoney(previewAccount ?? totalAccountAmount)}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function PayrollPaymentsTable({
  canManagePayments,
  canEditDeposits,
  cancelDepositPayoutPending,
  channelPerms,
  employeesById,
  isLoading,
  lines,
  onCancelDepositPayout,
  periodLabel,
  runId,
  runStatus,
}: {
  canManagePayments: boolean;
  canEditDeposits: boolean;
  cancelDepositPayoutPending: boolean;
  channelPerms: PayrollCashChannelPerms;
  employeesById: Map<string, Employee>;
  isLoading: boolean;
  lines: PayrollLine[];
  onCancelDepositPayout: (employeeId: string) => void;
  periodLabel: string;
  runId: string;
  runStatus: string;
}) {
  const queryClient = useQueryClient();
  const [statusFilter, setStatusFilter] = useState<"all" | "pending" | "partial" | "paid">("all");
  const [selectedLineId, setSelectedLineId] = useState<string | null>(null);
  const [selectedEmployeeIds, setSelectedEmployeeIds] = useState<Set<string>>(new Set());
  const [bulkDialogOpen, setBulkDialogOpen] = useState(false);
  const [bulkWalletCode, setBulkWalletCode] = useState<PayrollCashWalletCode | null>(null);
  const [bulkCanSubmit, setBulkCanSubmit] = useState(false);

  const rows = useMemo(() => {
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

    return order.map((groupKey) => {
      const groupLines = groups.get(groupKey) ?? [];
      const line = mergeEmployeeLines(groupLines);
      const employee = employeesById.get(line.employee_id);
      return {
        line,
        employee,
        employeeName: employee?.full_name ?? "Сотрудник требует настройки",
        hours: lineHours(line),
        roles: Array.from(new Set(groupLines.map((item) => item.role).filter(Boolean))),
        sourceLineIds: groupLines.map((item) => item.id),
      };
    });
  }, [employeesById, lines]);

  const unpaidEmployeeIds = useMemo(
    () =>
      rows.filter((row) => row.line.payment_status !== "paid").map((row) => row.line.employee_id),
    [rows],
  );

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

  const bulkMarkMutation = useMutation({
    mutationFn: ({
      employeeIds,
      walletCode,
    }: {
      employeeIds: string[];
      walletCode: PayrollCashWalletCode;
    }) => bulkMarkPayrollPayments(runId, employeeIds, todayDateInputValue(), walletCode),
    onSuccess: async (response) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["payroll-run", runId] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-run-lines", runId] }),
        queryClient.invalidateQueries({ queryKey: ["run-bank-draft", runId] }),
        queryClient.invalidateQueries({ queryKey: ["run-payout-delta", runId] }),
      ]);
      setSelectedEmployeeIds(new Set());
      setBulkDialogOpen(false);
      setBulkWalletCode(null);
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

  const filteredRows = rows.filter((row) => {
    if (statusFilter === "all") return true;
    if (statusFilter === "partial") return row.line.payment_status === "partially_paid";
    return row.line.payment_status === statusFilter;
  });

  const totals = rows.reduce(
    (acc, row) => ({
      accrued: acc.accrued + moneyValue(row.line.total_payable),
      payable: acc.payable + lineOnHand(row.line),
      remaining: acc.remaining + lineRemainingOnHand(row.line),
    }),
    { accrued: 0, payable: 0, remaining: 0 },
  );

  const selectedLine = rows.find((row) => row.line.id === selectedLineId) ?? null;
  const selectedAmount = normalizeMoney(
    rows
      .filter((row) => selectedEmployeeIds.has(row.line.employee_id))
      .reduce((sum, row) => sum + lineRemainingOnHand(row.line), 0),
  );
  const statusChips = [
    { key: "all" as const, label: "Все" },
    { key: "pending" as const, label: "Ожидают" },
    { key: "partial" as const, label: "Частично" },
    { key: "paid" as const, label: "Выплачено" },
  ];
  const statusCount = (key: (typeof statusChips)[number]["key"]) =>
    key === "all"
      ? rows.length
      : rows.filter((row) =>
          key === "partial"
            ? row.line.payment_status === "partially_paid"
            : row.line.payment_status === key,
        ).length;

  function toggleEmployee(employeeId: string, checked: boolean) {
    setSelectedEmployeeIds((current) => {
      const next = new Set(current);
      if (checked) next.add(employeeId);
      else next.delete(employeeId);
      return next;
    });
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          {statusChips.map((chip) => (
            <button
              aria-pressed={statusFilter === chip.key}
              className={cn(
                "rounded-full border px-3 py-1 text-xs",
                statusFilter === chip.key
                  ? "border-emerald-300 bg-emerald-50 text-emerald-800"
                  : "border-border text-muted-foreground hover:bg-muted",
              )}
              key={chip.key}
              onClick={() => setStatusFilter(chip.key)}
              type="button"
            >
              {chip.label} · {statusCount(chip.key)}
            </button>
          ))}
        </div>
        {canManagePayments && selectedCount > 0 ? (
          <Button
            disabled={bulkMarkMutation.isPending}
            onClick={() => {
              setBulkWalletCode(null);
              setBulkDialogOpen(true);
            }}
            size="sm"
            type="button"
          >
            {bulkMarkMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
            ) : (
              <CheckCircle2 size={15} aria-hidden="true" />
            )}
            Выплатить полностью · {selectedCount}
          </Button>
        ) : null}
      </div>

      <Dialog onOpenChange={setBulkDialogOpen} open={bulkDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Выплатить выбранным сотрудникам</DialogTitle>
            <DialogDescription>
              {selectedCount} сотрудников · к выплате {formatMoney(selectedAmount)}. Укажите, откуда
              фактически выдаются деньги.
            </DialogDescription>
          </DialogHeader>
          <CashPayoutSourcePicker
            amount={selectedAmount}
            channelPerms={channelPerms}
            onCanSubmitChange={setBulkCanSubmit}
            onChange={setBulkWalletCode}
            runId={runId}
            value={bulkWalletCode}
          />
          <DialogFooter>
            <Button onClick={() => setBulkDialogOpen(false)} type="button" variant="outline">
              Отмена
            </Button>
            <Button
              disabled={!bulkCanSubmit || !bulkWalletCode || bulkMarkMutation.isPending}
              onClick={() => {
                if (!bulkWalletCode) return;
                bulkMarkMutation.mutate({
                  employeeIds: Array.from(selectedEmployeeIds),
                  walletCode: bulkWalletCode,
                });
              }}
              type="button"
            >
              {bulkMarkMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
              ) : null}
              Выплатить {formatMoney(selectedAmount)}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {lines.length === 0 && !isLoading ? (
        <EmptyState
          icon={<AlertTriangle className="h-5 w-5" aria-hidden="true" />}
          title="Строк расчёта нет"
          description="После успешного запуска здесь появятся сотрудники и суммы к выплате."
        />
      ) : (
        <div className="overflow-x-auto rounded-lg border bg-card">
          <table className="w-full min-w-[760px] text-sm">
            <thead>
              <tr className="bg-muted/50 text-xs text-muted-foreground">
                {canManagePayments ? (
                  <th className="w-10 px-3 py-2">
                    <Checkbox
                      aria-label="Выбрать всех"
                      checked={allUnpaidSelected}
                      disabled={unpaidEmployeeIds.length === 0}
                      onChange={(event) =>
                        setSelectedEmployeeIds(
                          event.target.checked ? new Set(unpaidEmployeeIds) : new Set(),
                        )
                      }
                      ref={(element) => {
                        if (element) element.indeterminate = someUnpaidSelected;
                      }}
                    />
                  </th>
                ) : null}
                <th className="px-3 py-2 text-left font-medium">Сотрудник</th>
                <th className="px-3 py-2 text-right font-medium">Начислено</th>
                <th className="px-3 py-2 text-right font-medium">К выплате</th>
                <th className="px-3 py-2 text-right font-medium">Остаток</th>
                <th className="px-3 py-2 text-left font-medium">Статус</th>
              </tr>
            </thead>
            <tbody>
              {isLoading
                ? Array.from({ length: 4 }).map((_, index) => (
                    <tr className="border-t" key={index}>
                      <td
                        className="h-14 animate-pulse bg-muted/30"
                        colSpan={canManagePayments ? 6 : 5}
                      />
                    </tr>
                  ))
                : filteredRows.map((row) => {
                    const isPaid = row.line.payment_status === "paid";
                    const roleLabel =
                      row.roles.length > 0
                        ? row.roles.map((role) => payrollRoleLabel(role)).join(" · ")
                        : row.employee?.position || "Роль не указана";
                    return (
                      <tr
                        className={cn(
                          "cursor-pointer border-t transition-colors hover:bg-muted/40",
                          isPaid && "opacity-70",
                        )}
                        key={row.line.id}
                        onClick={() => setSelectedLineId(row.line.id)}
                      >
                        {canManagePayments ? (
                          <td className="px-3 py-3">
                            {!isPaid ? (
                              <Checkbox
                                aria-label={`Выбрать ${row.employeeName}`}
                                checked={selectedEmployeeIds.has(row.line.employee_id)}
                                onChange={(event) =>
                                  toggleEmployee(row.line.employee_id, event.target.checked)
                                }
                                onClick={(event) => event.stopPropagation()}
                              />
                            ) : null}
                          </td>
                        ) : null}
                        <td className="px-3 py-3">
                          <div className="font-medium">{row.employeeName}</div>
                          <div className="text-xs text-muted-foreground">{roleLabel}</div>
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums">
                          <div>{formatMoney(row.line.total_payable)}</div>
                          {moneyValue(row.line.deposit_payout) > 0 ? (
                            <div className="text-xs text-violet-700">
                              + депозит {formatMoney(row.line.deposit_payout)}
                            </div>
                          ) : null}
                        </td>
                        <td className="px-3 py-3 text-right font-medium tabular-nums">
                          {formatMoney(lineOnHand(row.line))}
                        </td>
                        <td className="px-3 py-3 text-right font-medium tabular-nums">
                          {formatMoney(lineRemainingOnHand(row.line))}
                        </td>
                        <td className="px-3 py-3">
                          <PaymentStatusSummary line={row.line} />
                        </td>
                      </tr>
                    );
                  })}
            </tbody>
            {!isLoading ? (
              <tfoot>
                <tr className="border-t bg-muted/50 tabular-nums">
                  {canManagePayments ? <td className="px-3 py-2" /> : null}
                  <td className="px-3 py-2 text-xs text-muted-foreground">
                    ИТОГО · {rows.length} чел
                  </td>
                  <td className="px-3 py-2 text-right font-medium">
                    {formatMoney(totals.accrued)}
                  </td>
                  <td className="px-3 py-2 text-right font-medium">
                    {formatMoney(totals.payable)}
                  </td>
                  <td className="px-3 py-2 text-right font-medium text-amber-700">
                    {formatMoney(totals.remaining)}
                  </td>
                  <td className="px-3 py-2" />
                </tr>
              </tfoot>
            ) : null}
          </table>
        </div>
      )}

      <Dialog
        open={Boolean(selectedLine)}
        onOpenChange={(open) => {
          if (!open) setSelectedLineId(null);
        }}
      >
        <DialogContent className="max-w-5xl">
          {selectedLine ? (
            <PayrollLineDialogContent
              canEditDeposits={canEditDeposits}
              canManagePayments={canManagePayments}
              cancelDepositPayoutPending={cancelDepositPayoutPending}
              channelPerms={channelPerms}
              onCancelDepositPayout={() => onCancelDepositPayout(selectedLine.line.employee_id)}
              periodLabel={periodLabel}
              row={selectedLine}
              runStatus={runStatus}
            />
          ) : null}
        </DialogContent>
      </Dialog>
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
  embedded = false,
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
  embedded?: boolean;
}) {
  const queryClient = useQueryClient();
  const [isDialogOpen, setIsDialogOpen] = useState(false);
  const [cashValue, setCashValue] = useState(moneyInputValue(payoutCashTotal));
  const [walletCode, setWalletCode] = useState<string>("");
  // Банк, в котором формируется черновик выплаты (через Сейф). По умолчанию Тинькофф.
  const [bankProvider, setBankProvider] = useState<"tbank" | "sber">("tbank");
  const draftAmount = moneyValue(draft?.amount ?? totalAccountAmount);
  const hasDraft = Boolean(draft);

  const fundingQuery = useQuery({
    queryKey: ["run-funding-sources", runId],
    queryFn: () => getRunFundingSources(runId),
  });
  const cashWallets = useMemo<PayrollFundingSource[]>(
    () =>
      // Показываем только счета, выдача с которых разрешена правами на канал.
      (fundingQuery.data?.cash_sources ?? []).filter((wallet) => {
        if (wallet.code === "cash_safe") {
          return channelPerms.safe;
        }
        if (wallet.code === "tk_chernikova") {
          return channelPerms.cash_tk;
        }
        return true;
      }),
    [fundingQuery.data?.cash_sources, channelPerms.safe, channelPerms.cash_tk],
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
    cashValid && cashAmount !== null ? normalizeMoney(Math.max(0, grandTotal - cashAmount)) : null;
  const selectedCashSource = cashWallets.find((wallet) => wallet.code === walletCode);
  const selectedBankSource = fundingQuery.data?.bank_sources.find(
    (source) => source.provider === bankProvider,
  );
  const cashFundsValid =
    !needsWallet ||
    !fundingQuery.isSuccess ||
    (selectedCashSource !== undefined &&
      cashAmount !== null &&
      cashAmount <= moneyValue(selectedCashSource.available));
  const bankFundsValid =
    previewAccount === null ||
    previewAccount <= 0 ||
    !fundingQuery.isSuccess ||
    (selectedBankSource?.is_configured === true &&
      previewAccount <= moneyValue(selectedBankSource.available));
  const currentWalletId = selectedCashSource?.id ?? null;
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
      queryClient.invalidateQueries({ queryKey: ["run-funding-sources", runId] }),
    ]);

  const cashMutation = useMutation({
    mutationFn: (amountCash: number) =>
      setRunPayoutCash(runId, amountCash, needsWallet ? walletCode : null, bankProvider),
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
    <section className={embedded ? undefined : "rounded-lg border bg-card p-4 shadow-sm"}>
      {embedded ? null : (
        <>
          <div className="flex items-center gap-2">
            <Landmark className="h-5 w-5 text-muted-foreground" aria-hidden="true" />
            <h2 className="text-base font-semibold tracking-normal">Черновик выплаты в банк</h2>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            Укажите наличную сумму и счёт, с которого выдаются наличные. Безналичный остаток уходит
            одним черновиком на счёт ИП — после оплаты в банке деньги автоматически переводятся в
            Сейф. В ДДС зарплата проводится по статьям по факту «Выплатить»: безналичная часть
            списывается с Сейфа, наличная — с выбранного счёта.
          </p>
        </>
      )}

      <div className={cn("grid gap-3 sm:grid-cols-2 lg:grid-cols-4", !embedded && "mt-4")}>
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
            className={cn(
              "mt-1",
              !cashFundsValid && "border-destructive focus-visible:ring-destructive",
            )}
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
                {wallet.name} · доступно {formatMoney(moneyValue(wallet.available))}
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
      {cashValid && walletValid && !cashFundsValid && selectedCashSource ? (
        <div className="mt-2 text-xs text-destructive">
          На счёте доступно {formatMoney(moneyValue(selectedCashSource.available))}. Уменьшите
          наличную часть.
        </div>
      ) : null}
      {previewAccount !== null && previewAccount > 0 && selectedBankSource ? (
        <div
          className={cn(
            "mt-2 text-xs text-muted-foreground",
            !bankFundsValid && "text-destructive",
          )}
        >
          В {selectedBankSource.name} доступно{" "}
          {formatMoney(moneyValue(selectedBankSource.available))}
          {!selectedBankSource.is_configured ? " · счёт не настроен" : ""}.
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
          disabled={
            !cashValid ||
            !walletValid ||
            !cashFundsValid ||
            !bankFundsValid ||
            !fundingQuery.isSuccess ||
            !cashDirty ||
            cashMutation.isPending
          }
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

        <PayrollPayoutWalletCorrectionButton
          onCorrected={invalidatePayoutQueries}
          runId={runId}
          wallets={cashWallets}
        />

        <select
          aria-label="Банк черновика"
          className="h-9 rounded-md border border-input bg-background px-2 text-sm disabled:opacity-50"
          disabled={isLoading || mutation.isPending || cashDirty || !channelPerms.bank_draft}
          onChange={(event) => setBankProvider(event.target.value as "tbank" | "sber")}
          title={channelPerms.bank_draft ? undefined : "Нет права на формирование банк-черновиков"}
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
                !cashFundsValid ||
                !bankFundsValid ||
                !fundingQuery.isSuccess ||
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
          <span className="text-xs text-muted-foreground">Сначала сохраните наличную сумму.</span>
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
      toast.success(response.applied_count > 0 ? "Дельта применена" : "Дельта не изменилась");
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
            <span className="mt-1 block text-sm text-muted-foreground">{applyDescription}</span>
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

function PaymentStatusSummary({ line }: { line: PayrollLine }) {
  const accrued = lineOnHand(line);
  const paid = linePaidOnHand(line);
  const remaining = lineRemainingOnHand(line);

  if (line.payment_status === "paid") {
    return (
      <div className="space-y-1">
        <Badge className="rounded-md border-emerald-200 bg-emerald-50 text-emerald-800 shadow-none">
          Выплачено
        </Badge>
        {line.paid_at ? (
          <div className="text-xs text-muted-foreground">{formatDate(line.paid_at)}</div>
        ) : null}
      </div>
    );
  }

  if (line.payment_status === "partially_paid") {
    return (
      <div className="space-y-1">
        <Badge className="rounded-md border-amber-200 bg-amber-50 text-amber-800 shadow-none">
          Частично
        </Badge>
        <div className="text-xs text-amber-700">
          {formatMoney(paid)} из {formatMoney(accrued)} · остаток {formatMoney(remaining)}
        </div>
      </div>
    );
  }

  return (
    <Badge className="rounded-md border-border bg-muted text-muted-foreground shadow-none">
      Ожидает
    </Badge>
  );
}

function PaymentCell({
  canManagePayments,
  channelPerms,
  line,
}: {
  canManagePayments: boolean;
  channelPerms: PayrollCashChannelPerms;
  line: PayrollLine;
}) {
  const queryClient = useQueryClient();
  const isPaid = line.payment_status === "paid";
  const isPartial = line.payment_status === "partially_paid";
  const hasDepositPayout = moneyValue(line.deposit_payout) > 0;
  const accrued = lineOnHand(line);
  const paid = linePaidOnHand(line);
  const remaining = normalizeMoney(Math.max(0, accrued - paid));
  const [dialogOpen, setDialogOpen] = useState(false);
  const [amountInput, setAmountInput] = useState("");
  const [comment, setComment] = useState("");
  const [partialWalletCode, setPartialWalletCode] = useState<PayrollCashWalletCode | null>(null);
  const [partialCanSubmit, setPartialCanSubmit] = useState(false);
  const [fullDialogOpen, setFullDialogOpen] = useState(false);
  const [fullWalletCode, setFullWalletCode] = useState<PayrollCashWalletCode | null>(null);
  const [fullCanSubmit, setFullCanSubmit] = useState(false);

  const invalidate = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ["payroll-run", line.run_id] }),
      queryClient.invalidateQueries({ queryKey: ["payroll-run-lines", line.run_id] }),
      queryClient.invalidateQueries({ queryKey: ["run-bank-draft", line.run_id] }),
      queryClient.invalidateQueries({ queryKey: ["run-payout-delta", line.run_id] }),
      queryClient.invalidateQueries({ queryKey: ["run-funding-sources", line.run_id] }),
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
    mutationFn: (payload: {
      amount: number | null;
      comment: string | null;
      walletCode: PayrollCashWalletCode;
    }) =>
      markPartialPayrollPayment(line.run_id, {
        employee_id: line.employee_id,
        amount: payload.amount,
        paid_at: todayDateInputValue(),
        comment: payload.comment,
        cash_wallet_code: payload.walletCode,
      }),
    onSuccess: async () => {
      await invalidate();
      setDialogOpen(false);
      setAmountInput("");
      setComment("");
      setPartialWalletCode(null);
      toast.success("Выплата отмечена");
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось отметить выплату"));
    },
  });

  const fullMutation = useMutation({
    mutationFn: (walletCode: PayrollCashWalletCode) =>
      bulkMarkPayrollPayments(line.run_id, [line.employee_id], todayDateInputValue(), walletCode),
    onSuccess: async () => {
      await invalidate();
      setFullDialogOpen(false);
      setFullWalletCode(null);
      toast.success("Выплата проведена полностью");
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось провести выплату"));
    },
  });

  function openDialog() {
    setAmountInput(remaining > 0 ? String(remaining) : "");
    setComment("");
    setPartialWalletCode(null);
    setDialogOpen(true);
  }

  function submitPartial() {
    if (!partialWalletCode || !partialCanSubmit) {
      toast.error("Выберите счёт с достаточным остатком");
      return;
    }
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
      walletCode: partialWalletCode,
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
          {line.paid_amount !== null ? ` · ${formatMoney(paid)}` : ""}
        </span>
      ) : null}
      {isPartial && line.payment_comment ? (
        <span className="text-xs text-muted-foreground">{line.payment_comment}</span>
      ) : null}
      {canManagePayments && !isPaid ? (
        <div className="flex flex-wrap gap-2">
          <Button
            disabled={fullMutation.isPending || remaining <= 0}
            onClick={(event) => {
              event.stopPropagation();
              setFullWalletCode(null);
              setFullDialogOpen(true);
            }}
            size="sm"
            type="button"
          >
            {fullMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
            ) : (
              <CheckCircle2 size={15} aria-hidden="true" />
            )}
            {isPartial ? "Доплатить" : "Выплатить"} {formatMoney(remaining)}
          </Button>
          {!hasDepositPayout ? (
            <Button
              onClick={(event) => {
                event.stopPropagation();
                openDialog();
              }}
              size="sm"
              type="button"
              variant="outline"
            >
              <Banknote size={15} aria-hidden="true" />
              Выплатить частично
            </Button>
          ) : null}
        </div>
      ) : null}
      {canManagePayments && isPaid ? (
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
            <CashPayoutSourcePicker
              amount={normalizeMoney(
                amountInput.trim() ? Number(amountInput.trim().replace(",", ".")) || 0 : remaining,
              )}
              channelPerms={channelPerms}
              onCanSubmitChange={setPartialCanSubmit}
              onChange={setPartialWalletCode}
              runId={line.run_id}
              value={partialWalletCode}
            />
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
            <Button
              disabled={!partialCanSubmit || partialMutation.isPending}
              onClick={submitPartial}
              type="button"
            >
              {partialMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
              ) : null}
              {isPartial ? "Доплатить" : "Выплатить"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog onOpenChange={setFullDialogOpen} open={fullDialogOpen}>
        <DialogContent onClick={(event) => event.stopPropagation()}>
          <DialogHeader>
            <DialogTitle>{isPartial ? "Доплатить сотруднику" : "Выплатить сотруднику"}</DialogTitle>
            <DialogDescription>
              К выплате {formatMoney(remaining)}. Выберите счёт фактической выдачи денег.
            </DialogDescription>
          </DialogHeader>
          <CashPayoutSourcePicker
            amount={remaining}
            channelPerms={channelPerms}
            onCanSubmitChange={setFullCanSubmit}
            onChange={setFullWalletCode}
            runId={line.run_id}
            value={fullWalletCode}
          />
          <DialogFooter>
            <Button onClick={() => setFullDialogOpen(false)} type="button" variant="outline">
              Отмена
            </Button>
            <Button
              disabled={!fullCanSubmit || !fullWalletCode || fullMutation.isPending}
              onClick={() => fullWalletCode && fullMutation.mutate(fullWalletCode)}
              type="button"
            >
              {fullMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
              ) : null}
              Выплатить {formatMoney(remaining)}
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

function PayrollLineDialogContent({
  canEditDeposits,
  canManagePayments,
  cancelDepositPayoutPending,
  channelPerms,
  onCancelDepositPayout,
  periodLabel,
  row,
  runStatus,
}: {
  canEditDeposits: boolean;
  canManagePayments: boolean;
  cancelDepositPayoutPending: boolean;
  channelPerms: PayrollCashChannelPerms;
  onCancelDepositPayout: () => void;
  periodLabel: string;
  row: PayrollLineRowModel;
  runStatus: string;
}) {
  const dayComponents = lineDays(row.line);
  const days = lineDailyPayoutRows(row.line);
  const weekdayPremiumTotal = dayComponents.reduce((sum, day) => sum + day.weekdayPremium, 0);
  const adjustments = lineAdjustments(row.line);
  const runIsFinal = isFinalStatus(runStatus);
  const depositPayout = moneyValue(row.line.deposit_payout);
  const depositWithholding = moneyValue(row.line.deposit_withholding);

  return (
    <div className="space-y-5">
      <DialogHeader>
        <DialogTitle className="pr-8">{row.employeeName}</DialogTitle>
        <DialogDescription>
          {row.roles.length > 0
            ? row.roles.map((role) => payrollRoleLabel(role)).join(", ")
            : payrollRoleLabel(row.line.role) || "Роль не задана"}{" "}
          · {periodLabel || "Период ведомости"} · {formatHours(row.hours)}
        </DialogDescription>
      </DialogHeader>

      <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
        <ComponentValue label="Оклад" value={formatMoney(row.line.base_pay)} />
        <ComponentValue label="Процент" value={formatMoney(row.line.percent_pay)} />
        <ComponentValue label="Премии" value={formatMoney(row.line.premium)} />
        {moneyValue(row.line.deduction) > 0 ? (
          <ComponentValue label="Всего удержано" value={formatMoney(row.line.deduction)} />
        ) : null}
        {depositPayout > 0 ? (
          <ComponentValue label="Выдача депозита" value={formatMoney(depositPayout)} />
        ) : null}
        <ComponentValue label="К выплате" value={formatMoney(lineOnHand(row.line))} strong />
      </section>

      {depositWithholding > 0 ? (
        <DepositOverrideControl line={row.line} lineIds={row.sourceLineIds} runStatus={runStatus} />
      ) : null}

      {depositPayout > 0 ? (
        <section className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-violet-200 bg-violet-50/60 p-3">
          <div>
            <div className="text-sm font-semibold text-violet-950">Выдача депозита</div>
            <div className="mt-1 text-sm text-violet-800">
              В эту ведомость добавлено {formatMoney(depositPayout)} поверх зарплатной части.
            </div>
          </div>
          <Button
            disabled={runIsFinal || !canEditDeposits || cancelDepositPayoutPending}
            onClick={onCancelDepositPayout}
            title={runIsFinal ? "После финализации выдачу отменить нельзя" : undefined}
            type="button"
            variant="outline"
          >
            {cancelDepositPayoutPending ? (
              <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
            ) : (
              <Undo2 size={15} aria-hidden="true" />
            )}
            Отменить выдачу
          </Button>
        </section>
      ) : null}

      {weekdayPremiumTotal > 0 ? (
        <section className="rounded-md border bg-muted/30 px-3 py-2 text-sm">
          В окладе: надбавка пт/сб {formatMoney(weekdayPremiumTotal)} за{" "}
          {days.filter((day) => day.weekdayPremium > 0).length} дн.
        </section>
      ) : null}

      <AdjustmentDisclosure
        defaultOpen={adjustments.bonuses.length > 0}
        items={adjustments.bonuses}
        kind="bonus"
        title="Премии"
      />

      <AdjustmentDisclosure
        items={adjustments.penalties}
        kind="deduction"
        title="Штрафы и удержания"
      />

      <section className="space-y-3">
        <div className="text-sm font-semibold">Смены и начисления</div>
        {days.length > 0 ? (
          <div className="overflow-x-auto rounded-lg border">
            <table className="w-full min-w-[940px] text-sm">
              <thead>
                <tr className="bg-muted/50 text-xs text-muted-foreground">
                  <th className="px-3 py-2 text-left font-medium">Дата / роль</th>
                  <th className="px-3 py-2 text-right font-medium">Часы</th>
                  <th className="px-3 py-2 text-right font-medium">Оклад</th>
                  <th className="px-3 py-2 text-right font-medium">Процент</th>
                  <th className="px-3 py-2 text-right font-medium">Премии</th>
                  <th className="px-3 py-2 text-right font-medium">Удержания</th>
                  <th className="px-3 py-2 text-right font-medium">Отпуск</th>
                  <th className="px-3 py-2 text-right font-medium">Фонд</th>
                  <th className="px-3 py-2 text-right font-medium">Итого</th>
                </tr>
              </thead>
              <tbody>
                {days.map((day) => (
                  <tr className="border-t" key={day.date}>
                    <td className="px-3 py-2">
                      <div className="font-medium">{formatDate(day.date)}</div>
                      <div className="text-xs text-muted-foreground">
                        {day.roles.length > 0
                          ? day.roles.map((role) => payrollRoleLabel(role)).join(", ")
                          : "Начисление без смены"}
                        {day.categories.length > 0
                          ? ` · ${day.categories
                              .map((category) => employeeCategoryLabel(category))
                              .join(", ")}`
                          : ""}
                      </div>
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums">{formatHours(day.hours)}</td>
                    <td className="px-3 py-2 text-right tabular-nums">
                      {formatMoney(day.basePay)}
                      {day.weekdayPremium > 0 ? (
                        <div className="text-xs text-emerald-700">
                          в т.ч. пт/сб +{formatMoney(day.weekdayPremium)}
                        </div>
                      ) : null}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums">
                      {formatMoney(day.percentPay)}
                    </td>
                    <td
                      className={cn(
                        "px-3 py-2 text-right font-medium tabular-nums",
                        day.premium > 0 ? "text-emerald-700" : undefined,
                      )}
                    >
                      {formatSignedMoney(day.premium)}
                    </td>
                    <td
                      className={cn(
                        "px-3 py-2 text-right font-medium tabular-nums",
                        day.deduction > 0 ? "text-rose-700" : undefined,
                      )}
                    >
                      {formatSignedMoney(-day.deduction)}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums">
                      {formatMoney(day.vacationPay)}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums">
                      {formatMoney(day.fundAccrual)}
                    </td>
                    <td
                      className={cn(
                        "px-3 py-2 text-right font-semibold tabular-nums",
                        day.total > 0
                          ? "text-emerald-700"
                          : day.total < 0
                            ? "text-rose-700"
                            : undefined,
                      )}
                    >
                      {formatSignedMoney(day.total)}
                      {day.periodAdjustment !== 0 ? (
                        <div className="text-[11px] font-normal text-muted-foreground">
                          корректировка периода {formatSignedMoney(day.periodAdjustment)}
                        </div>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState
            title="Детализация смен не загружена"
            description="В строке есть итоговые суммы, но нет дневных компонентов."
          />
        )}
      </section>

      <section className="grid gap-3 rounded-lg border border-emerald-200 bg-emerald-50/50 p-4 lg:grid-cols-[1fr_auto] lg:items-center">
        <div>
          <div className="text-sm font-semibold text-emerald-950">Итог выплаты</div>
          <PayoutFormula line={row.line} />
        </div>
        <PaymentCell
          canManagePayments={canManagePayments}
          channelPerms={channelPerms}
          line={row.line}
        />
      </section>
    </div>
  );
}

type PayoutFormulaTerm = {
  label: string;
  amount: number;
};

function PayoutFormula({ line }: { line: PayrollLine }) {
  const salary = lineSalaryBeforeSettlement(line);
  const flows = lineSettlementFlows(line);
  const rounding = extractPayrollRounding(line);
  const terms: PayoutFormulaTerm[] = [
    { label: "удержание депозита", amount: -moneyValue(line.deposit_withholding) },
    { label: "выдача депозита", amount: moneyValue(line.deposit_payout) },
    { label: "аванс", amount: flows.advanceIssued },
    { label: "заём", amount: flows.loanIssued },
    { label: "аванс/заём", amount: flows.unspecifiedIssued },
    { label: "возврат аванса", amount: -flows.advanceRecovered },
    { label: "возврат займа", amount: -flows.loanRecovered },
    { label: "возврат аванса/займа", amount: -flows.unspecifiedRecovered },
    { label: "выплачено ранее", amount: -flows.previouslyPaid },
    { label: "округление до 5 ₽", amount: -rounding },
  ].filter((term) => Math.abs(term.amount) >= 0.005);

  const explainedTotal = normalizeMoney(salary + terms.reduce((sum, term) => sum + term.amount, 0));
  const finalTotal = lineOnHand(line);
  const unexplained = normalizeMoney(finalTotal - explainedTotal);
  if (Math.abs(unexplained) >= 0.005) {
    terms.push({
      label: unexplained > 0 ? "прочие выплаты" : "прочие удержания",
      amount: unexplained,
    });
  }

  return (
    <div className="mt-1 flex flex-wrap items-baseline gap-x-2 gap-y-1 text-sm text-emerald-900">
      <span>зарплата {formatMoney(salary)}</span>
      {terms.map((term, index) => (
        <span className="contents" key={`${term.label}-${index}`}>
          <span aria-hidden="true">{term.amount >= 0 ? "+" : "−"}</span>
          <span>
            {term.label} {formatMoney(Math.abs(term.amount))}
          </span>
        </span>
      ))}
      <span aria-hidden="true">=</span>
      <strong className="text-lg tabular-nums">{formatMoney(finalTotal)}</strong>
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
    // Депозит удерживается только по основной роли, но override хранится per-(employee, role).
    // Применяем его ко всем физическим строкам объединённого сотрудника, чтобы настройка
    // сохранилась при последующей смене основной роли и пересчёте.
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
          <div className="text-sm font-semibold">Удержание депозита</div>
          <div className="mt-1 text-sm text-muted-foreground">
            В расчёте: {formatMoney(line.deposit_withholding)}
          </div>
        </div>
        <Label
          className="flex items-center gap-2 text-sm"
          title={isFinal ? disabledReason : undefined}
        >
          <span>Удерживать</span>
          <Switch
            checked={!line.deposit_excluded_for_run}
            disabled={!canEditDeposit || isFinal || mutation.isPending}
            onCheckedChange={(checked) => {
              mutation.mutate({
                deposit_excluded_for_run: !checked,
                deposit_exclusion_reason: checked ? null : cleanOptionalText(reason),
              });
            }}
          />
        </Label>
      </div>
      <div className="text-xs leading-relaxed text-muted-foreground">
        Выключите, чтобы не удерживать депозит только в этой ведомости. Изменение применится после
        пересчёта и не затронет настройки сотрудника.
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

function AdjustmentDisclosure({
  defaultOpen = false,
  items,
  kind,
  title,
}: {
  defaultOpen?: boolean;
  items: AdjustmentComponent[];
  kind: "bonus" | "deduction";
  title: string;
}) {
  const total = items.reduce((sum, item) => sum + moneyValue(item.amount), 0);

  return (
    <details className="group rounded-md border bg-card" open={defaultOpen || undefined}>
      <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-3 py-2 text-sm [&::-webkit-details-marker]:hidden">
        <span className="flex items-center gap-2 font-semibold">
          <ChevronRight
            className="h-4 w-4 text-muted-foreground transition-transform group-open:rotate-90"
            aria-hidden="true"
          />
          {title}
          <span className="font-normal text-muted-foreground">· {items.length}</span>
        </span>
        <span
          className={cn(
            "font-medium tabular-nums",
            kind === "bonus" && total > 0 ? "text-emerald-700" : undefined,
            kind === "deduction" && total > 0 ? "text-rose-700" : undefined,
          )}
        >
          {kind === "bonus" && total > 0 ? "+" : kind === "deduction" && total > 0 ? "−" : ""}
          {formatMoney(total)}
        </span>
      </summary>
      <div className="border-t px-3 py-2">
        {items.length > 0 ? (
          <div className="divide-y">
            {items.map((item) => (
              <div
                className="flex flex-wrap items-center gap-x-2 gap-y-1 py-1.5 text-xs"
                key={item.id}
              >
                <span className="text-muted-foreground">{formatDate(item.workDate)}</span>
                <span className="border-l pl-2 font-medium">{adjustmentTypeLabel(item, kind)}</span>
                <span className="border-l pl-2">{item.category}</span>
                {item.comment ? (
                  <span className="min-w-0 flex-1 border-l pl-2 text-muted-foreground">
                    {item.comment}
                  </span>
                ) : (
                  <span className="min-w-0 flex-1" />
                )}
                <span
                  className={cn(
                    "ml-auto border-l pl-2 font-semibold tabular-nums",
                    kind === "bonus" ? "text-emerald-700" : "text-rose-700",
                  )}
                >
                  {kind === "bonus" ? "+" : "−"}
                  {formatMoney(item.amount)}
                </span>
              </div>
            ))}
          </div>
        ) : (
          <div className="text-xs text-muted-foreground">
            {kind === "bonus" ? "За этот период премий нет." : "Штрафов и удержаний нет."}
          </div>
        )}
      </div>
    </details>
  );
}

function adjustmentTypeLabel(item: AdjustmentComponent, kind: "bonus" | "deduction") {
  if (kind === "bonus") return "Премия";
  return item.category.toLowerCase().includes("штраф") ? "Штраф" : "Удержание";
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
  return PAYROLL_ROLE_LABELS[role as keyof typeof PAYROLL_ROLE_LABELS] ?? "Роль не распознана";
}

function employeeCategoryLabel(category: string): string {
  const label = EMPLOYEE_CATEGORY_LABELS[category as keyof typeof EMPLOYEE_CATEGORY_LABELS];
  if (!label) return "Категория не распознана";
  return category.startsWith("category_") ? `${label} категория` : label;
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
      advance_issuances: mergeComponentArray(lines, (components) => components.advance_issuances),
      advance_recoveries: mergeComponentArray(lines, (components) => components.advance_recoveries),
      employee_payout_offsets: mergeComponentArray(
        lines,
        (components) => components.employee_payout_offsets,
      ),
      payroll_rounding: {
        unit: "5.00",
        amount: lines.reduce((sum, line) => sum + extractPayrollRounding(line), 0),
      },
    },
  };
}

// «На руки» по строке = сумма ведомости (total_payable, включая аванс/заём через ведомость)
// + выдача депозита (хранится отдельно и в total_payable не входит).
function lineOnHand(line: PayrollLine) {
  return moneyValue(line.total_payable) + moneyValue(line.deposit_payout);
}

// paid_amount хранит только зарплатную часть. Для полностью закрытой строки выдача депозита
// также уже проведена отдельной корзиной и должна входить в показанное «выплачено».
function linePaidOnHand(line: PayrollLine) {
  const salaryPaid = moneyValue(line.paid_amount ?? 0);
  return salaryPaid + (line.payment_status === "paid" ? moneyValue(line.deposit_payout) : 0);
}

function lineRemainingOnHand(line: PayrollLine) {
  return normalizeMoney(Math.max(0, lineOnHand(line) - linePaidOnHand(line)));
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
  ndflWithheld: number;
};

type DailyPayoutRow = {
  date: string;
  roles: string[];
  categories: string[];
  hours: number;
  basePay: number;
  weekdayPremium: number;
  percentPay: number;
  vacationPay: number;
  fundAccrual: number;
  premium: number;
  deduction: number;
  ndflWithheld: number;
  periodAdjustment: number;
  total: number;
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
    ndflWithheld: Number(day.ndfl_withheld ?? 0),
  }));
}

function lineDailyPayoutRows(line: PayrollLine): DailyPayoutRow[] {
  const rows = new Map<string, DailyPayoutRow>();
  const ensureRow = (date: string) => {
    const key = date || "period";
    const existing = rows.get(key);
    if (existing) return existing;
    const next: DailyPayoutRow = {
      date,
      roles: [],
      categories: [],
      hours: 0,
      basePay: 0,
      weekdayPremium: 0,
      percentPay: 0,
      vacationPay: 0,
      fundAccrual: 0,
      premium: 0,
      deduction: 0,
      ndflWithheld: 0,
      periodAdjustment: 0,
      total: 0,
    };
    rows.set(key, next);
    return next;
  };

  for (const day of lineDays(line)) {
    const row = ensureRow(day.date);
    if (day.role && !row.roles.includes(day.role)) row.roles.push(day.role);
    if (day.category && !row.categories.includes(day.category)) row.categories.push(day.category);
    row.hours += day.hours;
    row.basePay += day.basePay;
    row.weekdayPremium += day.weekdayPremium;
    row.percentPay += day.percentPay;
    row.vacationPay += day.vacationPay;
    row.fundAccrual += day.fundAccrual;
    row.ndflWithheld += day.ndflWithheld;
  }

  const adjustments = lineAdjustments(line);
  for (const bonus of adjustments.bonuses) {
    ensureRow(bonus.workDate).premium += moneyValue(bonus.amount);
  }
  for (const penalty of adjustments.penalties) {
    ensureRow(penalty.workDate).deduction += moneyValue(penalty.amount);
  }

  const result = Array.from(rows.values()).sort((left, right) =>
    left.date.localeCompare(right.date),
  );
  for (const row of result) {
    row.total = normalizeMoney(
      row.basePay +
        row.percentPay +
        row.premium +
        row.vacationPay -
        row.deduction -
        row.ndflWithheld,
    );
  }

  // Современный расчёт раскладывает все зарплатные компоненты по дням. Для старой
  // импортированной ведомости часть суммы может быть только в итоговых полях строки —
  // сохраняем равенство «зарплата = сумма дневных итогов» и явно помечаем поправку.
  if (result.length > 0) {
    const rowsTotal = result.reduce((sum, row) => sum + row.total, 0);
    const residual = normalizeMoney(lineSalaryBeforeSettlement(line) - rowsTotal);
    if (Math.abs(residual) >= 0.005) {
      result[0].periodAdjustment = residual;
      result[0].total = normalizeMoney(result[0].total + residual);
    }
  }

  return result;
}

function lineSalaryBeforeSettlement(line: PayrollLine) {
  const salaryDeductions = Math.max(
    0,
    moneyValue(line.deduction) - moneyValue(line.deposit_withholding),
  );
  return normalizeMoney(
    moneyValue(line.base_pay) +
      moneyValue(line.percent_pay) +
      moneyValue(line.premium) +
      moneyValue(line.vacation_pay) -
      salaryDeductions -
      moneyValue(line.ndfl_withheld),
  );
}

function lineSettlementFlows(line: PayrollLine) {
  const issuances = lineComponentMoneyItems(line, "advance_issuances");
  const recoveries = lineComponentMoneyItems(line, "advance_recoveries");
  const payoutOffsets = lineComponentMoneyItems(line, "employee_payout_offsets");
  const issuedByKind = flowAmountsByKind(issuances);
  const recoveredByKind = flowAmountsByKind(recoveries);
  const detailedIssued = issuedByKind.advance + issuedByKind.loan + issuedByKind.unspecified;

  return {
    advanceIssued: issuedByKind.advance,
    loanIssued: issuedByKind.loan,
    unspecifiedIssued: normalizeMoney(
      issuedByKind.unspecified + Math.max(0, moneyValue(line.advance_issued) - detailedIssued),
    ),
    advanceRecovered: recoveredByKind.advance,
    loanRecovered: recoveredByKind.loan,
    unspecifiedRecovered: recoveredByKind.unspecified,
    previouslyPaid: normalizeMoney(payoutOffsets.reduce((sum, item) => sum + item.amount, 0)),
  };
}

type LineComponentMoneyItem = {
  kind: string;
  amount: number;
};

function lineComponentMoneyItems(line: PayrollLine, key: string): LineComponentMoneyItem[] {
  const value = isRecord(line.components) ? line.components[key] : undefined;
  if (!Array.isArray(value)) return [];
  return value.filter(isRecord).map((item) => ({
    kind: String(item.kind ?? ""),
    amount: moneyValue(item.amount as number | string | null | undefined),
  }));
}

function flowAmountsByKind(items: LineComponentMoneyItem[]) {
  return items.reduce(
    (totals, item) => {
      const key = item.kind === "advance" || item.kind === "loan" ? item.kind : "unspecified";
      totals[key] += item.amount;
      return totals;
    },
    { advance: 0, loan: 0, unspecified: 0 },
  );
}

function lineAdjustments(line: PayrollLine) {
  const adjustments = isRecord(line.components.adjustments) ? line.components.adjustments : {};
  return {
    bonuses: adjustmentItems(adjustments.bonuses),
    penalties: adjustmentItems(adjustments.penalties),
  };
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
