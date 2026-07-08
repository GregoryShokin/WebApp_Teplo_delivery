import { History, Trash2, X } from "lucide-react";
import { useState } from "react";
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
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/ui-app/EmptyState";
import {
  apiErrorMessage,
  deleteCourierDepositTransaction,
  getCourierDepositCard,
  type CourierDepositRow,
  type CourierDepositTransaction,
  type CourierDepositTransactionType,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  COURIER_TRANSACTION_LABELS,
  formatCents,
  formatDate,
  formatDateTime,
  formatPercent,
  shortId,
  transactionTone,
} from "./courier-deposit-utils";

type CourierDepositHistoryDrawerProps = {
  // Право на удаление пополнений (couriers.deposits.delete). Без него — только просмотр.
  canDeleteTopup?: boolean;
  courier: CourierDepositRow | null;
  onOpenChange: (open: boolean) => void;
  open: boolean;
};

export function CourierDepositHistoryDrawer({
  canDeleteTopup = false,
  courier,
  onOpenChange,
  open,
}: CourierDepositHistoryDrawerProps) {
  const queryClient = useQueryClient();
  const [pendingDelete, setPendingDelete] = useState<CourierDepositTransaction | null>(null);
  const cardQuery = useQuery({
    queryKey: ["courier-deposit-card", courier?.employee_id],
    queryFn: () => getCourierDepositCard(courier?.employee_id ?? ""),
    enabled: open && Boolean(courier?.employee_id),
    staleTime: 0,
  });
  const account = cardQuery.data?.account;
  const balance = cardQuery.data?.balance_cents ?? courier?.balance_cents ?? 0;
  const target = account?.target_amount_cents ?? courier?.target_amount_cents ?? 0;
  const progress = target > 0 ? Math.min((balance / target) * 100, 100) : 0;
  const transactions = cardQuery.data?.transactions ?? [];

  const deleteMutation = useMutation({
    mutationFn: (transactionId: number) =>
      deleteCourierDepositTransaction(courier?.employee_id ?? "", transactionId),
    onSuccess: async () => {
      toast.success("Пополнение удалено");
      setPendingDelete(null);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["courier-deposit-card"] }),
        queryClient.invalidateQueries({ queryKey: ["courier-deposits"] }),
      ]);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось удалить пополнение"));
    },
  });

  return (
    <Sheet onOpenChange={onOpenChange} open={open}>
      <SheetContent className="flex w-[620px] max-w-full flex-col gap-0 p-0 sm:max-w-xl">
        <div className="border-b px-5 py-4">
          <div className="flex items-start justify-between gap-3 pr-8">
            <SheetHeader className="space-y-1">
              <SheetTitle>{courier?.full_name ?? "История операций"}</SheetTitle>
              <SheetDescription>
                Баланс {formatCents(balance)} · цель {formatCents(target)} ·{" "}
                {formatPercent(progress)}
              </SheetDescription>
            </SheetHeader>
            <Button onClick={() => onOpenChange(false)} size="sm" variant="outline">
              <X size={16} aria-hidden="true" />
              Закрыть
            </Button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4">
          {cardQuery.isLoading ? (
            <div className="space-y-3">
              <Skeleton className="h-16 w-full" />
              <Skeleton className="h-16 w-full" />
              <Skeleton className="h-16 w-full" />
            </div>
          ) : cardQuery.isError ? (
            <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {apiErrorMessage(cardQuery.error, "Не удалось загрузить историю операций")}
            </div>
          ) : transactions.length === 0 ? (
            <EmptyState
              icon={<History size={18} aria-hidden="true" />}
              title="Нет операций по этому курьеру"
              description="После пополнения, возврата или списания записи появятся здесь."
            />
          ) : (
            <div className="space-y-3">
              {transactions.map((transaction) => {
                // Удалять можно только пополнения: у них нет следа в iiko/банке.
                const canDelete =
                  canDeleteTopup &&
                  transaction.transaction_type === "top_up" &&
                  transaction.id != null;
                return (
                  <div
                    className="rounded-md border bg-card px-4 py-3"
                    key={transaction.id ?? `${transaction.transaction_date}-${transaction.amount_cents}`}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <TransactionBadge type={transaction.transaction_type} />
                          <span className="text-sm text-muted-foreground">
                            {formatDate(transaction.transaction_date)}
                          </span>
                        </div>
                        <div className="mt-2 text-sm leading-6">
                          {transaction.comment?.trim() || "Комментарий не указан"}
                        </div>
                        <div className="mt-1 text-xs leading-5 text-muted-foreground">
                          Автор: {transaction.created_by_name || shortId(transaction.created_by)} ·{" "}
                          создано {formatDateTime(transaction.created_at)}
                        </div>
                      </div>
                      <div className="flex shrink-0 flex-col items-end gap-2">
                        <div
                          className={cn(
                            "text-right text-base font-semibold tabular-nums",
                            transactionTone(transaction.transaction_type) === "positive"
                              ? "text-emerald-700"
                              : "text-rose-700",
                          )}
                        >
                          {transactionTone(transaction.transaction_type) === "positive" ? "+" : "−"}
                          {formatCents(transaction.amount_cents)}
                        </div>
                        {canDelete ? (
                          <Button
                            className="h-7 gap-1 px-2 text-xs text-muted-foreground hover:text-destructive"
                            disabled={deleteMutation.isPending}
                            onClick={() => setPendingDelete(transaction)}
                            size="sm"
                            variant="ghost"
                          >
                            <Trash2 size={14} aria-hidden="true" />
                            Удалить
                          </Button>
                        ) : null}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </SheetContent>

      <AlertDialog
        onOpenChange={(dialogOpen) => {
          if (!dialogOpen) {
            setPendingDelete(null);
          }
        }}
        open={pendingDelete != null}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Удалить пополнение?</AlertDialogTitle>
            <AlertDialogDescription>
              Пополнение на {formatCents(pendingDelete?.amount_cents ?? 0)} от{" "}
              {formatDate(pendingDelete?.transaction_date ?? "")} будет удалено вместе со связанной
              проводкой в ДДС. Баланс депозита и кассы «ТК Черникова» уменьшатся. Действие необратимо.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteMutation.isPending}>Отмена</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              disabled={deleteMutation.isPending}
              onClick={(event) => {
                event.preventDefault();
                if (pendingDelete?.id != null) {
                  deleteMutation.mutate(pendingDelete.id);
                }
              }}
            >
              {deleteMutation.isPending ? "Удаление…" : "Удалить"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Sheet>
  );
}

function TransactionBadge({ type }: { type: CourierDepositTransactionType }) {
  const isPositive = transactionTone(type) === "positive";
  return (
    <Badge
      className={cn(
        "rounded-md shadow-none",
        isPositive
          ? "border-emerald-200 bg-emerald-50 text-emerald-800 hover:bg-emerald-50"
          : "border-rose-200 bg-rose-50 text-rose-800 hover:bg-rose-50",
      )}
      variant="outline"
    >
      {COURIER_TRANSACTION_LABELS[type]}
    </Badge>
  );
}
