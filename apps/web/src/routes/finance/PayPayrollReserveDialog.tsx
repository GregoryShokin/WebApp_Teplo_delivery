import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, Loader2, Pencil, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
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
  getEmployees,
  getPayrollRunLines,
  getRunSolvency,
  payEmployeeFromReserve,
  payRunFromPool,
  type PayrollLine,
} from "@/lib/api";
import { cn } from "@/lib/utils";

import type { PaymentRow } from "./payments-api";

const money = new Intl.NumberFormat("ru-RU", {
  style: "currency",
  currency: "RUB",
  maximumFractionDigits: 0,
});

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

type RegisterRow = {
  employeeId: string;
  name: string;
  accrued: number;
  paid: number;
  remaining: number;
  status: string;
  depositScheduled: number;
};

// Клиентское превью раскладки пула — зеркалит backend allocate_pool: меньшие первыми, ручной
// граничный уходит в конец (получает остаток пула). Тай-брейк по код-поинтам (как str(uuid)).
function previewAllocate(
  pool: number,
  rows: RegisterRow[],
  selected: Set<string>,
  boundaryId: string | null,
): Map<string, number> {
  let cand = rows.filter((r) => r.remaining > 0.001 && selected.has(r.employeeId));
  cand = [...cand].sort(
    (a, b) => a.remaining - b.remaining || (a.employeeId < b.employeeId ? -1 : 1),
  );
  if (boundaryId && cand.some((r) => r.employeeId === boundaryId)) {
    cand = [
      ...cand.filter((r) => r.employeeId !== boundaryId),
      ...cand.filter((r) => r.employeeId === boundaryId),
    ];
  }
  const result = new Map<string, number>();
  let left = pool;
  for (const r of cand) {
    if (left <= 0.001) break;
    const take = Math.min(r.remaining, left);
    if (take > 0.001) {
      result.set(r.employeeId, Math.round(take * 100) / 100);
      left -= take;
    }
  }
  return result;
}

