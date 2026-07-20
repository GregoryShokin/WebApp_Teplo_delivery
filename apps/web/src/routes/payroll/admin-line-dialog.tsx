import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Banknote, CheckCircle2, LoaderCircle, Scale, Undo2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
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
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  apiErrorMessage,
  bulkMarkPayrollPayments,
  markPartialPayrollPayment,
  unmarkPayrollPayment,
  type PayrollLine,
} from "@/lib/api";

import { AdjustmentDisclosure, ComponentValue, PayoutFormula } from "./admin-payslip-shared";
import {
  dishwasherShiftCount,
  extractEmployeePayoutOffset,
  lineAdjustments,
  lineOnHand,
  linePaidOnHand,
  moneyValue,
  normalizeMoney,
  onDemandPeriodAccrual,
  paymentMethodLabel,
  todayDateInputValue,
} from "./admin-payslip-utils";
import { formatDate, formatMoney } from "./runs";

export type AdminLineDetailRow = {
  line: PayrollLine;
  employeeName: string;
  position: string;
};

/**
 * Раскрытие строки сотрудника администрации (модальное окно, как в «Расчётах»):
 * детализация начислений, премий/штрафов, формула итога и действия по выплате.
 * Управление удержаниями (авансы/займы) и on-demand выплата собственника вынесены
 * колбэками, чтобы переиспользовать существующие диалоги родителя.
 */
