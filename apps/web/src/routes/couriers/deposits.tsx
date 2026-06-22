import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  History,
  LoaderCircle,
  Plus,
  RefreshCw,
  Search,
  WalletCards,
} from "lucide-react";
import { toast } from "sonner";

import { CourierDepositHistoryDrawer } from "@/components/deposits/CourierDepositHistoryDrawer";
import {
  COURIER_CATEGORY_LABELS,
  COURIER_STATUS_LABELS,
  COURIER_TRANSACTION_LABELS,
  centsFromRubInput,
  formatCents,
  formatDate,
  formatPercent,
  isPositiveRubInput,
  progressValue,
  statusLabel,
  todayKey,
  transactionTone,
} from "@/components/deposits/courier-deposit-utils";
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
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { EmptyState } from "@/components/ui-app/EmptyState";
import { PageHeader } from "@/components/ui-app/PageHeader";
import {
  apiErrorMessage,
  getCourierDeposits,
  postCourierDepositTransaction,
  type CourierDepositCategoryFilter,
  type CourierDepositRow,
  type CourierDepositStatusFilter,
  type CourierDepositTransaction,
  type CourierDepositTransactionType,
} from "@/lib/api";
import { usePermissions } from "@/lib/permissions";
import { cn } from "@/lib/utils";

type CourierDepositsRouteProps = {
  onNavigate?: (path: string) => void;
  embedded?: boolean;
};

type PendingOperation = {
  row: CourierDepositRow;
  type: CourierDepositTransactionType;
};

function isDepositCollected(row: CourierDepositRow) {
  return row.balance_cents >= row.target_amount_cents && row.target_amount_cents > 0;
}

function depositGroupRank(row: CourierDepositRow) {
  if (row.balance_cents <= 0) {
    return 2; // без операций — вниз
  }
  if (isDepositCollected(row)) {
    return 1; // собрали полностью
  }
  return 0; // собирают — наверх
}

