import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, HandCoins, LoaderCircle, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { apiErrorMessage } from "@/lib/api";
import { formatRub } from "@/routes/counterparties/shared";
import {
  cancelKassaAdvancePermission,
  disburseKassaAdvancePermission,
  getKassaFreelancerShifts,
  getKassaPending,
  payKassaFreelancerShifts,
  payKassaPayrollTarget,
  payKassaTarget,
  syncKassaFreelancerShifts,
  type KassaAdvancePermission,
  type KassaFreelancer,
  type KassaTarget,
} from "@/routes/kassa/api";

const KIND_LABEL: Record<KassaAdvancePermission["kind"], string> = {
  advance: "Аванс",
  loan: "Заём",
};
const AUTO_BOUNDARY_VALUE = "auto";

// То же превью, что в окне «Активные платежи»: меньшие остатки первыми, выбранный
// граничный — последним и получает остаток пула (возможно частично).
function previewPayrollAllocation(
  pool: number,
  employees: KassaTarget["payroll_employees"],
  selected: Set<string>,
  boundaryId: string | null,
): Map<string, number> {
  let candidates = employees
    .filter(
      (employee) =>
        employee.payable && employee.remaining > 0.005 && selected.has(employee.employee_id),
    )
    .sort(
      (left, right) =>
        left.remaining - right.remaining || left.employee_id.localeCompare(right.employee_id),
    );
  if (boundaryId && candidates.some((employee) => employee.employee_id === boundaryId)) {
    candidates = [
      ...candidates.filter((employee) => employee.employee_id !== boundaryId),
      ...candidates.filter((employee) => employee.employee_id === boundaryId),
    ];
  }
  const result = new Map<string, number>();
  let left = pool;
  for (const employee of candidates) {
    if (left <= 0.005) break;
    const amount = Math.min(employee.remaining, left);
    result.set(employee.employee_id, Math.round(amount * 100) / 100);
    left -= amount;
  }
  return result;
}

/**
 * Вкладка «К выдаче»: целёвки, переданные в кассу (частичная выдача допустима),
 * и разрешения на авансы/займы (только вся сумма). Выдаёт администратор кассы.
 */