export function AdminLineDialog({
  canManagePayments,
  canCreatePayout,
  onIncludeOnDemand,
  onManageRecovery,
  onOpenChange,
  open,
  periodLabel,
  row,
}: {
  canManagePayments: boolean;
  canCreatePayout: boolean;
  onIncludeOnDemand: (row: AdminLineDetailRow) => void;
  onManageRecovery: (employeeId: string) => void;
  onOpenChange: (open: boolean) => void;
  open: boolean;
  periodLabel: string;
  row: AdminLineDetailRow | null;
}) {
  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent className="max-w-3xl">
        {row ? (
          <AdminLineDialogContent
            canCreatePayout={canCreatePayout}
            canManagePayments={canManagePayments}
            onIncludeOnDemand={() => onIncludeOnDemand(row)}
            onManageRecovery={() => onManageRecovery(row.line.employee_id)}
            periodLabel={periodLabel}
            row={row}
          />
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

function AdminLineDialogContent({
  canCreatePayout,
  canManagePayments,
  onIncludeOnDemand,
  onManageRecovery,
  periodLabel,
  row,
}: {
  canCreatePayout: boolean;
  canManagePayments: boolean;
  onIncludeOnDemand: () => void;
  onManageRecovery: () => void;
  periodLabel: string;
  row: AdminLineDetailRow;
}) {
  const { line } = row;
  const adjustments = lineAdjustments(line);
  const shifts = dishwasherShiftCount(line);
  const bankPaid = extractEmployeePayoutOffset(line);
  const deduction = moneyValue(line.deduction);
  const advanceIssued = moneyValue(line.advance_issued);
  const isOnDemand = line.on_demand;

  const accruedLabel = isOnDemand
    ? formatMoney(onDemandPeriodAccrual(line))
    : formatMoney(line.base_pay);
  const accruedHint = isOnDemand
    ? "оклад за месяц"
    : shifts !== null
      ? `${shifts} смен`
      : undefined;

  return (
    <div className="space-y-5">
      <DialogHeader>
        <DialogTitle className="pr-8">{row.employeeName}</DialogTitle>
        <DialogDescription>
          {row.position || line.role || "Должность не указана"} ·{" "}
          {periodLabel || "Период ведомости"}
        </DialogDescription>
      </DialogHeader>

      <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
        <div className="rounded-md border bg-background p-3">
          <div className="text-xs text-muted-foreground">Начислено</div>
          <div className="mt-1 font-medium tabular-nums">{accruedLabel}</div>
          {accruedHint ? (
            <div className="mt-0.5 text-xs text-muted-foreground">{accruedHint}</div>
          ) : null}
        </div>
        <ComponentValue label="Премии" value={formatMoney(line.premium)} />
        {deduction > 0 ? (
          <ComponentValue label="Всего удержано" value={formatMoney(deduction)} />
        ) : null}
        {advanceIssued > 0 ? (
          <ComponentValue label="Авансы/займы" value={formatMoney(advanceIssued)} />
        ) : null}
        {bankPaid > 0 ? (
          <ComponentValue label="Уже выпл. банком" value={formatMoney(bankPaid)} />
        ) : null}
        <ComponentValue
          label="К выплате"
          value={isOnDemand ? "—" : formatMoney(line.total_payable)}
          strong
        />
      </section>

      {isOnDemand ? (
        <section className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-amber-200 bg-amber-50/60 p-3">
          <div>
            <div className="text-sm font-semibold text-amber-950">Оклад «по востребованию»</div>
            <div className="mt-1 text-sm text-amber-800">
              Не выплачивается автоматически. Остаток долга{" "}
              {formatMoney(moneyValue(line.on_demand_debt))}.
            </div>
          </div>
          {canCreatePayout ? (
            <Button onClick={onIncludeOnDemand} size="sm" type="button" variant="outline">
              Включить в выплату
            </Button>
          ) : null}
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

      <section className="flex flex-wrap items-center justify-between gap-2 rounded-md border bg-muted/20 px-3 py-2">
        <div className="text-sm">
          <span className="font-medium">Удержания авансов и займов</span>
          <span className="text-muted-foreground"> · управление отсрочкой и ручным удержанием</span>
        </div>
        <Button onClick={onManageRecovery} size="sm" type="button" variant="outline">
          <Scale size={15} aria-hidden="true" />
          Управление удержаниями
        </Button>
      </section>

      <section className="grid gap-3 rounded-lg border border-emerald-200 bg-emerald-50/50 p-4 lg:grid-cols-[1fr_auto] lg:items-center">
        <div>
          <div className="text-sm font-semibold text-emerald-950">Итог выплаты</div>
          {isOnDemand ? (
            <div className="mt-1 text-sm text-emerald-900">
              Выплата отражается вручную через «Включить в выплату».
            </div>
          ) : (
            <PayoutFormula line={line} />
          )}
        </div>
        {isOnDemand ? null : (
          <AdminPaymentActions canManagePayments={canManagePayments} line={line} />
        )}
      </section>
    </div>
  );
}

/** Действия по выплате строки: полная, частичная (доплата остатка) и отмена — как в
 *  «Расчётах», с инвалидацией админских ключей. */
function AdminPaymentActions({
  canManagePayments,
  line,
}: {
  canManagePayments: boolean;
  line: PayrollLine;
}) {
  const queryClient = useQueryClient();
  const isPaid = line.payment_status === "paid";
  const isPartial = line.payment_status === "partially_paid";
  const accrued = lineOnHand(line);
  const paid = linePaidOnHand(line);
  const remaining = normalizeMoney(Math.max(0, accrued - paid));
  const [dialogOpen, setDialogOpen] = useState(false);
  const [amountInput, setAmountInput] = useState("");
  const [comment, setComment] = useState("");

  const invalidate = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ["payroll-admin-run", line.run_id] }),
      queryClient.invalidateQueries({ queryKey: ["payroll-admin-run-lines", line.run_id] }),
      queryClient.invalidateQueries({ queryKey: ["admin-run-bank-draft", line.run_id] }),
    ]);

  const unmarkMutation = useMutation({
    mutationFn: () => unmarkPayrollPayment(line.run_id, line.employee_id),
    onSuccess: async () => {
      await invalidate();
      toast.success("Отметка выплаты отменена");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось отменить отметку")),
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
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось отметить выплату")),
  });

  const fullMutation = useMutation({
    mutationFn: () =>
      bulkMarkPayrollPayments(line.run_id, [line.employee_id], todayDateInputValue()),
    onSuccess: async () => {
      await invalidate();
      toast.success("Выплата проведена полностью");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось провести выплату")),
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
    partialMutation.mutate({ amount: parsed, comment: comment.trim() ? comment.trim() : null });
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
            onClick={() => fullMutation.mutate()}
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
          <Button onClick={openDialog} size="sm" type="button" variant="outline">
            <Banknote size={15} aria-hidden="true" />
            Выплатить частично
          </Button>
        </div>
      ) : null}
      {canManagePayments && isPaid ? (
        <Button
          disabled={unmarkMutation.isPending}
          onClick={() => unmarkMutation.mutate()}
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
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{isPartial ? "Доплатить остаток" : "Частичная выплата"}</DialogTitle>
            <DialogDescription>
              Начислено {formatMoney(accrued)} · выплачено {formatMoney(paid)} · остаток{" "}
              {formatMoney(remaining)}.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1.5">
              <Label htmlFor="admin-partial-amount">Сумма к выплате</Label>
              <Input
                autoFocus
                id="admin-partial-amount"
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
              <Label htmlFor="admin-partial-comment">Причина недоплаты (необязательно)</Label>
              <Textarea
                id="admin-partial-comment"
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