export function CourierDepositsRoute({ onNavigate, embedded = false }: CourierDepositsRouteProps) {
  void onNavigate;
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const canWrite = permissions.canPerformAction("couriers.deposits.edit");
  const [statusFilter, setStatusFilter] = useState<CourierDepositStatusFilter>("active");
  const [categoryFilter, setCategoryFilter] = useState<CourierDepositCategoryFilter>("all");
  const [search, setSearch] = useState("");
  const [pendingOperation, setPendingOperation] = useState<PendingOperation | null>(null);
  const [historyCourier, setHistoryCourier] = useState<CourierDepositRow | null>(null);

  const depositsQuery = useQuery({
    queryKey: ["courier-deposits", statusFilter, categoryFilter],
    queryFn: () => getCourierDeposits({ category: categoryFilter, status: statusFilter }),
    staleTime: 60_000,
  });

  const rows = useMemo(() => {
    const searchValue = search.trim().toLocaleLowerCase("ru");
    return [...(depositsQuery.data ?? [])]
      .filter((row) => !searchValue || row.full_name.toLocaleLowerCase("ru").includes(searchValue))
      .sort((left, right) => {
        const rankDiff = depositGroupRank(left) - depositGroupRank(right);
        if (rankDiff !== 0) {
          return rankDiff;
        }
        // внутри «собирают» — по убыванию суммы (кто больше собрал, тот выше)
        if (depositGroupRank(left) === 0 && right.balance_cents !== left.balance_cents) {
          return right.balance_cents - left.balance_cents;
        }
        return left.full_name.localeCompare(right.full_name, "ru");
      });
  }, [depositsQuery.data, search]);

  const refreshButton = (
    <Button
      onClick={() => void queryClient.invalidateQueries({ queryKey: ["courier-deposits"] })}
      title="Обновить"
      variant="outline"
    >
      <RefreshCw size={16} aria-hidden="true" />
      Обновить
    </Button>
  );

  return (
    <div className="space-y-5">
      {embedded ? (
        <div className="flex justify-end">{refreshButton}</div>
      ) : (
        <PageHeader
          title="Депозиты курьеров"
          description="Оперативная работа с балансами курьеров."
          action={refreshButton}
        />
      )}

      {!canWrite ? (
        <div className="rounded-md border bg-muted/40 px-3 py-2 text-sm text-muted-foreground">
          Режим просмотра. Балансы и история доступны, операции скрыты без права редактировать депозиты курьеров.
        </div>
      ) : null}

      <section className="space-y-4 rounded-lg border bg-card p-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="grid gap-2">
            <Label>Статус</Label>
            <div className="flex rounded-md border bg-background p-1">
              {(["active", "fired", "all"] as const).map((status) => (
                <button
                  className={cn(
                    "h-8 rounded-sm px-3 text-sm font-medium transition-colors",
                    statusFilter === status
                      ? "bg-primary text-primary-foreground"
                      : "text-muted-foreground hover:bg-muted hover:text-foreground",
                  )}
                  key={status}
                  onClick={() => setStatusFilter(status)}
                  type="button"
                >
                  {COURIER_STATUS_LABELS[status]}
                </button>
              ))}
            </div>
          </div>

          <Label className="grid w-48 gap-2">
            <span>Категория</span>
            <Select
              onValueChange={(value) =>
                setCategoryFilter(value as CourierDepositCategoryFilter)
              }
              value={categoryFilter}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">Все</SelectItem>
                <SelectItem value="primary">Primary</SelectItem>
                <SelectItem value="secondary">Secondary</SelectItem>
              </SelectContent>
            </Select>
          </Label>

          <Label className="grid min-w-[280px] flex-1 gap-2">
            <span>Поиск по ФИО</span>
            <div className="relative">
              <Search
                className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
                aria-hidden="true"
              />
              <Input
                className="pl-9"
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Введите имя курьера"
                value={search}
              />
            </div>
          </Label>
        </div>
      </section>

      <section className="rounded-lg border bg-card">
        {depositsQuery.isLoading ? (
          <CourierDepositsTableSkeleton />
        ) : depositsQuery.isError ? (
          <div className="p-4">
            <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {apiErrorMessage(depositsQuery.error, "Не удалось загрузить депозиты курьеров")}
            </div>
          </div>
        ) : rows.length === 0 ? (
          <div className="p-4">
            <EmptyState
              icon={<WalletCards size={18} aria-hidden="true" />}
              title={emptyTitle(statusFilter)}
              description="Измените фильтры или проверьте, что сотрудники с должностью «Курьер» загружены."
            />
          </div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="min-w-[260px]">Курьер</TableHead>
                <TableHead>Категория</TableHead>
                <TableHead className="text-right">Текущий баланс</TableHead>
                <TableHead className="min-w-[180px]">% достижения</TableHead>
                <TableHead className="min-w-[220px]">Последняя операция</TableHead>
                <TableHead className="w-[220px] text-right">Действия</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => (
                <TableRow key={row.employee_id}>
                  <TableCell>
                    <button
                      className="grid text-left text-sm font-medium underline-offset-2 hover:underline"
                      onClick={() => setHistoryCourier(row)}
                      type="button"
                    >
                      {row.full_name}
                      <span className="mt-1 text-xs font-normal text-muted-foreground">
                        {statusLabel(row.status)}
                      </span>
                    </button>
                  </TableCell>
                  <TableCell>
                    {row.category ? (
                      <Badge className="rounded-md" variant="secondary">
                        {COURIER_CATEGORY_LABELS[row.category]}
                      </Badge>
                    ) : (
                      <span className="text-sm text-muted-foreground">Не задана</span>
                    )}
                  </TableCell>
                  <TableCell className="text-right font-semibold tabular-nums">
                    {formatCents(row.balance_cents)}
                  </TableCell>
                  <TableCell>
                    <ProgressBar value={progressValue(row)} />
                    <div className="mt-1 text-xs text-muted-foreground">
                      {formatPercent(row.progress_pct)} · цель {formatCents(row.target_amount_cents)}
                    </div>
                  </TableCell>
                  <TableCell>
                    <LastOperation transaction={row.last_transaction} />
                  </TableCell>
                  <TableCell className="text-right">
                    <div className="flex justify-end gap-2">
                      {canWrite ? (
                        isDepositCollected(row) ? (
                          <Button size="sm" disabled title="Депозит собран — собирать нечего">
                            <Plus size={16} aria-hidden="true" />
                            Собран
                          </Button>
                        ) : (
                        <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <Button size="sm">
                              <Plus size={16} aria-hidden="true" />
                              Операция
                            </Button>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="end">
                            <DropdownMenuItem
                              onClick={() => setPendingOperation({ row, type: "top_up" })}
                            >
                              Пополнение
                            </DropdownMenuItem>
                            <DropdownMenuItem
                              onClick={() => setPendingOperation({ row, type: "return" })}
                            >
                              Возврат
                            </DropdownMenuItem>
                            <DropdownMenuItem
                              onClick={() => setPendingOperation({ row, type: "forfeit" })}
                            >
                              Списание
                            </DropdownMenuItem>
                          </DropdownMenuContent>
                        </DropdownMenu>
                        )
                      ) : null}
                      <Button onClick={() => setHistoryCourier(row)} size="sm" variant="outline">
                        <History size={16} aria-hidden="true" />
                        История
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </section>

      <CourierOperationDialog
        operation={pendingOperation}
        onClose={() => setPendingOperation(null)}
        queryClient={queryClient}
      />

      <CourierDepositHistoryDrawer
        courier={historyCourier}
        onOpenChange={(open) => {
          if (!open) {
            setHistoryCourier(null);
          }
        }}
        open={Boolean(historyCourier)}
      />
    </div>
  );
}

function CourierOperationDialog({
  onClose,
  operation,
  queryClient,
}: {
  onClose: () => void;
  operation: PendingOperation | null;
  queryClient: ReturnType<typeof useQueryClient>;
}) {
  const [date, setDate] = useState(todayKey());
  const [amount, setAmount] = useState("");
  const [comment, setComment] = useState("");
  const [payoutMethod, setPayoutMethod] = useState<"cash_tk" | "cash_safe" | "bank_draft">(
    "cash_tk",
  );
  const [submitted, setSubmitted] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const type = operation?.type ?? "top_up";
  const requiresConfirmation = type === "return" || type === "forfeit";
  const amountValid = isPositiveRubInput(amount);
  const commentValid = type !== "forfeit" || comment.trim().length > 0;
  const title = operation ? COURIER_TRANSACTION_LABELS[operation.type] : "Операция";

  useEffect(() => {
    if (!operation) {
      return;
    }
    setDate(todayKey());
    setAmount("");
    setComment("");
    setPayoutMethod("cash_tk");
    setSubmitted(false);
    setConfirmOpen(false);
    setFormError(null);
  }, [operation]);

  const mutation = useMutation({
    mutationFn: async () => {
      const cents = centsFromRubInput(amount);
      if (!operation || cents === null || cents <= 0) {
        throw new Error("Проверьте данные операции");
      }
      return postCourierDepositTransaction(operation.row.employee_id, {
        amount_cents: cents,
        comment: comment.trim() || null,
        transaction_date: date,
        transaction_type: operation.type,
        payout_method: operation.type === "return" ? payoutMethod : undefined,
      });
    },
    onSuccess: async () => {
      toast.success("Операция создана");
      setConfirmOpen(false);
      onClose();
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["courier-deposits"] }),
        queryClient.invalidateQueries({ queryKey: ["courier-deposit-card"] }),
      ]);
    },
    onError: (error) => {
      const message = apiErrorMessage(error, "Не удалось создать операцию");
      setFormError(message);
      toast.error(message);
    },
  });

  function submit() {
    setSubmitted(true);
    setFormError(null);
    if (!amountValid || !commentValid) {
      return;
    }
    if (requiresConfirmation) {
      setConfirmOpen(true);
      return;
    }
    mutation.mutate();
  }

  return (
    <>
      <Dialog
        onOpenChange={(open) => {
          if (!open && !mutation.isPending) {
            onClose();
          }
        }}
        open={Boolean(operation)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            <DialogDescription>
              {operation ? `Курьер: ${operation.row.full_name}` : "Создание операции"}
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-4">
            <Label className="grid gap-2">
              <span>Дата</span>
              <Input
                disabled={mutation.isPending}
                onChange={(event) => setDate(event.target.value)}
                type="date"
                value={date}
              />
            </Label>

            <Label className="grid gap-2">
              <span>Сумма ₽</span>
              <Input
                disabled={mutation.isPending}
                min={0}
                onChange={(event) => setAmount(event.target.value)}
                type="number"
                value={amount}
              />
              {submitted && !amountValid ? (
                <span className="text-sm text-destructive">Введите положительную сумму.</span>
              ) : null}
            </Label>

            {type === "return" ? (
              <Label className="grid gap-2">
                <span>Счёт выдачи</span>
                <select
                  className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm disabled:opacity-50"
                  disabled={mutation.isPending}
                  onChange={(event) =>
                    setPayoutMethod(
                      event.target.value as "cash_tk" | "cash_safe" | "bank_draft",
                    )
                  }
                  value={payoutMethod}
                >
                  <option value="cash_tk">Торговая касса Черникова</option>
                  <option value="cash_safe">Сейф</option>
                  <option value="bank_draft">Банк-черновик (через Сейф)</option>
                </select>
                <span className="text-xs text-muted-foreground">
                  {payoutMethod === "cash_tk"
                    ? "Наличные из кассы Черникова + изъятие в iiko."
                    : payoutMethod === "cash_safe"
                      ? "Наличные с карты «Сейф». Изъятие в iiko не делается."
                      : "Перевод р/с → Сейф и черновик платежа на ИП Шокину (как ЗП); раздача с Сейфа, iiko не трогаем."}
                </span>
              </Label>
            ) : null}

            <Label className="grid gap-2">
              <span>{type === "forfeit" ? "Причина" : "Комментарий"}</span>
              <Textarea
                disabled={mutation.isPending}
                onChange={(event) => setComment(event.target.value)}
                placeholder={type === "forfeit" ? "Укажите причину списания" : "Необязательно"}
                value={comment}
              />
              {submitted && !commentValid ? (
                <span className="text-sm text-destructive">Для списания нужна причина.</span>
              ) : null}
            </Label>

            {formError ? (
              <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                {formError}
              </div>
            ) : null}
          </div>

          <DialogFooter>
            <Button disabled={mutation.isPending} onClick={onClose} type="button" variant="outline">
              Отмена
            </Button>
            <Button disabled={mutation.isPending} onClick={submit} type="button">
              {mutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <Plus size={16} aria-hidden="true" />
              )}
              Создать операцию
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog onOpenChange={setConfirmOpen} open={confirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Подтвердить операцию?</AlertDialogTitle>
            <AlertDialogDescription>
              {operation
                ? `Создать операцию «${COURIER_TRANSACTION_LABELS[operation.type]}» на ${formatCents(
                    centsFromRubInput(amount) ?? 0,
                  )} для курьера «${operation.row.full_name}»?`
                : ""}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={mutation.isPending}>Отмена</AlertDialogCancel>
            <AlertDialogAction
              disabled={mutation.isPending}
              onClick={(event) => {
                event.preventDefault();
                mutation.mutate();
              }}
            >
              {mutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : null}
              Создать
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

function LastOperation({ transaction }: { transaction: CourierDepositTransaction | null }) {
  if (!transaction) {
    return <span className="text-sm text-muted-foreground">Нет операций</span>;
  }
  const positive = transactionTone(transaction.transaction_type) === "positive";
  return (
    <div className="grid gap-1 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <TransactionBadge type={transaction.transaction_type} />
        <span className="tabular-nums">{formatDate(transaction.transaction_date)}</span>
      </div>
      <span className={cn("font-medium tabular-nums", positive ? "text-emerald-700" : "text-rose-700")}>
        {positive ? "+" : "−"}
        {formatCents(transaction.amount_cents)}
      </span>
    </div>
  );
}

function TransactionBadge({ type }: { type: CourierDepositTransactionType }) {
  const positive = transactionTone(type) === "positive";
  return (
    <Badge
      className={cn(
        "rounded-md shadow-none",
        positive
          ? "border-emerald-200 bg-emerald-50 text-emerald-800 hover:bg-emerald-50"
          : "border-rose-200 bg-rose-50 text-rose-800 hover:bg-rose-50",
      )}
      variant="outline"
    >
      {COURIER_TRANSACTION_LABELS[type]}
    </Badge>
  );
}

function ProgressBar({ value }: { value: number }) {
  return (
    <div className="h-2 overflow-hidden rounded-full bg-muted">
      <div className="h-full rounded-full bg-primary" style={{ width: `${value}%` }} />
    </div>
  );
}

function CourierDepositsTableSkeleton() {
  return (
    <div className="space-y-3 p-4">
      <Skeleton className="h-12 w-full" />
      <Skeleton className="h-12 w-full" />
      <Skeleton className="h-12 w-full" />
      <div className="flex justify-end">
        <LoaderCircle className="h-5 w-5 animate-spin text-muted-foreground" aria-hidden="true" />
      </div>
    </div>
  );
}

function emptyTitle(status: CourierDepositStatusFilter) {
  if (status === "active") {
    return "Нет активных курьеров";
  }
  if (status === "fired") {
    return "Нет уволенных курьеров";
  }
  return "Курьеры не найдены";
}

