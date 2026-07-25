import { useMutation, useQuery } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useState } from "react";
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
import { confirmEmployeePayout, getDdsBankOperations } from "@/lib/api";
import { toIsoDate } from "@/lib/date";
import { cn } from "@/lib/utils";

import type { PaymentRow } from "./payments-api";

// Окно выписки для поиска операции. Выплата подтверждается вручную только когда автоматический
// путь (вебхук/поллинг статуса) не сработал, поэтому операция может быть и не сегодняшней.
const LOOKBACK_DAYS = 45;

function daysAgoIso(days: number): string {
  const date = new Date();
  date.setDate(date.getDate() - days);
  return toIsoDate(date);
}

/**
 * Ручное подтверждение банковской выплаты сотруднику привязкой к операции выписки.
 *
 * Штатно выплату доводит до «оплачено» статус платёжного документа (вебхук Т-Банка или
 * поллинг), заводя транзит р/с→Сейф и резерв. Это окно — запасной путь: Сбер черновиков не
 * создаёт, а вебхук может быть пропущен. Раньше единственный вход в привязку жил внутри
 * «Нового платежа» сразу после создания — закрыв его, владелец терял выплату из виду.
 */
export function LinkPayoutOperationDialog({
  row,
  onOpenChange,
  onDone,
}: {
  row: PaymentRow | null;
  onOpenChange: (open: boolean) => void;
  onDone: () => Promise<void> | void;
}) {
  const [operationId, setOperationId] = useState("");
  const open = row !== null;

  const operationsQuery = useQuery({
    queryKey: ["finance-payments", "payout-operations"],
    queryFn: () =>
      getDdsBankOperations({
        from: daysAgoIso(LOOKBACK_DAYS),
        to: toIsoDate(new Date()),
        limit: 100,
      }),
    enabled: open,
  });
  // Кандидаты — только исходящие и ещё не разобранные: привязка заведёт проводки сама,
  // а уже классифицированная операция задвоила бы движение денег.
  const operations = (operationsQuery.data?.items ?? []).filter(
    (op) => op.direction === "out" && op.cashflow_transaction_id === null,
  );

  const confirmMutation = useMutation({
    mutationFn: () => confirmEmployeePayout(row?.ref_id ?? "", operationId),
    onSuccess: async () => {
      toast.success("Выплата подтверждена — деньги переведены на Сейф резервом");
      setOperationId("");
      onOpenChange(false);
      await onDone();
    },
    onError: (error) => {
      const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data
        ?.detail;
      toast.error(detail ?? "Не удалось подтвердить выплату");
    },
  });

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) setOperationId("");
        onOpenChange(next);
      }}
    >
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Привязать операцию</DialogTitle>
          <DialogDescription>
            {row?.title}. Выберите исходящую операцию из выписки — она подтвердит выплату и заведёт
            перевод на Сейф с резервом.
          </DialogDescription>
        </DialogHeader>

        <div className="max-h-[340px] space-y-2 overflow-y-auto">
          {operationsQuery.isLoading ? (
            <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
              <Loader2 className="animate-spin" size={16} aria-hidden="true" />
              Загрузка операций…
            </div>
          ) : operationsQuery.isError ? (
            <div className="py-6 text-sm text-muted-foreground">
              Не удалось загрузить операции из выписки (возможно, нет права просмотра ДДС). Выплата
              сохранена — привязать можно позже.
            </div>
          ) : operations.length === 0 ? (
            <div className="py-6 text-sm text-muted-foreground">
              Нет несопоставленных исходящих операций за последние {LOOKBACK_DAYS} дней. Операция
              появится после импорта выписки — привяжите позже.
            </div>
          ) : (
            operations.map((op) => (
              <button
                className={cn(
                  "w-full rounded-md border p-2 text-left text-sm transition hover:bg-muted/50",
                  operationId === op.id ? "border-primary bg-muted/50" : "border-border",
                )}
                key={op.id}
                onClick={() => setOperationId(op.id)}
                type="button"
              >
                <div className="flex justify-between gap-2">
                  <span className="font-medium tabular-nums">{op.amount} ₽</span>
                  <span className="text-muted-foreground">{op.operation_date}</span>
                </div>
                <div className="truncate text-xs text-muted-foreground">
                  {op.counterparty_name_raw || op.payment_purpose || "—"}
                </div>
              </button>
            ))
          )}
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={confirmMutation.isPending}
          >
            Позже
          </Button>
          <Button
            onClick={() => confirmMutation.mutate()}
            disabled={!operationId || confirmMutation.isPending}
          >
            {confirmMutation.isPending ? (
              <Loader2 className="animate-spin" size={14} />
            ) : (
              "Подтвердить выплату"
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
