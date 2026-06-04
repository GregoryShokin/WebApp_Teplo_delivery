import { History, X } from "lucide-react";

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
  getCourierDepositCard,
  type CourierDepositRow,
  type CourierDepositTransactionType,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { useQuery } from "@tanstack/react-query";

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
  courier: CourierDepositRow | null;
  onOpenChange: (open: boolean) => void;
  open: boolean;
};

export function CourierDepositHistoryDrawer({
  courier,
  onOpenChange,
  open,
}: CourierDepositHistoryDrawerProps) {
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
              {transactions.map((transaction) => (
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
                    <div
                      className={cn(
                        "shrink-0 text-right text-base font-semibold tabular-nums",
                        transactionTone(transaction.transaction_type) === "positive"
                          ? "text-emerald-700"
                          : "text-rose-700",
                      )}
                    >
                      {transactionTone(transaction.transaction_type) === "positive" ? "+" : "−"}
                      {formatCents(transaction.amount_cents)}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </SheetContent>
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
