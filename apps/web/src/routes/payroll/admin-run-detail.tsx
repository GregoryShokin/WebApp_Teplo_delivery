import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeft,
  CalendarClock,
  CheckCircle2,
  LoaderCircle,
  RefreshCw,
  Search,
  Undo2,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
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
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import { EmptyState } from "@/components/ui-app/EmptyState";
import { PageHeader } from "@/components/ui-app/PageHeader";
import { StatusBadge } from "@/components/ui-app/StatusBadge";
import {
  apiErrorMessage,
  bulkMarkPayrollPayments,
  createAdminPayrollRun,
  deferPayrollAdvanceRecovery,
  finalizePayrollRun,
  getAdminPayrollRun,
  getAdminPayrollRunLines,
  getEmployees,
  getPayrollAdvances,
  unfinalizePayrollRun,
  unmarkPayrollPayment,
  type Employee,
  type PayrollAdvance,
  type PayrollCashWalletCode,
  type PayrollLine,
} from "@/lib/api";
import { usePermissions } from "@/lib/permissions";

import { AdminLineDialog } from "./admin-line-dialog";
import { AdminPayoutDialog } from "./admin-payout-dialog";
import { KpiCard } from "./admin-payslip-shared";
import {
  dishwasherShiftCount,
  extractEmployeePayoutOffset,
  extractRecoveries,
  formatDateTime,
  isFinalStatus,
  onDemandPeriodAccrual,
  paymentMethodLabel,
  todayDateInputValue,
} from "./admin-payslip-utils";
import { EmployeePayoutDialog } from "./employee-payout-dialog";
import { CashPayoutSourcePicker, type PayrollCashChannelPerms } from "./cash-payout-source-picker";
import { RecoveryDialog } from "./recovery-dialog";
import { formatDate, formatMoney, formatPeriodRange } from "./runs";

type PayrollAdminRunDetailRouteProps = {
  runId: string;
  onNavigate: (path: string) => void;
};

type AdminLineRowModel = {
  line: PayrollLine;
  employee?: Employee;
  employeeName: string;
  position: string;
};

