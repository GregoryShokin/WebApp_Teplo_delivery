import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowRightLeft, Check, Loader2, Pencil, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  cancelPayrollReserve,
  getEmployees,
  getPayrollRunLines,
  getRunSolvency,
  getPayrollReservePlan,
  editPayrollReservePlan,
  payRunFromPool,
  transferPayrollReserve,
  type PayrollLine,
} from "@/lib/api";
import { cn } from "@/lib/utils";

import type { PaymentRow } from "./payments-api";
import { todayIso } from "@/lib/date";

const money = new Intl.NumberFormat("ru-RU", {
  style: "currency",
  currency: "RUB",
  minimumFractionDigits: 0,
  maximumFractionDigits: 2,
});

type RegisterRow = {
  employeeId: string;
  name: string;
  accrued: number;
  paid: number;
  remaining: number;
  status: string;
  depositScheduled: number;
};

// Окно работы с пулом-резервом ЗП (Сейф/касса): выбранную раскладку можно выплатить из
// текущего счёта, перенести вместе с деньгами на второй наличный счёт или отменить резерв.
// Карандаш сохраняет только план. Факт выдачи создаёт отдельная кнопка «Выплатить».
export function PayPayrollReserveDialog({
  row,
  onOpenChange,
  onPaid,
}: {
  row: PaymentRow | null;
  onOpenChange: (open: boolean) => void;
  onPaid: () => void | Promise<void>;
}) {
  const queryClient = useQueryClient();
  const runId = (row?.extra?.run_id as string | undefined) ?? null;
  const reserveId = row?.ref_id ?? null;
  const isKassa = row?.bucket === "reserved_kassa" || row?.extra?.location === "kassa";
  const channel = isKassa ? "кассы" : "Сейфа";

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");
  const [cancelConfirmOpen, setCancelConfirmOpen] = useState(false);
  const [payConfirmOpen, setPayConfirmOpen] = useState(false);
  const [remainderEdit, setRemainderEdit] = useState<{
    employeeId: string;
    name: string;
    amount: number;
    remainder: number;
  } | null>(null);

  const planQuery = useQuery({
    queryKey: ["payroll-reserve-plan", reserveId],
    queryFn: () => getPayrollReservePlan(reserveId as string),
    enabled: Boolean(row && reserveId),
  });
  const plan = planQuery.data;
  const poolLeft = plan?.outstanding ?? 0;
  const planned = useMemo(
    () => new Map((plan?.allocations ?? []).map((i) => [i.employee_id, i])),
    [plan],
  );

  const linesQuery = useQuery({
    queryKey: ["payroll-run-lines", runId],
    queryFn: () => getPayrollRunLines(runId as string),
    enabled: Boolean(runId) && Boolean(row),
  });
  const employeesQuery = useQuery({
    queryKey: ["employees", "all"],
    queryFn: () => getEmployees({ status: "all" }),
    enabled: Boolean(row),
  });
  const solvencyQuery = useQuery({
    queryKey: ["run-solvency", runId],
    queryFn: () => getRunSolvency(runId as string),
    enabled: Boolean(runId) && Boolean(row),
  });

  const rows = useMemo<RegisterRow[]>(() => {
    const lines = linesQuery.data ?? [];
    const names = new Map((employeesQuery.data ?? []).map((e) => [e.id, e.full_name]));
    const byEmployee = new Map<string, RegisterRow>();
    for (const line of lines as PayrollLine[]) {
      const cur = byEmployee.get(line.employee_id) ?? {
        employeeId: line.employee_id,
        name: names.get(line.employee_id) ?? "Сотрудник",
        accrued: 0,
        paid: 0,
        remaining: 0,
        status: "pending",
        depositScheduled: 0,
      };
      cur.accrued += line.total_payable;
      cur.depositScheduled += line.deposit_payout_scheduled ?? 0;
      if (line.payment_status === "paid" || line.payment_status === "partially_paid") {
        cur.paid = line.paid_amount ?? 0;
        cur.status = line.payment_status;
      }
      byEmployee.set(line.employee_id, cur);
    }
    return Array.from(byEmployee.values()).map((r) => ({
      ...r,
      remaining: Math.max(0, Math.round((r.accrued - r.paid) * 100) / 100),
    }));
  }, [linesQuery.data, employeesQuery.data]);

  // Депозит-сотрудники исключены (как backend run_pool_shares) — идут полным путём «Выплатить».
  const payable = useMemo(
    () => rows.filter((r) => r.remaining > 0.001 && r.depositScheduled <= 0.001),
    [rows],
  );

  // По умолчанию отмечаем всех с долгом; остаток пула — из резерва.
  useEffect(() => {
    if (row) {
      setSelected(new Set(payable.map((r) => r.employeeId)));
      setEditingId(null);
      setRemainderEdit(null);
      setPayConfirmOpen(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [row?.id, payable.length]);

  const preview = useMemo(
    () =>
      new Map(
        (plan?.allocations ?? [])
          .filter((i) => selected.has(i.employee_id))
          .map((i) => [i.employee_id, i.amount]),
      ),
    [plan, selected],
  );
  const covered = Array.from(preview.values()).reduce((a, b) => a + b, 0);
  const selectedRemaining = payable
    .filter((r) => selected.has(r.employeeId))
    .reduce((a, r) => a + r.remaining, 0);
  const uncoveredHere = Math.max(0, Math.round((selectedRemaining - covered) * 100) / 100);

  const solvency = solvencyQuery.data;
  const loading = linesQuery.isLoading || employeesQuery.isLoading || planQuery.isLoading;

  async function refreshAfterPay() {
    // Полная связка с ведомостью: обновляем и деталь (строки/шапка/черновик/дельта), и список
    // ведомостей (бейдж «Выплачено частично», paid_total) — тот же набор, что реестр run-detail.
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["payroll-run", runId] }),
      queryClient.invalidateQueries({ queryKey: ["payroll-run-lines", runId] }),
      queryClient.invalidateQueries({ queryKey: ["payroll-runs"] }),
      queryClient.invalidateQueries({ queryKey: ["run-bank-draft", runId] }),
      queryClient.invalidateQueries({ queryKey: ["run-payout-delta", runId] }),
      queryClient.invalidateQueries({ queryKey: ["run-solvency", runId] }),
      queryClient.invalidateQueries({ queryKey: ["payroll-reserve-plan"] }),
    ]);
    await onPaid();
  }

  // Авто-раскладка пула по выбранным (+граничный); второй счёт теперь выбирается явно.
  const payMutation = useMutation({
    mutationFn: () =>
      payRunFromPool(reserveId as string, {
        selected_ids: Array.from(selected),
        plan_version: plan!.version,
        allow_overflow: false,
        paid_at: todayIso(),
      }),
    onSuccess: async (res) => {
      setPayConfirmOpen(false);
      await refreshAfterPay();
      toast.success(
        `Выплачено ${money.format(res.primary_booked)} из ${channel} — ${res.employees_paid} чел.`,
      );
      onOpenChange(false);
    },
    onError: (error: unknown) => {
      const detail =
        (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Не удалось провести выплату";
      toast.error(detail);
    },
  });

  const transferMutation = useMutation({
    mutationFn: () =>
      transferPayrollReserve(reserveId as string, {
        selected_ids: Array.from(selected),
        plan_version: plan!.version,
        operation_date: todayIso(),
      }),
    onSuccess: async (res) => {
      await refreshAfterPay();
      const destination = res.destination_location === "safe" ? "Сейф" : "кассу";
      toast.success(
        `Передано ${money.format(res.amount)} в ${destination} с резервом для ${res.allocations.length} чел.`,
      );
      onOpenChange(false);
    },
    onError: (error: unknown) => {
      const detail =
        (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Не удалось передать резерв";
      toast.error(detail);
    },
  });

  const cancelMutation = useMutation({
    mutationFn: () => cancelPayrollReserve(reserveId as string),
    onSuccess: async (res) => {
      setCancelConfirmOpen(false);
      await refreshAfterPay();
      toast.success(`Резерв отменён · освобождено ${money.format(res.released)}`);
      onOpenChange(false);
    },
    onError: (error: unknown) => {
      const detail =
        (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Не удалось отменить резерв";
      toast.error(detail);
    },
  });

  const editPlanMutation = useMutation({
    mutationFn: (vars: {
      employeeId: string;
      amount: number;
      destination: "safe" | "kassa" | null;
    }) =>
      editPayrollReservePlan(reserveId as string, {
        employee_id: vars.employeeId,
        amount: vars.amount,
        expected_version: plan!.version,
        remainder_destination: vars.destination,
        operation_date: todayIso(),
      }),
    onSuccess: async (res) => {
      queryClient.setQueryData(["payroll-reserve-plan", reserveId], res);
      setEditingId(null);
      setRemainderEdit(null);
      await refreshAfterPay();
      toast.success(
        res.transferred > 0
          ? `План сохранён · ${money.format(res.transferred)} перенесены ${isKassa ? "на Сейф" : "в кассу"}. Зарплата не выплачена`
          : "План сохранён. Зарплата не выплачена",
      );
      if (res.outstanding < 0.01) onOpenChange(false);
    },
    onError: (error: unknown) => {
      const detail =
        (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Не удалось сохранить план";
      toast.error(detail);
      setRemainderEdit(null);
      setEditingId(null);
      void planQuery.refetch();
    },
  });

  const busy =
    payMutation.isPending ||
    editPlanMutation.isPending ||
    transferMutation.isPending ||
    cancelMutation.isPending;
  const unavailable =
    loading || !plan || planQuery.isError || linesQuery.isError || employeesQuery.isError;

  function toggle(employeeId: string) {
    setSelected((cur) => {
      const next = new Set(cur);
      if (next.has(employeeId)) {
        next.delete(employeeId);
      } else {
        next.add(employeeId);
      }
      return next;
    });
  }

  function startEdit(r: RegisterRow) {
    setEditingId(r.employeeId);
    setEditValue(String(planned.get(r.employeeId)?.amount ?? 0));
  }

  function submitEdit(r: RegisterRow) {
    const value = Number(editValue.replace(",", ".").replace(/\s/g, ""));
    const other = (plan?.allocations ?? [])
      .filter((i) => i.employee_id !== r.employeeId)
      .reduce((sum, i) => sum + i.amount + i.deferred, 0);
    const cap = Math.min(r.remaining, poolLeft - other);
    if (
      !editValue.trim() ||
      !Number.isFinite(value) ||
      value < 0 ||
      Math.abs(value * 100 - Math.round(value * 100)) > 0.00001
    ) {
      toast.error("Введите сумму от нуля, не более двух знаков после запятой");
      return;
    }
    if (value > cap + 0.001) {
      toast.error(`Максимум ${money.format(cap)} (остаток сотрудника / резерва)`);
      return;
    }
    const remainder = Math.round(((planned.get(r.employeeId)?.amount ?? 0) - value) * 100) / 100;
    if (remainder > 0.001) {
      setRemainderEdit({ employeeId: r.employeeId, name: r.name, amount: value, remainder });
    } else {
      editPlanMutation.mutate({ employeeId: r.employeeId, amount: value, destination: null });
    }
  }

  return (
    <>
      <Dialog open={Boolean(row)} onOpenChange={(next) => !next && !busy && onOpenChange(false)}>
        <DialogContent className="flex max-h-[86vh] max-w-xl flex-col gap-0 overflow-hidden p-0">
          <DialogHeader className="shrink-0 space-y-0 border-b px-6 py-4">
            <DialogTitle className="text-lg">Выплата ЗП из {channel}</DialogTitle>
            <DialogDescription className="mt-0.5">
              В резерве {money.format(poolLeft)}. Карандаш меняет только план. Деньги выдаются после
              отдельного подтверждения «Выплатить».
            </DialogDescription>
          </DialogHeader>

          <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-6 py-4">
            {solvency && !solvency.solvent ? (
              <div className="flex items-start gap-2 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-800">
                <AlertTriangle size={16} className="mt-0.5 shrink-0" />
                <span>
                  Не хватает {money.format(solvency.shortfall)}. Пополните счёт, либо выберите, кому
                  не доплатить — сформируется долг.{" "}
                  <span className="opacity-70">Банк/овердрафт — по последней выписке.</span>
                </span>
              </div>
            ) : null}

            {planQuery.isError || linesQuery.isError || employeesQuery.isError ? (
              <div role="alert" className="py-6 text-sm text-destructive">
                Не удалось загрузить актуальный план. Закройте окно и откройте снова.
              </div>
            ) : loading ? (
              <div className="flex items-center justify-center py-10 text-muted-foreground">
                <Loader2 className="animate-spin" size={18} />
              </div>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b text-xs text-muted-foreground">
                    <th className="w-8 py-2" />
                    <th className="py-2 text-left font-medium">Сотрудник</th>
                    <th className="py-2 text-right font-medium">Остаток</th>
                    <th className="py-2 text-right font-medium">К выплате здесь</th>
                  </tr>
                </thead>
                <tbody>
                  {payable.map((r) => {
                    const take = preview.get(r.employeeId) ?? 0;
                    const isSelected = selected.has(r.employeeId);
                    const partial = take > 0.001 && take < r.remaining - 0.001;
                    const editing = editingId === r.employeeId;
                    const deferred = planned.get(r.employeeId)?.deferred ?? 0;
                    return (
                      <tr key={r.employeeId} className="border-b last:border-0">
                        <td className="py-2">
                          <input
                            type="checkbox"
                            checked={isSelected}
                            disabled={busy || editing}
                            onChange={() => toggle(r.employeeId)}
                          />
                        </td>
                        <td className="py-2">{r.name}</td>
                        <td className="py-2 text-right tabular-nums">
                          {money.format(r.remaining)}
                        </td>
                        <td className="py-2">
                          {editing ? (
                            <div className="flex items-center justify-end gap-1">
                              <Input
                                autoFocus
                                className="h-8 w-24 text-right"
                                inputMode="decimal"
                                value={editValue}
                                onChange={(e) => setEditValue(e.target.value)}
                                onKeyDown={(e) => {
                                  if (e.key === "Enter") {
                                    e.preventDefault();
                                    if (!busy) submitEdit(r);
                                  }
                                  if (e.key === "Escape") setEditingId(null);
                                }}
                              />
                              <Button
                                aria-label="Сохранить сумму"
                                size="icon"
                                variant="outline"
                                className="h-8 w-8"
                                disabled={busy}
                                onClick={() => submitEdit(r)}
                              >
                                {editPlanMutation.isPending ? (
                                  <Loader2 className="animate-spin" size={14} />
                                ) : (
                                  <Check size={14} />
                                )}
                              </Button>
                              <Button
                                aria-label="Отмена"
                                size="icon"
                                variant="ghost"
                                className="h-8 w-8"
                                disabled={busy}
                                onClick={() => setEditingId(null)}
                              >
                                <X size={14} />
                              </Button>
                            </div>
                          ) : (
                            <div className="flex items-center justify-end gap-2">
                              <span
                                className={cn(
                                  "tabular-nums",
                                  !isSelected && "text-muted-foreground",
                                  partial ? "text-amber-700" : isSelected ? "text-emerald-700" : "",
                                )}
                              >
                                {isSelected ? money.format(take) : "—"}
                                {partial ? " ⚠" : ""}
                              </span>
                              <Button
                                aria-label="Изменить сумму"
                                size="icon"
                                variant="ghost"
                                className="h-8 w-8 text-muted-foreground"
                                disabled={busy || unavailable || poolLeft < 0.01}
                                onClick={() => startEdit(r)}
                              >
                                <Pencil size={14} />
                              </Button>
                            </div>
                          )}
                          {deferred > 0.001 ? (
                            <div className="mt-1 text-right text-xs text-amber-700">
                              Ещё {money.format(deferred)} оставлены здесь в резерве
                            </div>
                          ) : null}
                          {(planned.get(r.employeeId)?.other_amount ?? 0) > 0.001 ? (
                            <div className="mt-1 text-right text-xs text-muted-foreground">
                              {money.format(planned.get(r.employeeId)!.other_amount)} к выплате{" "}
                              {plan?.other_location === "safe" ? "на Сейфе" : "в кассе"}
                            </div>
                          ) : null}
                        </td>
                      </tr>
                    );
                  })}
                  {payable.length === 0 ? (
                    <tr>
                      <td colSpan={4} className="py-6 text-center text-muted-foreground">
                        Все сотрудники ведомости уже выплачены
                      </td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            )}
          </div>

          <DialogFooter className="shrink-0 flex-col items-stretch gap-3 border-t px-6 py-3 sm:flex-col sm:items-stretch">
            <div className="text-xs text-muted-foreground">
              Покроет {money.format(covered)} из {money.format(selectedRemaining)}
              {uncoveredHere > 0.001 ? ` · ${money.format(uncoveredHere)} останется к выплате` : ""}
            </div>
            <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
              <Button
                variant="outline"
                className="text-destructive hover:text-destructive"
                disabled={busy || unavailable || Boolean(editingId) || poolLeft < 0.01}
                onClick={() => setCancelConfirmOpen(true)}
              >
                Отменить
              </Button>
              <Button
                variant="outline"
                disabled={
                  busy || unavailable || Boolean(editingId) || selected.size === 0 || covered < 0.01
                }
                onClick={() => transferMutation.mutate()}
              >
                {transferMutation.isPending ? (
                  <Loader2 className="animate-spin" size={16} />
                ) : (
                  <>
                    <ArrowRightLeft size={16} />
                    {isKassa ? "Передать на Сейф" : "Передать в кассу"}
                  </>
                )}
              </Button>
              <Button
                disabled={
                  busy || unavailable || Boolean(editingId) || selected.size === 0 || covered < 0.01
                }
                onClick={() => setPayConfirmOpen(true)}
              >
                {payMutation.isPending ? (
                  <Loader2 className="animate-spin" size={16} />
                ) : (
                  "Выплатить"
                )}
              </Button>
            </div>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={Boolean(remainderEdit)}
        onOpenChange={(open) => !open && !busy && setRemainderEdit(null)}
      >
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Откуда выплатить остаток?</DialogTitle>
            <DialogDescription>
              {remainderEdit?.name}: {money.format(remainderEdit?.amount ?? 0)} к выплате из{" "}
              {channel}. Остаток {money.format(remainderEdit?.remainder ?? 0)} остаётся долгом
              сотруднику. Ничего сейчас не выплачивается.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="flex-col gap-2 sm:flex-col">
            <Button
              variant="outline"
              disabled={busy}
              onClick={() =>
                remainderEdit &&
                editPlanMutation.mutate({
                  employeeId: remainderEdit.employeeId,
                  amount: remainderEdit.amount,
                  destination: null,
                })
              }
            >
              Оставить на этом счёте
            </Button>
            <Button
              variant="outline"
              disabled={busy}
              onClick={() =>
                remainderEdit &&
                editPlanMutation.mutate({
                  employeeId: remainderEdit.employeeId,
                  amount: remainderEdit.amount,
                  destination: isKassa ? "safe" : "kassa",
                })
              }
            >
              Перенести {money.format(remainderEdit?.remainder ?? 0)}{" "}
              {isKassa ? "на Сейф" : "в кассу"}
            </Button>
            <p className="text-xs text-muted-foreground">
              Перенос переместит только этот остаток вместе с резервом между счетами. Это не выплата
              зарплаты.
            </p>
            <Button variant="ghost" disabled={busy} onClick={() => setRemainderEdit(null)}>
              Назад к сумме
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog open={payConfirmOpen} onOpenChange={(open) => !busy && setPayConfirmOpen(open)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Подтвердить выдачу зарплаты?</AlertDialogTitle>
            <AlertDialogDescription>
              Будет выплачено {money.format(covered)} из {channel} по сохранённым суммам.
              Оставленные в резерве суммы не выплачиваются.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>Назад</AlertDialogCancel>
            <AlertDialogAction
              disabled={busy || unavailable || covered < 0.01}
              onClick={(event) => {
                event.preventDefault();
                payMutation.mutate();
              }}
            >
              {payMutation.isPending ? "Проводим выплату…" : "Подтвердить выплату"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={cancelConfirmOpen} onOpenChange={setCancelConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Отменить резерв?</AlertDialogTitle>
            <AlertDialogDescription>
              {money.format(poolLeft)} станут свободными на {isKassa ? "кассе" : "Сейфе"}. Уже
              выплаченные суммы и движения денег не изменятся.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={cancelMutation.isPending}>Назад</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              disabled={cancelMutation.isPending}
              onClick={(event) => {
                event.preventDefault();
                cancelMutation.mutate();
              }}
            >
              {cancelMutation.isPending ? (
                <Loader2 className="animate-spin" size={16} />
              ) : (
                "Отменить резерв"
              )}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