export function KassaPendingTab() {
  const queryClient = useQueryClient();
  const pendingQuery = useQuery({ queryKey: ["kassa", "pending"], queryFn: getKassaPending });
  const [activeFreelancer, setActiveFreelancer] = useState<KassaFreelancer | null>(null);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["kassa"] });
    void queryClient.invalidateQueries({ queryKey: ["dds"] });
  };

  const payTargetMutation = useMutation({
    mutationFn: ({ id, amount }: { id: string; amount: number }) => payKassaTarget(id, amount),
    onSuccess: () => {
      toast.success("Выдано — запись в кассовом журнале");
      invalidate();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось выдать")),
  });

  const payPayrollMutation = useMutation({
    mutationFn: ({
      id,
      employeeIds,
      boundaryId,
    }: {
      id: string;
      employeeIds: string[];
      boundaryId: string | null;
    }) => payKassaPayrollTarget(id, employeeIds, boundaryId),
    onSuccess: (_pending, variables) => {
      toast.success(`Зарплата выдана: ${variables.employeeIds.length} сотрудникам`);
      invalidate();
      void queryClient.invalidateQueries({ queryKey: ["payroll-runs"] });
      void queryClient.invalidateQueries({ queryKey: ["payroll-run"] });
      void queryClient.invalidateQueries({ queryKey: ["payroll-run-lines"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось провести выдачу зарплаты")),
  });

  const disburseMutation = useMutation({
    mutationFn: (id: string) => disburseKassaAdvancePermission(id),
    onSuccess: () => {
      toast.success("Выплачено — удержание пойдёт с даты выдачи");
      invalidate();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось выплатить")),
  });

  const cancelMutation = useMutation({
    mutationFn: (id: string) => cancelKassaAdvancePermission(id),
    onSuccess: () => {
      toast.success("Разрешение отменено — создатель увидит отметку");
      invalidate();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось отменить")),
  });

  const syncShiftsMutation = useMutation({
    mutationFn: () => syncKassaFreelancerShifts(),
    onSuccess: (report) => {
      // Считаем только тех, кому реально есть что выдать: человек с одной ещё идущей
      // сменой в списке появится, но звать за деньгами по нему рано.
      const count = report.freelancers.filter((item) => item.unpaid_total > 0).length;
      toast.success(
        count ? `Смены синхронизированы: внештатников к выдаче ${count}` : "Смены синхронизированы",
      );
      invalidate();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось синхронизировать смены")),
  });

  const payShiftsMutation = useMutation({
    mutationFn: (ids: string[]) => payKassaFreelancerShifts(ids),
    onSuccess: () => {
      toast.success("Выплачено — записи в кассовом журнале");
      setActiveFreelancer(null);
      invalidate();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось выдать")),
  });

  const pending = pendingQuery.data;
  const busy =
    payTargetMutation.isPending ||
    payPayrollMutation.isPending ||
    disburseMutation.isPending ||
    cancelMutation.isPending ||
    syncShiftsMutation.isPending ||
    payShiftsMutation.isPending;
  const isEmpty =
    !!pending &&
    pending.targets.length === 0 &&
    pending.permissions.length === 0 &&
    pending.freelancers.length === 0;

  if (pendingQuery.isLoading) {
    return <div className="h-24 animate-pulse rounded-lg bg-muted/60" />;
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-end">
        <Button
          size="sm"
          variant="outline"
          disabled={busy}
          onClick={() => syncShiftsMutation.mutate()}
          title="Перечитать смены внештатников из iiko — закрывшиеся станут доступны к выдаче"
        >
          {syncShiftsMutation.isPending ? (
            <LoaderCircle size={14} className="animate-spin" aria-hidden="true" />
          ) : (
            <RefreshCw size={14} aria-hidden="true" />
          )}
          Синхронизировать смены
        </Button>
      </div>

      <Card>
        <CardContent className="pt-5">
          <div className="text-sm text-muted-foreground">Наличные администраторов</div>
          <div className="mt-1 text-2xl font-semibold tabular-nums">
            {pending ? formatRub(pending.balance) : "—"}
          </div>
          {pending ? (
            <div className="mt-0.5 text-xs text-muted-foreground">
              в кассе {formatRub(pending.balance)} · из них целевые{" "}
              <span className="font-medium text-amber-600">{formatRub(pending.targets_total)}</span>
            </div>
          ) : null}
        </CardContent>
      </Card>

      {isEmpty ? (
        <div className="rounded-lg border border-dashed p-6 text-sm text-muted-foreground">
          К выдаче ничего нет: целёвки передаются из Сейфа («Передать в кассу»), разрешения на
          авансы и займы приходят со страницы «Авансы и займы», а смены внештатников подтягиваются
          кнопкой «Синхронизировать смены».
        </div>
      ) : null}

      {pending && pending.targets.length > 0 ? (
        <div className="grid gap-2">
          <Label className="text-base font-semibold">Целевые выплаты</Label>
          {pending.targets.map((target) => (
            <TargetCard
              key={target.id}
              target={target}
              balance={pending.balance}
              busy={busy}
              onPay={(amount) => payTargetMutation.mutate({ id: target.id, amount })}
              onPayPayroll={(employeeIds, boundaryId) =>
                payPayrollMutation
                  .mutateAsync({ id: target.id, employeeIds, boundaryId })
                  .then(() => undefined)
              }
            />
          ))}
        </div>
      ) : null}

      {pending && pending.permissions.length > 0 ? (
        <div className="grid gap-2">
          <Label className="text-base font-semibold">Разрешения на авансы и займы</Label>
          {pending.permissions.map((permission) => (
            <div key={permission.id} className="rounded-md border p-3 text-sm">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="font-medium">{permission.employee_name}</span>
                    <Badge variant={permission.kind === "loan" ? "destructive" : "secondary"}>
                      {KIND_LABEL[permission.kind]}
                    </Badge>
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {[
                      permission.created_by_label
                        ? `оформил(а) ${permission.created_by_label}`
                        : null,
                      permission.comment,
                    ]
                      .filter(Boolean)
                      .join(" · ") || "Выдаётся только вся сумма"}
                  </div>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    size="sm"
                    disabled={busy}
                    onClick={() => disburseMutation.mutate(permission.id)}
                  >
                    <HandCoins size={14} aria-hidden="true" />
                    Выплачено {formatRub(permission.amount)}
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={busy}
                    onClick={() => cancelMutation.mutate(permission.id)}
                  >
                    Отменить
                  </Button>
                </div>
              </div>
            </div>
          ))}
        </div>
      ) : null}

      {pending && pending.freelancers.length > 0 ? (
        <FreelancersSection
          freelancers={pending.freelancers}
          busy={busy}
          onOpen={setActiveFreelancer}
        />
      ) : null}

      <FreelancerShiftsDialog
        freelancer={activeFreelancer}
        balance={pending?.balance ?? 0}
        paying={payShiftsMutation.isPending}
        onClose={() => setActiveFreelancer(null)}
        onPay={(ids) => payShiftsMutation.mutate(ids)}
      />
    </div>
  );
}

const shiftDayFormatter = new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "2-digit" });

function formatShiftDay(iso: string): string {
  const parsed = new Date(`${iso}T00:00:00`);
  return Number.isNaN(parsed.getTime()) ? iso : shiftDayFormatter.format(parsed);
}

/**
 * Выплаты за смены внештатников: ОДНА строка на человека (имя + «к выдаче N ₽» = сумма его
 * неоплаченных смен). Клик открывает модалку со сменами построчно.
 */
/** Русское склонение по числу: 1 смена, 2 смены, 5 смен. */
function pluralShifts(count: number): string {
  const tail = count % 10;
  const hundred = count % 100;
  if (tail === 1 && hundred !== 11) return `${count} смена`;
  if (tail >= 2 && tail <= 4 && (hundred < 12 || hundred > 14)) return `${count} смены`;
  return `${count} смен`;
}

/** «1 смена идёт» / «2 смены идут» — счётчик ещё не закрытых смен. */
function openShiftsHint(count: number): string {
  return `${pluralShifts(count)} ${count % 10 === 1 && count % 100 !== 11 ? "идёт" : "идут"}`;
}

function FreelancersSection({
  freelancers,
  busy,
  onOpen,
}: {
  freelancers: KassaFreelancer[];
  busy: boolean;
  onOpen: (freelancer: KassaFreelancer) => void;
}) {
  return (
    <div className="grid gap-2">
      <Label className="text-base font-semibold">Выплаты за смены внештатников</Label>
      {freelancers.map((freelancer) => (
        <button
          key={freelancer.employee_id}
          type="button"
          disabled={busy}
          onClick={() => onOpen(freelancer)}
          className="flex items-center justify-between gap-2 rounded-md border p-3 text-left text-sm hover:bg-muted/40 disabled:cursor-not-allowed disabled:opacity-60"
        >
          <div className="min-w-0">
            <div className="font-medium">{freelancer.name}</div>
            <div className="text-xs text-muted-foreground">
              {freelancer.shift_count > 0
                ? `${pluralShifts(freelancer.shift_count)} · нажмите, чтобы выбрать`
                : "нажмите, чтобы посмотреть"}
              {freelancer.open_count > 0 ? ` · ${openShiftsHint(freelancer.open_count)}` : ""}
            </div>
          </div>
          <div className="flex items-center gap-1.5">
            <div className="text-right font-medium tabular-nums">
              к выдаче {formatRub(freelancer.unpaid_total)}
            </div>
            <ChevronRight size={16} className="text-muted-foreground" aria-hidden="true" />
          </div>
        </button>
      ))}
    </div>
  );
}

/**
 * Модалка смен внештатника: смены построчно (дата · часы · сумма) чекбоксами. Неоплаченные
 * ЗАКРЫТЫЕ смены по умолчанию отмечены; оплаченные — как выданные/недоступны; ещё идущие —
 * серые и неотмечаемые (за них платить нечего, пока iiko не отдал время закрытия: часы у
 * них расчётные, и выдача ушла бы по полной ставке за неотработанное). Внизу итог по
 * отмеченным и «Выплатить N ₽» — каждая выбранная смена выдаётся целиком (сумму и статью
 * пишет движок).
 */
function FreelancerShiftsDialog({
  freelancer,
  balance,
  paying,
  onClose,
  onPay,
}: {
  freelancer: KassaFreelancer | null;
  balance: number;
  paying: boolean;
  onClose: () => void;
  onPay: (attendanceEntryIds: string[]) => void;
}) {
  const employeeId = freelancer?.employee_id ?? null;
  const shiftsQuery = useQuery({
    queryKey: ["kassa", "freelancer-shifts", employeeId],
    queryFn: () => getKassaFreelancerShifts(employeeId as string),
    enabled: !!employeeId,
  });
  // Выбор ведём по attendance_entry_id; при первой загрузке неоплаченные отмечены.
  const [selected, setSelected] = useState<Set<string> | null>(null);
  const shifts = shiftsQuery.data;

  const effectiveSelected = useMemo(() => {
    if (selected) return selected;
    if (!shifts) return new Set<string>();
    return new Set(
      shifts
        .filter((shift) => !shift.paid && !shift.is_open)
        .map((shift) => shift.attendance_entry_id),
    );
  }, [selected, shifts]);

  const chosen = (shifts ?? []).filter(
    (shift) => !shift.paid && !shift.is_open && effectiveSelected.has(shift.attendance_entry_id),
  );
  const selectedTotal = chosen.reduce((sum, shift) => sum + shift.amount, 0);
  const overBalance = selectedTotal > 0 && selectedTotal > balance;

  const toggle = (id: string) => {
    setSelected((prev) => {
      const base = prev ?? effectiveSelected;
      const next = new Set(base);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <Dialog
      open={!!freelancer}
      onOpenChange={(open) => {
        if (!open) {
          setSelected(null);
          onClose();
        }
      }}
    >
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{freelancer?.name}</DialogTitle>
          <DialogDescription>
            Отметьте смены — каждая выдаётся целиком. Статью и сумму пишет движок.
          </DialogDescription>
        </DialogHeader>

        {shiftsQuery.isLoading ? (
          <div className="h-24 animate-pulse rounded-lg bg-muted/60" />
        ) : shifts && shifts.length > 0 ? (
          <div className="grid max-h-[50vh] gap-2 overflow-y-auto">
            {shifts.map((shift) => {
              const locked = shift.paid || shift.is_open;
              const checked = shift.paid || effectiveSelected.has(shift.attendance_entry_id);
              return (
                <label
                  key={shift.attendance_entry_id}
                  className={
                    locked
                      ? "flex items-center justify-between gap-2 rounded-md border border-dashed p-3 text-sm opacity-60"
                      : "flex cursor-pointer items-center justify-between gap-2 rounded-md border p-3 text-sm hover:bg-muted/40"
                  }
                >
                  <div className="flex min-w-0 items-center gap-2.5">
                    <input
                      type="checkbox"
                      className="h-4 w-4 shrink-0"
                      checked={checked}
                      disabled={locked || paying}
                      onChange={() => toggle(shift.attendance_entry_id)}
                    />
                    <div className="min-w-0">
                      <div className="font-medium">смена {formatShiftDay(shift.work_date)}</div>
                      <div className="text-xs text-muted-foreground">
                        {shift.is_open ? (
                          <>смена идёт — выдача после закрытия</>
                        ) : (
                          <>
                            {shift.hours} ч{shift.paid ? " · выдано" : ""}
                          </>
                        )}
                      </div>
                    </div>
                  </div>
                  <div className="text-right font-medium tabular-nums">
                    {shift.is_open ? "—" : formatRub(shift.amount)}
                  </div>
                </label>
              );
            })}
          </div>
        ) : (
          <div className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
            Непогашенных смен нет.
          </div>
        )}

        {overBalance ? (
          <p className="text-xs font-medium text-amber-600">
            Сумма больше учётного остатка кассы ({formatRub(balance)}) — выдача пройдёт, но остаток
            уйдёт в минус. Проверьте суммы.
          </p>
        ) : null}

        <DialogFooter className="flex-row items-center justify-between gap-2 sm:justify-between">
          <div className="text-xs text-muted-foreground">
            {chosen.length > 0 ? `Выбрано ${chosen.length} · ${formatRub(selectedTotal)}` : "—"}
          </div>
          <Button
            size="sm"
            disabled={paying || chosen.length === 0}
            onClick={() => onPay(chosen.map((shift) => shift.attendance_entry_id))}
          >
            {paying ? (
              <LoaderCircle size={14} className="animate-spin" aria-hidden="true" />
            ) : (
              <HandCoins size={14} aria-hidden="true" />
            )}
            Выплатить{chosen.length > 0 ? ` ${formatRub(selectedTotal)}` : ""}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Карточка целёвки: «Выдано» раскрывает поле суммы, предзаполненное остатком. */
function TargetCard({
  target,
  balance,
  busy,
  onPay,
  onPayPayroll,
}: {
  target: KassaTarget;
  balance: number;
  busy: boolean;
  onPay: (amount: number) => void;
  onPayPayroll: (employeeIds: string[], boundaryId: string | null) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [amount, setAmount] = useState("");
  const [selectedEmployees, setSelectedEmployees] = useState<Set<string>>(new Set());
  const [boundaryId, setBoundaryId] = useState<string | null>(null);

  const amountNumber = Number(amount.replace(",", "."));
  const validAmount = Number.isFinite(amountNumber) && amountNumber > 0;
  const overOutstanding = validAmount && amountNumber > target.outstanding + 0.005;
  const overBalance = validAmount && amountNumber > balance;
  const selectedPayrollRows = target.payroll_employees.filter(
    (employee) =>
      employee.payable && employee.remaining > 0.005 && selectedEmployees.has(employee.employee_id),
  );
  const selectedPayrollRemaining = selectedPayrollRows.reduce(
    (total, employee) => total + employee.remaining,
    0,
  );
  const payrollPreview = previewPayrollAllocation(
    target.outstanding,
    target.payroll_employees,
    selectedEmployees,
    boundaryId,
  );
  const selectedPayrollTotal = Array.from(payrollPreview.values()).reduce(
    (total, amount) => total + amount,
    0,
  );
  const payrollUncovered = Math.max(0, selectedPayrollRemaining - selectedPayrollTotal);
  const payrollOverBalance = selectedPayrollTotal > balance + 0.005;

  const toggleEmployee = (employeeId: string) => {
    setSelectedEmployees((current) => {
      const next = new Set(current);
      if (next.has(employeeId)) {
        next.delete(employeeId);
        if (boundaryId === employeeId) setBoundaryId(null);
      } else next.add(employeeId);
      return next;
    });
  };

  const paySelectedEmployees = async () => {
    await onPayPayroll(
      selectedPayrollRows.map((employee) => employee.employee_id),
      boundaryId,
    );
    setSelectedEmployees(new Set());
    setBoundaryId(null);
  };

  return (
    <div className="rounded-md border p-3 text-sm">
      <div
        className={
          target.is_payroll
            ? "flex cursor-pointer flex-wrap items-center justify-between gap-2 rounded-md hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            : "flex flex-wrap items-center justify-between gap-2"
        }
        role={target.is_payroll ? "button" : undefined}
        tabIndex={target.is_payroll ? 0 : undefined}
        onClick={target.is_payroll ? () => setOpen((current) => !current) : undefined}
        onKeyDown={
          target.is_payroll
            ? (event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  setOpen((current) => !current);
                }
              }
            : undefined
        }
      >
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="font-medium">{target.article_name ?? "Без статьи"}</span>
            {target.from_bank_payout ? (
              <Badge
                className="border-amber-200 bg-amber-50 text-amber-700"
                title="Создана автоматически при оплате банковской выплаты на карту ИП"
              >
                из банковской выплаты
              </Badge>
            ) : null}
            {target.is_payroll ? <Badge variant="secondary">зарплатная ведомость</Badge> : null}
          </div>
          {target.counterparty_name ? (
            <div className="text-xs font-medium">{target.counterparty_name}</div>
          ) : null}
          {target.purpose ? (
            <div className="text-xs text-muted-foreground">{target.purpose}</div>
          ) : null}
        </div>
        <div className="flex items-center gap-2">
          <div className="text-right tabular-nums">
            <div className="font-medium">{formatRub(target.outstanding)}</div>
            {target.amount_paid > 0 ? (
              <div className="text-xs text-muted-foreground">
                из {formatRub(target.amount)} · выдано {formatRub(target.amount_paid)}
              </div>
            ) : (
              <div className="text-xs text-muted-foreground">из {formatRub(target.amount)}</div>
            )}
          </div>
          {target.is_payroll ? (
            <ChevronRight
              size={16}
              className={`text-muted-foreground transition-transform ${open ? "rotate-90" : ""}`}
              aria-hidden="true"
            />
          ) : null}
        </div>
      </div>

      {target.is_payroll && open ? (
        <div className="mt-3 grid gap-2 border-t pt-3">
          <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
            <span>Отметьте сотрудников, которым фактически выдали деньги</span>
            <button
              type="button"
              className="font-medium text-foreground hover:underline"
              disabled={busy}
              onClick={() => {
                setBoundaryId(null);
                setSelectedEmployees(
                  new Set(
                    target.payroll_employees
                      .filter((employee) => employee.payable && employee.remaining > 0.005)
                      .map((employee) => employee.employee_id),
                  ),
                );
              }}
            >
              Выбрать всех
            </button>
          </div>

          {target.payroll_employees.length > 0 ? (
            <div className="grid max-h-80 gap-1.5 overflow-y-auto">
              {target.payroll_employees.map((employee) => {
                const paid = employee.remaining <= 0.005;
                const checked = paid || selectedEmployees.has(employee.employee_id);
                const previewAmount = payrollPreview.get(employee.employee_id) ?? 0;
                const partial = previewAmount > 0.005 && previewAmount < employee.remaining - 0.005;
                return (
                  <div
                    key={employee.employee_id}
                    className={`flex items-center justify-between gap-3 rounded-md border px-3 py-2 ${
                      paid || !employee.payable ? "border-dashed opacity-60" : "hover:bg-muted/40"
                    }`}
                  >
                    <div className="flex min-w-0 items-center gap-2.5">
                      <input
                        type="checkbox"
                        className="h-4 w-4 shrink-0"
                        checked={checked}
                        disabled={busy || paid || !employee.payable}
                        onChange={() => toggleEmployee(employee.employee_id)}
                      />
                      <div className="min-w-0">
                        <div className="truncate font-medium">{employee.employee_name}</div>
                        <div className="text-xs text-muted-foreground">
                          {!employee.payable
                            ? "выдача депозита — через ведомость"
                            : paid
                              ? `выдано ${formatRub(employee.paid)}`
                              : employee.paid > 0
                                ? `частично выдано ${formatRub(employee.paid)}`
                                : checked && partial
                                  ? `получит частично · останется ${formatRub(employee.remaining - previewAmount)}`
                                  : checked && previewAmount <= 0.005
                                    ? "не покрывается выбранным резервом"
                                    : "ожидает выдачи"}
                        </div>
                      </div>
                    </div>
                    <div className="shrink-0 text-right font-medium tabular-nums">
                      {paid
                        ? "выдано"
                        : checked
                          ? `получит ${formatRub(previewAmount)}`
                          : formatRub(employee.remaining)}
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="rounded-md border border-dashed p-3 text-xs text-muted-foreground">
              В ведомости нет сотрудников к выдаче.
            </div>
          )}

          {payrollUncovered > 0.005 && selectedPayrollRows.length > 1 ? (
            <div className="grid gap-1.5 rounded-md border bg-muted/30 p-3">
              <Label className="text-xs font-medium">Кому отдать неполный остаток резерва</Label>
              <Select
                value={boundaryId ?? AUTO_BOUNDARY_VALUE}
                disabled={busy}
                onValueChange={(value) =>
                  setBoundaryId(value === AUTO_BOUNDARY_VALUE ? null : value)
                }
              >
                <SelectTrigger className="h-9 bg-background">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={AUTO_BOUNDARY_VALUE}>
                    Автоматически — сотруднику с наибольшим остатком
                  </SelectItem>
                  {selectedPayrollRows.map((employee) => (
                    <SelectItem key={employee.employee_id} value={employee.employee_id}>
                      {employee.employee_name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                Только выбранный здесь сотрудник рассчитывается последним и получает остаток
                резерва. Остальные получают суммы по очереди.
              </p>
            </div>
          ) : null}

          {payrollOverBalance ? (
            <p className="text-xs font-medium text-destructive">
              В кассе недостаточно денег: доступно {formatRub(balance)}. Счёт не может уйти в минус.
            </p>
          ) : payrollUncovered > 0.005 ? (
            <p className="text-xs font-medium text-amber-700">
              Резерв покроет {formatRub(selectedPayrollTotal)}; долг выбранных сотрудников после
              выдачи составит {formatRub(payrollUncovered)}.
            </p>
          ) : null}

          <div className="flex flex-wrap items-center justify-between gap-2 border-t pt-2">
            <div className="text-xs text-muted-foreground">
              {selectedPayrollRows.length > 0
                ? `Выбрано ${selectedPayrollRows.length} · к выдаче ${formatRub(selectedPayrollTotal)}`
                : "Никто не выбран"}
            </div>
            <Button
              size="sm"
              disabled={busy || selectedPayrollRows.length === 0 || payrollOverBalance}
              onClick={() => void paySelectedEmployees()}
            >
              {busy ? (
                <LoaderCircle size={14} className="animate-spin" aria-hidden="true" />
              ) : (
                <HandCoins size={14} aria-hidden="true" />
              )}
              Выплатить{selectedPayrollTotal > 0 ? ` ${formatRub(selectedPayrollTotal)}` : ""}
            </Button>
          </div>
        </div>
      ) : target.is_payroll ? (
        <div className="mt-2 rounded-md bg-muted/60 px-3 py-2 text-xs text-muted-foreground">
          Нажмите на строку, чтобы выбрать сотрудников и отметить фактическую выдачу.
        </div>
      ) : open ? (
        <div className="mt-2 grid gap-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Input
              className="h-9 w-36 text-right tabular-nums"
              inputMode="decimal"
              value={amount}
              onChange={(event) => setAmount(event.target.value)}
            />
            <Button
              size="sm"
              disabled={busy || !validAmount || overOutstanding}
              onClick={() => onPay(amountNumber)}
            >
              Подтвердить выдачу
            </Button>
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => setOpen(false)}>
              Отмена
            </Button>
          </div>
          {overOutstanding ? (
            <p className="text-xs font-medium text-destructive">
              Больше остатка целёвки ({formatRub(target.outstanding)}) выдать нельзя.
            </p>
          ) : overBalance ? (
            <p className="text-xs font-medium text-amber-600">
              Сумма больше учётного остатка кассы ({formatRub(balance)}) — выдача пройдёт, но
              остаток уйдёт в минус. Проверьте сумму.
            </p>
          ) : (
            <p className="text-xs text-muted-foreground">
              Частичная выдача допустима — остаток целёвки останется в списке.
            </p>
          )}
        </div>
      ) : (
        <div className="mt-2">
          <Button
            size="sm"
            disabled={busy}
            onClick={() => {
              setAmount(String(target.outstanding));
              setOpen(true);
            }}
          >
            <HandCoins size={14} aria-hidden="true" />
            Выдано
          </Button>
        </div>
      )}
    </div>
  );
}