export function PayrollAdminRunDetailRoute({ runId, onNavigate }: PayrollAdminRunDetailRouteProps) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const canRecalculate = permissions.canPerformAction("payroll.runs.admin.recalculate");
  const canFinalizeRuns = permissions.canPerformAction("payroll.runs.finalize");
  const canReopenRuns = permissions.canPerformAction("payroll.runs.reopen");
  const canMarkPaid = permissions.canPerformAction("payroll.runs.mark_paid");
  const canManageLoans = permissions.canPerformAction("payroll.loans.issue");
  const canCreatePayout = permissions.canPerformAction("payroll.employee_payouts.create");
  const canBankDraft = permissions.canPerformAction("payroll.runs.bank_draft");
  const payoutChannelPerms = {
    safe: permissions.hasPermission("finance.payout_channel.safe"),
    cash_tk: permissions.hasPermission("finance.payout_channel.cash_tk"),
    bank_draft: permissions.hasPermission("finance.payout_channel.bank_draft"),
  };
  const [isRecalculateDialogOpen, setIsRecalculateDialogOpen] = useState(false);
  const [isUnfinalizeDialogOpen, setIsUnfinalizeDialogOpen] = useState(false);
  const [unfinalizeReason, setUnfinalizeReason] = useState("");

  const runQuery = useQuery({
    queryKey: ["payroll-admin-run", runId],
    queryFn: () => getAdminPayrollRun(runId),
  });
  const linesQuery = useQuery({
    queryKey: ["payroll-admin-run-lines", runId],
    queryFn: () => getAdminPayrollRunLines(runId),
  });
  const employeesQuery = useQuery({
    queryKey: ["employees", "payroll-line-map"],
    queryFn: () => getEmployees({ status: "all" }),
  });
  const advancesQuery = useQuery({
    queryKey: ["payroll-advances", "all"],
    queryFn: () => getPayrollAdvances(),
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
  const loans = useMemo(
    () =>
      (advancesQuery.data ?? []).filter(
        (advance) => advance.kind === "loan" && advance.status === "issued",
      ),
    [advancesQuery.data],
  );
  const totalPayable = Number(run?.summary.total_payable ?? 0);
  const employeeCount = new Set(lines.map((line) => line.employee_id)).size;
  const blockers = run?.blocking_issues ?? [];
  const excludedNoOklad = useMemo(() => {
    const raw = run?.summary?.excluded_no_oklad;
    if (!Array.isArray(raw)) {
      return [] as string[];
    }
    return raw
      .map((item) => {
        if (item && typeof item === "object") {
          const record = item as Record<string, unknown>;
          const name = typeof record.employee_name === "string" ? record.employee_name : "";
          const position = typeof record.position === "string" ? ` (${record.position})` : "";
          return name ? `${name}${position}` : "";
        }
        return "";
      })
      .filter(Boolean);
  }, [run?.summary]);
  const excludedWrongPosition = useMemo(() => {
    const raw = run?.summary?.excluded_wrong_position;
    if (!Array.isArray(raw)) {
      return [] as string[];
    }
    return raw
      .map((item) => {
        if (item && typeof item === "object") {
          const record = item as Record<string, unknown>;
          const name = typeof record.employee_name === "string" ? record.employee_name : "";
          const position =
            typeof record.period_position === "string" ? record.period_position : "—";
          return name ? `${name} (в периоде: ${position})` : "";
        }
        return "";
      })
      .filter(Boolean);
  }, [run?.summary]);
  const isFinal = run ? isFinalStatus(run.status) : false;
  // Ведомость посчитана, но после расчёта появились авансы/займы, не учтённые в итоге
  // (например, выданные задним числом). Финализация заблокирована до пересчёта.
  const needsRecalc = run?.status === "completed" && Boolean(run?.needs_recalc);
  const canFinalize =
    Boolean(run) &&
    run?.status === "completed" &&
    blockers.length === 0 &&
    !needsRecalc &&
    !isFinal &&
    canFinalizeRuns;
  const canUnfinalize = Boolean(run) && isFinal && canReopenRuns;
  const canManagePayments = Boolean(run) && run?.status === "finalized" && canMarkPaid;
  const canSubmitUnfinalize = unfinalizeReason.trim().length > 0;

  const invalidateRunQueries = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ["payroll-admin-run", runId] }),
      queryClient.invalidateQueries({ queryKey: ["payroll-admin-run-lines", runId] }),
      queryClient.invalidateQueries({ queryKey: ["payroll-admin-runs"] }),
    ]);

  const finalizeMutation = useMutation({
    mutationFn: () => finalizePayrollRun(runId),
    onSuccess: async () => {
      toast.success("Ведомость финализирована");
      await invalidateRunQueries();
    },
    onError: (error) => toast.error((error as Error).message),
  });

  const unfinalizeMutation = useMutation({
    mutationFn: (reason: string) => unfinalizePayrollRun(runId, reason),
    onSuccess: async () => {
      await invalidateRunQueries();
      setIsUnfinalizeDialogOpen(false);
      setUnfinalizeReason("");
      toast.success("Ведомость возвращена в работу");
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось вернуть ведомость в работу"));
    },
  });

  const recalculateMutation = useMutation({
    mutationFn: () => createAdminPayrollRun({ periodId: run?.period_id, forceRefresh: true }),
    onSuccess: async () => {
      await invalidateRunQueries();
      setIsRecalculateDialogOpen(false);
      toast.success("Ведомость обновлена");
    },
    onError: (error) => {
      setIsRecalculateDialogOpen(false);
      toast.error(apiErrorMessage(error, "Не удалось пересчитать ведомость"));
    },
  });

  // Недельная (производственная) ведомость по админской ссылке — уводим на её страницу:
  // «Пересчитать» здесь пересобирает оклады администрации, для производственников это не то.
  useEffect(() => {
    if (run?.period?.period_type === "week") {
      onNavigate(`/payroll/runs/${runId}`);
    }
  }, [onNavigate, run?.period?.period_type, runId]);

  function finalize() {
    if (isFinal || !canFinalize) {
      return;
    }
    if (
      !window.confirm(
        "Финализировать ведомость? После закрытия повторный расчёт будет заблокирован.",
      )
    ) {
      return;
    }
    finalizeMutation.mutate();
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title={run ? formatPeriodRange(run.period) : "Ведомость администрации"}
        description={run ? runMeta(run) : "Загрузка ведомости"}
        action={
          <>
            {run ? <StatusBadge status={run.status} /> : null}
            <Button onClick={() => onNavigate("/payroll/admin")} title="Назад" variant="outline">
              <ArrowLeft size={16} aria-hidden="true" />
              Назад
            </Button>
            {canRecalculate ? (
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
                      Пересчитать ведомость за {run ? formatPeriodRange(run.period) : "период"}?
                    </AlertDialogTitle>
                    <AlertDialogDescription>
                      Строки ведомости будут пересозданы с актуальными окладами, премиями и
                      штрафами.
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
                          recalculateMutation.mutate();
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
                    <Label htmlFor="admin-unfinalize-reason">Причина</Label>
                    <Textarea
                      disabled={unfinalizeMutation.isPending}
                      id="admin-unfinalize-reason"
                      onChange={(event) => setUnfinalizeReason(event.target.value)}
                      placeholder="Например: забыли премию"
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

      <section className="grid gap-3 md:grid-cols-2">
        <KpiCard title="Сотрудников" value={String(employeeCount)} description="В ведомости" />
        <KpiCard title="ФОТ итого" value={formatMoney(totalPayable)} description="К выплате" />
      </section>

      {blockers.length > 0 ? (
        <section className="space-y-2 rounded-lg border border-destructive/40 bg-destructive/5 p-4">
          <div className="text-sm font-semibold text-destructive">
            Ведомость заблокирована — устраните причины и пересчитайте
          </div>
          <ul className="space-y-1 text-sm text-foreground/80">
            {blockers.map((issue, index) => (
              <li className="flex gap-2" key={index}>
                <span aria-hidden="true">•</span>
                <span>{blockerMessage(issue)}</span>
              </li>
            ))}
          </ul>
          <div className="text-xs text-muted-foreground">
            Оклады задаются в «Исходные данные → Оклады администрации».
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

      {excludedNoOklad.length > 0 ? (
        <section className="space-y-1 rounded-lg border bg-muted/40 p-4">
          <div className="text-sm font-medium">Не вошли в ведомость — не задан оклад</div>
          <ul className="space-y-1 text-sm text-muted-foreground">
            {excludedNoOklad.map((label, index) => (
              <li key={index}>• {label}</li>
            ))}
          </ul>
          <div className="text-xs text-muted-foreground">
            Задайте оклад в «Оклады администрации» и пересчитайте, либо отметьте «Не платить», если
            сотрудник не должен получать оклад.
          </div>
        </section>
      ) : null}

      {excludedWrongPosition.length > 0 ? (
        <section className="space-y-1 rounded-lg border bg-muted/40 p-4">
          <div className="text-sm font-medium">
            Не вошли — в этом периоде были на другой должности
          </div>
          <ul className="space-y-1 text-sm text-muted-foreground">
            {excludedWrongPosition.map((label, index) => (
              <li key={index}>• {label}</li>
            ))}
          </ul>
          <div className="text-xs text-muted-foreground">
            За этот период такой сотрудник считается по своей тогдашней должности (например в
            производственной ведомости), а как администратор попадёт в ведомости с момента
            назначения.
          </div>
        </section>
      ) : null}

      {runQuery.isError ? (
        <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {(runQuery.error as Error).message}
        </div>
      ) : null}

      {run && run.status === "finalized" && canBankDraft ? (
        <AdminPayoutDialog
          channelPerms={payoutChannelPerms}
          runId={runId}
          run={run}
          totalPayable={totalPayable}
        />
      ) : null}

      <AdminLinesTable
        canManagePayments={canManagePayments}
        canCreatePayout={canCreatePayout}
        canDeferLoans={canManageLoans && run?.status !== "finalized"}
        channelPerms={payoutChannelPerms}
        employeesById={employeesById}
        isLoading={linesQuery.isLoading || runQuery.isLoading}
        lines={lines}
        loans={loans}
        periodEnd={run?.period?.end_date}
        periodLabel={run?.period ? formatPeriodRange(run.period) : ""}
        runId={runId}
      />
    </div>
  );
}

function AdminLinesTable({
  canManagePayments,
  canCreatePayout,
  canDeferLoans,
  channelPerms,
  employeesById,
  isLoading,
  lines,
  loans,
  periodEnd,
  periodLabel,
  runId,
}: {
  canManagePayments: boolean;
  canCreatePayout: boolean;
  canDeferLoans: boolean;
  channelPerms: PayrollCashChannelPerms;
  employeesById: Map<string, Employee>;
  isLoading: boolean;
  lines: PayrollLine[];
  loans: PayrollAdvance[];
  periodEnd?: string;
  periodLabel: string;
  runId: string;
}) {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const loansByEmployee = useMemo(() => {
    const map = new Map<string, PayrollAdvance[]>();
    for (const loan of loans) {
      const list = map.get(loan.employee_id) ?? [];
      list.push(loan);
      map.set(loan.employee_id, list);
    }
    return map;
  }, [loans]);
  const [selectedEmployeeIds, setSelectedEmployeeIds] = useState<Set<string>>(new Set());
  const [recoveryEmployeeId, setRecoveryEmployeeId] = useState<string | null>(null);
  const [payoutRow, setPayoutRow] = useState<AdminLineRowModel | null>(null);
  const [detailRow, setDetailRow] = useState<AdminLineRowModel | null>(null);
  const [bulkDialogOpen, setBulkDialogOpen] = useState(false);
  const [bulkWalletCode, setBulkWalletCode] = useState<PayrollCashWalletCode | null>(null);
  const [bulkCanSubmit, setBulkCanSubmit] = useState(false);

  const unpaidEmployeeIds = useMemo(
    () =>
      Array.from(
        new Set(
          lines
            // on_demand-строки не выплачиваются через ведомость (к выплате 0, платятся
            // вручную по кнопке «Включить в выплату») — исключаем из массовой отметки.
            .filter((line) => line.payment_status !== "paid" && !line.on_demand)
            .map((line) => line.employee_id),
        ),
      ),
    [lines],
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
    mutationFn: ({
      employeeIds,
      walletCode,
    }: {
      employeeIds: string[];
      walletCode: PayrollCashWalletCode;
    }) => bulkMarkPayrollPayments(runId, employeeIds, todayDateInputValue(), walletCode),
    onSuccess: async (response) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["payroll-admin-run", runId] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-admin-run-lines", runId] }),
        queryClient.invalidateQueries({ queryKey: ["run-funding-sources", runId] }),
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

  const rows = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const prepared: AdminLineRowModel[] = lines.map((line) => {
      const employee = employeesById.get(line.employee_id);
      return {
        line,
        employee,
        employeeName: employee?.full_name ?? "Сотрудник требует настройки",
        position: employee?.position ?? line.role ?? "",
      };
    });
    return prepared
      .filter((row) => (needle ? row.employeeName.toLowerCase().includes(needle) : true))
      .sort((left, right) => left.employeeName.localeCompare(right.employeeName, "ru"));
  }, [employeesById, lines, search]);
  const selectedAmount = lines
    .filter((line) => selectedEmployeeIds.has(line.employee_id))
    .reduce(
      (sum, line) => sum + Math.max(0, Number(line.total_payable) - Number(line.paid_amount ?? 0)),
      0,
    );

  const tableColumns: Array<DataTableColumn<AdminLineRowModel>> = [
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
            cell: (row: AdminLineRowModel) =>
              row.line.payment_status === "paid" || row.line.on_demand ? null : (
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
          } satisfies DataTableColumn<AdminLineRowModel>,
        ]
      : []),
    {
      key: "name",
      header: "Имя",
      cell: (row) => (
        <div>
          <div className="font-medium">{row.employeeName}</div>
          <div className="text-xs text-muted-foreground">
            {row.position || "Должность не указана"}
          </div>
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
      key: "base_pay",
      header: "Начислено",
      cell: (row) => {
        if (row.line.on_demand) {
          return (
            <div className="flex flex-col items-end">
              <span>{formatMoney(onDemandPeriodAccrual(row.line))}</span>
              <span className="text-xs text-muted-foreground">оклад за месяц</span>
            </div>
          );
        }
        const shifts = dishwasherShiftCount(row.line);
        return (
          <div className="flex flex-col items-end">
            <span>{formatMoney(row.line.base_pay)}</span>
            {shifts !== null ? (
              <span className="text-xs text-muted-foreground">{shifts} смен</span>
            ) : null}
          </div>
        );
      },
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "premium",
      header: "Премии",
      cell: (row) => formatMoney(row.line.premium),
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "deduction",
      header: "Штрафы",
      cell: (row) => {
        const total = Number(row.line.deduction ?? 0);
        if (total <= 0) {
          return <span className="text-muted-foreground">0 ₽</span>;
        }
        return <span className="text-rose-700">−{formatMoney(total)}</span>;
      },
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "advance_issued",
      header: "Авансы/займы",
      cell: (row) => {
        const amount = Number(row.line.advance_issued ?? 0);
        if (amount <= 0) {
          return <span className="text-muted-foreground">0 ₽</span>;
        }
        return <span className="text-emerald-700">+{formatMoney(amount)}</span>;
      },
      className: "text-right tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "total",
      header: "К выплате",
      cell: (row) => {
        const bankPaid = extractEmployeePayoutOffset(row.line);
        if (row.line.on_demand) {
          return (
            <span
              className="text-muted-foreground"
              title="Оклад «по востребованию» — не выплачивается автоматически"
            >
              —
            </span>
          );
        }
        return (
          <div className="flex flex-col items-end">
            <span>{formatMoney(row.line.total_payable)}</span>
            {bankPaid > 0 ? (
              <span
                className="text-xs font-normal text-muted-foreground"
                title="Уже выплачено банком — привязка операции журнала ДДС к сотруднику"
              >
                уже выпл. банком {formatMoney(bankPaid)}
              </span>
            ) : null}
          </div>
        );
      },
      className: "text-right font-semibold tabular-nums",
      headerClassName: "text-right",
    },
    {
      key: "loan",
      header: "Удержание",
      cell: (row) => (
        <LoanRecoveryCell
          canDefer={canDeferLoans}
          line={row.line}
          loans={loansByEmployee.get(row.line.employee_id) ?? []}
          periodEnd={periodEnd}
          runId={runId}
        />
      ),
      className: "min-w-[180px]",
    },
    {
      key: "payment",
      header: "Выплата",
      cell: (row) =>
        row.line.on_demand ? (
          <OnDemandPayoutCell
            canCreatePayout={canCreatePayout}
            line={row.line}
            onPay={() => setPayoutRow(row)}
          />
        ) : (
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
            placeholder="Сотрудник"
            value={search}
          />
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="text-sm text-muted-foreground">{rows.length} строк</div>
          {canManagePayments ? (
            <Button
              disabled={selectedCount === 0 || bulkMarkMutation.isPending}
              onClick={() => {
                setBulkWalletCode(null);
                setBulkDialogOpen(true);
              }}
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

      <Dialog onOpenChange={setBulkDialogOpen} open={bulkDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Выплатить выбранным сотрудникам</DialogTitle>
            <DialogDescription>
              {selectedCount} сотрудников · к выплате {formatMoney(selectedAmount)}. Выберите счёт
              фактической выдачи.
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
              onClick={() =>
                bulkWalletCode &&
                bulkMarkMutation.mutate({
                  employeeIds: Array.from(selectedEmployeeIds),
                  walletCode: bulkWalletCode,
                })
              }
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
          title="Строк ведомости нет"
          description="После создания ведомости здесь появятся сотрудники и оклады к выплате."
        />
      ) : (
        <DataTable
          columns={tableColumns}
          rows={rows}
          isLoading={isLoading}
          getRowKey={(row) => row.line.id}
          onRowClick={(row) => setDetailRow(row)}
          emptyMessage="Сотрудники по фильтру не найдены"
        />
      )}

      <AdminLineDialog
        canCreatePayout={canCreatePayout}
        canManagePayments={canManagePayments}
        channelPerms={channelPerms}
        onIncludeOnDemand={(row) => setPayoutRow(row)}
        onManageRecovery={(employeeId) => setRecoveryEmployeeId(employeeId)}
        onOpenChange={(open) => {
          if (!open) {
            setDetailRow(null);
          }
        }}
        open={detailRow !== null}
        periodLabel={periodLabel}
        row={detailRow}
      />

      <RecoveryDialog
        employeeId={recoveryEmployeeId}
        onOpenChange={(open) => {
          if (!open) {
            setRecoveryEmployeeId(null);
          }
        }}
        open={recoveryEmployeeId !== null}
        runId={runId}
      />

      <EmployeePayoutDialog
        accrued={payoutRow?.line.on_demand_accrued ?? 0}
        employeeId={payoutRow?.line.employee_id ?? null}
        employeeName={payoutRow?.employeeName ?? ""}
        onOpenChange={(open) => {
          if (!open) {
            setPayoutRow(null);
          }
        }}
        open={payoutRow !== null}
        paid={payoutRow?.line.on_demand_paid ?? 0}
        remaining={payoutRow?.line.on_demand_debt ?? 0}
        runId={runId}
      />
    </div>
  );
}

function OnDemandPayoutCell({
  canCreatePayout,
  line,
  onPay,
}: {
  canCreatePayout: boolean;
  line: PayrollLine;
  onPay: () => void;
}) {
  const remaining = Number(line.on_demand_debt ?? 0);
  return (
    <div className="flex flex-col items-start gap-1">
      <span className="text-xs text-muted-foreground">
        Остаток:{" "}
        <span
          className={`tabular-nums ${remaining > 0 ? "font-medium text-rose-700" : "text-foreground"}`}
        >
          {formatMoney(remaining)}
        </span>
      </span>
      {canCreatePayout ? (
        <Button
          onClick={(event) => {
            event.stopPropagation();
            onPay();
          }}
          size="sm"
          type="button"
          variant="outline"
        >
          Включить в выплату
        </Button>
      ) : null}
    </div>
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
  const unmarkMutation = useMutation({
    mutationFn: () => unmarkPayrollPayment(line.run_id, line.employee_id),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["payroll-admin-run", line.run_id] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-admin-run-lines", line.run_id] }),
      ]);
      toast.success("Отметка выплаты отменена");
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось отменить отметку"));
    },
  });

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
    </div>
  );
}

function LoanRecoveryCell({
  canDefer,
  line,
  loans,
  periodEnd,
  runId,
}: {
  canDefer: boolean;
  line: PayrollLine;
  loans: PayrollAdvance[];
  periodEnd?: string;
  runId: string;
}) {
  const queryClient = useQueryClient();
  const recoveries = extractRecoveries(line);
  // Удерживается всё (аванс + заём), но отсрочить можно только заём.
  const total = recoveries.reduce((sum, item) => sum + item.amount, 0);
  const loanRecoveries = recoveries.filter((item) => item.kind === "loan" && item.advanceId);
  const deferredLoans = periodEnd
    ? loans.filter(
        (loan) => loan.recovery_start_date !== null && loan.recovery_start_date > periodEnd,
      )
    : [];

  const deferMutation = useMutation({
    mutationFn: ({ advanceId, defer }: { advanceId: string; defer: boolean }) =>
      deferPayrollAdvanceRecovery(runId, advanceId, defer),
    onSuccess: async (_data, variables) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["payroll-admin-run", runId] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-admin-run-lines", runId] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-advances", "all"] }),
      ]);
      toast.success(variables.defer ? "Удержание займа отсрочено" : "Удержание займа возвращено");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось изменить удержание")),
  });

  if (total <= 0 && deferredLoans.length === 0) {
    return <span className="text-muted-foreground">—</span>;
  }

  return (
    <div className="flex flex-col items-start gap-1">
      {total > 0 ? <span className="tabular-nums text-rose-700">−{formatMoney(total)}</span> : null}
      {canDefer
        ? loanRecoveries.map((item) => (
            <Button
              key={item.advanceId}
              disabled={deferMutation.isPending}
              onClick={(event) => {
                event.stopPropagation();
                deferMutation.mutate({ advanceId: item.advanceId, defer: true });
              }}
              size="sm"
              type="button"
              variant="outline"
            >
              {deferMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
              ) : (
                <CalendarClock size={15} aria-hidden="true" />
              )}
              Отсрочить заём
            </Button>
          ))
        : null}
      {deferredLoans.length > 0 ? (
        <div className="flex flex-col items-start gap-1">
          <Badge className="rounded-md border-amber-200 bg-amber-50 text-amber-800 shadow-none">
            Заём отсрочен
          </Badge>
          {canDefer
            ? deferredLoans.map((loan) => (
                <Button
                  key={loan.id}
                  disabled={deferMutation.isPending}
                  onClick={(event) => {
                    event.stopPropagation();
                    deferMutation.mutate({ advanceId: loan.id, defer: false });
                  }}
                  size="sm"
                  type="button"
                  variant="outline"
                >
                  {deferMutation.isPending ? (
                    <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
                  ) : (
                    <Undo2 size={15} aria-hidden="true" />
                  )}
                  Вернуть
                </Button>
              ))
            : null}
        </div>
      ) : null}
    </div>
  );
}

function blockerMessage(issue: Record<string, unknown>): string {
  if (typeof issue.message === "string" && issue.message.trim()) {
    return issue.message;
  }
  if (issue.type === "missing_admin_oklad") {
    const name = typeof issue.employee_name === "string" ? issue.employee_name : "сотрудник";
    const position = typeof issue.position === "string" ? ` (${issue.position})` : "";
    return `Не задан оклад: ${name}${position}`;
  }
  return typeof issue.type === "string" ? issue.type : "Неизвестная ошибка";
}

function runMeta(run: {
  started_at: string;
  finished_at: string | null;
  period: { finalized_at: string | null } | null;
}) {
  const parts = [`Создана ${formatDateTime(run.started_at)}`];
  if (run.finished_at) {
    parts.push(`посчитана ${formatDateTime(run.finished_at)}`);
  }
  if (run.period?.finalized_at) {
    parts.push(`финализирована ${formatDateTime(run.period.finalized_at)}`);
  }
  return parts.join(" · ");
}