// Окно выплаты ЗП из пула-резерва (Сейф/касса), привязанного к ведомости. Авто-раскладка «меньшие
// первыми» с выбором сотрудников (чекбоксы) и граничным (сплит) + перетоком на второй пул («Выплатить
// из Сейфа/кассы»); ПЛЮС карандаш в колонке «Получит» для ручной правки суммы конкретному сотруднику
// (остаток резерва лежит earmark'ом для следующей выплаты).
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
  const [boundaryId, setBoundaryId] = useState<string | null>(null);
  // Остаток пула держим локально: ручная выплата карандашом обновляет его, не закрывая окно
  // (row-проп от родителя отстаёт до рефетча).
  const [poolLeft, setPoolLeft] = useState(0);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");

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
      setBoundaryId(null);
      setPoolLeft(row.amount - (row.amount_paid ?? 0));
      setEditingId(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [row?.id, payable.length]);

  const preview = useMemo(
    () => previewAllocate(poolLeft, rows, selected, boundaryId),
    [poolLeft, rows, selected, boundaryId],
  );
  const covered = Array.from(preview.values()).reduce((a, b) => a + b, 0);
  const selectedRemaining = payable
    .filter((r) => selected.has(r.employeeId))
    .reduce((a, r) => a + r.remaining, 0);
  const uncoveredHere = Math.max(0, Math.round((selectedRemaining - covered) * 100) / 100);

  const solvency = solvencyQuery.data;
  const loading = linesQuery.isLoading || employeesQuery.isLoading;

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
    ]);
    await onPaid();
  }

  // Авто-раскладка пула по выбранным (+граничный, +переток на второй пул).
  const payMutation = useMutation({
    mutationFn: () =>
      payRunFromPool(reserveId as string, {
        selected_ids: Array.from(selected),
        boundary_id: boundaryId,
        allow_overflow: true,
        paid_at: todayIso(),
      }),
    onSuccess: async (res) => {
      await refreshAfterPay();
      const overflow =
        res.overflow_booked > 0 ? ` (+${money.format(res.overflow_booked)} перетоком)` : "";
      toast.success(
        `Выплачено ${money.format(res.primary_booked)} из ${channel}${overflow} — ${res.employees_paid} чел.`,
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

  // Ручная выплата карандашом одному сотруднику; остаток резерва остаётся.
  const payEmployeeMutation = useMutation({
    mutationFn: (vars: { employeeId: string; amount: number }) =>
      payEmployeeFromReserve(reserveId as string, {
        employee_id: vars.employeeId,
        amount: vars.amount,
        paid_at: todayIso(),
      }),
    onSuccess: async (res) => {
      setPoolLeft(res.reserve_outstanding);
      setEditingId(null);
      await refreshAfterPay();
      toast.success(`Выплачено ${money.format(res.booked)} из ${channel}`);
      if (res.reserve_status === "paid") {
        toast.info("Резерв исчерпан");
        onOpenChange(false);
      }
    },
    onError: (error: unknown) => {
      const detail =
        (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Не удалось провести выплату";
      toast.error(detail);
    },
  });

  const busy = payMutation.isPending || payEmployeeMutation.isPending;

  function toggle(employeeId: string) {
    setSelected((cur) => {
      const next = new Set(cur);
      if (next.has(employeeId)) {
        next.delete(employeeId);
        if (boundaryId === employeeId) setBoundaryId(null);
      } else {
        next.add(employeeId);
      }
      return next;
    });
  }

  function startEdit(r: RegisterRow) {
    setEditingId(r.employeeId);
    const cap = Math.min(r.remaining, poolLeft);
    // Дефолт — то, что дала бы авто-раскладка (или максимум), можно поправить.
    setEditValue(String(preview.get(r.employeeId) ?? Math.round(cap * 100) / 100));
  }

  function submitEdit(r: RegisterRow) {
    const value = Number(editValue.replace(",", ".").replace(/\s/g, ""));
    const cap = Math.min(r.remaining, poolLeft);
    if (!Number.isFinite(value) || value <= 0) {
      toast.error("Введите сумму больше нуля");
      return;
    }
    if (value > cap + 0.01) {
      toast.error(`Максимум ${money.format(cap)} (остаток сотрудника / резерва)`);
      return;
    }
    payEmployeeMutation.mutate({ employeeId: r.employeeId, amount: value });
  }

  return (
    <Dialog open={Boolean(row)} onOpenChange={(next) => !next && onOpenChange(false)}>
      <DialogContent className="flex max-h-[86vh] max-w-xl flex-col gap-0 overflow-hidden p-0">
        <DialogHeader className="shrink-0 space-y-0 border-b px-6 py-4">
          <DialogTitle className="text-lg">Выплата ЗП из {channel}</DialogTitle>
          <DialogDescription className="mt-0.5">
            В резерве {money.format(poolLeft)} · авто-раскладка «меньшие первыми» или правь сумму
            по каждому карандашом; остаток лежит резервом для следующей выплаты
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

          {loading ? (
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
                  <th className="py-2 text-right font-medium">Получит</th>
                  <th className="w-16 py-2 text-center font-medium">Гранич.</th>
                </tr>
              </thead>
              <tbody>
                {payable.map((r) => {
                  const take = preview.get(r.employeeId) ?? 0;
                  const isSelected = selected.has(r.employeeId);
                  const partial = take > 0.001 && take < r.remaining - 0.001;
                  const editing = editingId === r.employeeId;
                  const paying =
                    payEmployeeMutation.isPending &&
                    payEmployeeMutation.variables?.employeeId === r.employeeId;
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
                      <td className="py-2 text-right tabular-nums">{money.format(r.remaining)}</td>
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
                                if (e.key === "Enter") submitEdit(r);
                                if (e.key === "Escape") setEditingId(null);
                              }}
                            />
                            <Button
                              aria-label="Выплатить"
                              size="icon"
                              variant="outline"
                              className="h-8 w-8"
                              disabled={busy}
                              onClick={() => submitEdit(r)}
                            >
                              {paying ? (
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
                              disabled={busy || poolLeft < 0.01}
                              onClick={() => startEdit(r)}
                            >
                              <Pencil size={14} />
                            </Button>
                          </div>
                        )}
                      </td>
                      <td className="py-2 text-center">
                        <input
                          type="radio"
                          name="boundary"
                          disabled={!isSelected || busy || editing}
                          checked={boundaryId === r.employeeId}
                          onChange={() => setBoundaryId(r.employeeId)}
                        />
                      </td>
                    </tr>
                  );
                })}
                {payable.length === 0 ? (
                  <tr>
                    <td colSpan={5} className="py-6 text-center text-muted-foreground">
                      Все сотрудники ведомости уже выплачены
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          )}

          {boundaryId ? (
            <button
              type="button"
              className="text-xs text-muted-foreground underline"
              onClick={() => setBoundaryId(null)}
            >
              Сбросить граничного (авто)
            </button>
          ) : null}
        </div>

        <DialogFooter className="shrink-0 flex-col items-stretch gap-2 border-t px-6 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="text-xs text-muted-foreground">
            Покроет {money.format(covered)} из {money.format(selectedRemaining)}
            {uncoveredHere > 0.001 ? ` · ${money.format(uncoveredHere)} — переток/долг` : ""}
          </div>
          <Button
            disabled={busy || selected.size === 0 || poolLeft < 0.01}
            onClick={() => payMutation.mutate()}
          >
            {payMutation.isPending ? (
              <Loader2 className="animate-spin" size={16} />
            ) : (
              `Выплатить из ${isKassa ? "кассы" : "Сейфа"}`
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
