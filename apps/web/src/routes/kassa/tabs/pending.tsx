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
import { apiErrorMessage } from "@/lib/api";
import { formatRub } from "@/routes/counterparties/shared";
import {
  cancelKassaAdvancePermission,
  disburseKassaAdvancePermission,
  getKassaFreelancerShifts,
  getKassaPending,
  payKassaFreelancerShifts,
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
      const count = report.freelancers.length;
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
          title="Подтянуть свежие закрытые смены внештатников из iiko"
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
              <span className="font-medium text-amber-600">
                {formatRub(pending.targets_total)}
              </span>
            </div>
          ) : null}
        </CardContent>
      </Card>

      {isEmpty ? (
        <div className="rounded-lg border border-dashed p-6 text-sm text-muted-foreground">
          К выдаче ничего нет: целёвки передаются из Сейфа («Передать в кассу»), разрешения
          на авансы и займы приходят со страницы «Авансы и займы», а смены внештатников
          подтягиваются кнопкой «Синхронизировать смены».
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
              {freelancer.shift_count} смен · нажмите, чтобы выбрать
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
 * по умолчанию отмечены; оплаченные — как выданные/недоступны. Внизу итог по отмеченным и
 * «Выплатить N ₽» — каждая выбранная смена выдаётся целиком (сумму/статью пишет движок).
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
    return new Set(shifts.filter((shift) => !shift.paid).map((shift) => shift.attendance_entry_id));
  }, [selected, shifts]);

  const chosen = (shifts ?? []).filter(
    (shift) => !shift.paid && effectiveSelected.has(shift.attendance_entry_id),
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
              const checked = shift.paid || effectiveSelected.has(shift.attendance_entry_id);
              return (
                <label
                  key={shift.attendance_entry_id}
                  className={
                    shift.paid
                      ? "flex items-center justify-between gap-2 rounded-md border border-dashed p-3 text-sm opacity-60"
                      : "flex cursor-pointer items-center justify-between gap-2 rounded-md border p-3 text-sm hover:bg-muted/40"
                  }
                >
                  <div className="flex min-w-0 items-center gap-2.5">
                    <input
                      type="checkbox"
                      className="h-4 w-4 shrink-0"
                      checked={checked}
                      disabled={shift.paid || paying}
                      onChange={() => toggle(shift.attendance_entry_id)}
                    />
                    <div className="min-w-0">
                      <div className="font-medium">смена {formatShiftDay(shift.work_date)}</div>
                      <div className="text-xs text-muted-foreground">
                        {shift.hours} ч{shift.paid ? " · выдано" : ""}
                      </div>
                    </div>
                  </div>
                  <div className="text-right font-medium tabular-nums">
                    {formatRub(shift.amount)}
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
}: {
  target: KassaTarget;
  balance: number;
  busy: boolean;
  onPay: (amount: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const [amount, setAmount] = useState("");

  const amountNumber = Number(amount.replace(",", "."));
  const validAmount = Number.isFinite(amountNumber) && amountNumber > 0;
  const overOutstanding = validAmount && amountNumber > target.outstanding + 0.005;
  const overBalance = validAmount && amountNumber > balance;

  return (
    <div className="rounded-md border p-3 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
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
          </div>
          {target.counterparty_name ? (
            <div className="text-xs font-medium">{target.counterparty_name}</div>
          ) : null}
          {target.purpose ? (
            <div className="text-xs text-muted-foreground">{target.purpose}</div>
          ) : null}
        </div>
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
      </div>

      {open ? (
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
