import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Banknote, History, LoaderCircle, RefreshCw, Search, WalletCards } from "lucide-react";
import { toast } from "sonner";

import {
  formatDateTime,
  formatMoney,
  formatPercentValue,
  isDepositTargetPosition,
  normalizeDecimalInput,
  progressValue,
  validNonNegativeDecimalInput,
} from "@/components/deposits/deposit-utils";
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
  getDeposits,
  getDepositTransactions,
  postDepositPayout,
  postDepositWriteoff,
  type DepositListItem,
  type DepositTransaction,
  type EmployeeCategory,
} from "@/lib/api";
import { EMPLOYEE_CATEGORY_LABELS } from "@/lib/i18n/employee";
import { usePermissions } from "@/lib/permissions";
import { cn } from "@/lib/utils";

type PayrollDepositsRouteProps = {
  onNavigate?: (path: string) => void;
};

type OperationType = "payout" | "writeoff";

type PendingOperation = {
  row: DepositListItem;
  type: OperationType;
};

type CategoryFilter = EmployeeCategory | "all";

const OPERATION_TITLE: Record<OperationType, string> = {
  payout: "Выдать депозит",
  writeoff: "Списать депозит",
};

const DEPOSITS_QUERY_KEY = "production-deposits";
const DEPOSIT_HISTORY_QUERY_KEY = "production-deposit-history";

function balanceNumber(row: DepositListItem) {
  const value = Number(row.balance);
  return Number.isFinite(value) ? value : 0;
}

function targetNumber(row: DepositListItem) {
  const value = Number(row.target);
  return Number.isFinite(value) ? value : 0;
}

function isCollected(row: DepositListItem) {
  const target = targetNumber(row);
  return target > 0 && balanceNumber(row) >= target;
}

function depositGroupRank(row: DepositListItem) {
  if (balanceNumber(row) <= 0) {
    return 2; // без накоплений — вниз
  }
  if (isCollected(row)) {
    return 1; // собран
  }
  return 0; // копит — наверх
}

function depositTxLabel(type: string) {
  switch (type) {
    case "accrual":
      return "Накопление";
    case "payout":
      return "Выдача";
    case "write_off":
      return "Списание";
    case "dismissal_payout":
      return "Выдача при увольнении";
    case "dismissal_writeoff":
      return "Списание при увольнении";
    default:
      return type;
  }
}

function isPositiveDepositTx(type: string) {
  return type === "accrual";
}

export function PayrollDepositsRoute({ onNavigate }: PayrollDepositsRouteProps) {
  void onNavigate;
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const canWrite = permissions.hasPermission("payroll.production_deposits.edit");
  const [categoryFilter, setCategoryFilter] = useState<CategoryFilter>("all");
  const [search, setSearch] = useState("");
  const [pendingOperation, setPendingOperation] = useState<PendingOperation | null>(null);
  const [historyEmployee, setHistoryEmployee] = useState<DepositListItem | null>(null);

  const depositsQuery = useQuery({
    queryKey: [DEPOSITS_QUERY_KEY],
    queryFn: getDeposits,
    staleTime: 60_000,
  });

  const targetRows = useMemo(
    () => (depositsQuery.data ?? []).filter((row) => isDepositTargetPosition(row.position)),
    [depositsQuery.data],
  );

  const rows = useMemo(() => {
    const searchValue = search.trim().toLocaleLowerCase("ru");
    return [...targetRows]
      .filter((row) => categoryFilter === "all" || row.category === categoryFilter)
      .filter(
        (row) => !searchValue || row.full_name.toLocaleLowerCase("ru").includes(searchValue),
      )
      .sort((left, right) => {
        const rankDiff = depositGroupRank(left) - depositGroupRank(right);
        if (rankDiff !== 0) {
          return rankDiff;
        }
        if (depositGroupRank(left) === 0 && balanceNumber(right) !== balanceNumber(left)) {
          return balanceNumber(right) - balanceNumber(left);
        }
        return left.full_name.localeCompare(right.full_name, "ru");
      });
  }, [targetRows, search, categoryFilter]);

  const summary = useMemo(() => {
    const totalBalance = targetRows.reduce((sum, row) => sum + balanceNumber(row), 0);
    const collecting = targetRows.filter((row) => balanceNumber(row) > 0 && !isCollected(row)).length;
    return { count: targetRows.length, totalBalance, collecting };
  }, [targetRows]);

  const refreshButton = (
    <Button
      onClick={() => void queryClient.invalidateQueries({ queryKey: [DEPOSITS_QUERY_KEY] })}
      title="Обновить"
      variant="outline"
    >
      <RefreshCw size={16} aria-hidden="true" />
      Обновить
    </Button>
  );

  return (
    <div className="space-y-5">
      <PageHeader
        title="Депозиты сотрудников"
        description="Прогресс накоплений, выдача и списание депозитов поваров и кассиров."
        action={refreshButton}
      />

      {!canWrite ? (
        <div className="rounded-md border bg-muted/40 px-3 py-2 text-sm text-muted-foreground">
          Режим просмотра. Балансы и история доступны, выдача и списание скрыты без права
          редактировать депозиты производства.
        </div>
      ) : null}

      <section className="grid gap-3 sm:grid-cols-3">
        <SummaryCard label="Сотрудников с депозитом" value={String(summary.count)} />
        <SummaryCard label="Копят сейчас" value={String(summary.collecting)} />
        <SummaryCard label="Суммарный баланс" value={formatMoney(summary.totalBalance)} />
      </section>

      <section className="space-y-4 rounded-lg border bg-card p-4">
        <div className="flex flex-wrap items-end gap-3">
          <Label className="grid w-48 gap-2">
            <span>Категория</span>
            <Select
              onValueChange={(value) => setCategoryFilter(value as CategoryFilter)}
              value={categoryFilter}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">Все</SelectItem>
                <SelectItem value="category_1">{EMPLOYEE_CATEGORY_LABELS.category_1}</SelectItem>
                <SelectItem value="category_2">{EMPLOYEE_CATEGORY_LABELS.category_2}</SelectItem>
                <SelectItem value="category_3">{EMPLOYEE_CATEGORY_LABELS.category_3}</SelectItem>
                <SelectItem value="category_4">{EMPLOYEE_CATEGORY_LABELS.category_4}</SelectItem>
                <SelectItem value="intern">{EMPLOYEE_CATEGORY_LABELS.intern}</SelectItem>
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
                placeholder="Введите имя сотрудника"
                value={search}
              />
            </div>
          </Label>
        </div>
      </section>

      <section className="rounded-lg border bg-card">
        {depositsQuery.isLoading ? (
          <DepositsTableSkeleton />
        ) : depositsQuery.isError ? (
          <div className="p-4">
            <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {apiErrorMessage(depositsQuery.error, "Не удалось загрузить депозиты")}
            </div>
          </div>
        ) : rows.length === 0 ? (
          <div className="p-4">
            <EmptyState
              icon={<WalletCards size={18} aria-hidden="true" />}
              title="Нет сотрудников с депозитом"
              description="Измените фильтры или проверьте, что повара и кассиры загружены."
            />
          </div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="min-w-[260px]">Сотрудник</TableHead>
                <TableHead>Категория</TableHead>
                <TableHead className="text-right">Баланс / цель</TableHead>
                <TableHead className="min-w-[180px]">Прогресс</TableHead>
                <TableHead>Удержание за смену</TableHead>
                <TableHead className="w-[220px] text-right">Действия</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => {
                const noBalance = balanceNumber(row) <= 0;
                return (
                  <TableRow key={row.id}>
                    <TableCell>
                      <button
                        className="grid text-left text-sm font-medium underline-offset-2 hover:underline"
                        onClick={() => setHistoryEmployee(row)}
                        type="button"
                      >
                        {row.full_name}
                        <span className="mt-1 text-xs font-normal text-muted-foreground">
                          {row.position ?? "—"}
                        </span>
                      </button>
                    </TableCell>
                    <TableCell>
                      {row.category ? (
                        <Badge className="rounded-md" variant="secondary">
                          {EMPLOYEE_CATEGORY_LABELS[row.category]}
                        </Badge>
                      ) : (
                        <span className="text-sm text-muted-foreground">Не задана</span>
                      )}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      <span className="font-semibold">{formatMoney(row.balance)}</span>
                      <span className="text-muted-foreground"> / {formatMoney(row.target)}</span>
                    </TableCell>
                    <TableCell>
                      <ProgressBar value={progressValue(row.progress_pct)} />
                      <div className="mt-1 text-xs text-muted-foreground">
                        {formatPercentValue(row.progress_pct)}% к цели
                      </div>
                    </TableCell>
                    <TableCell>
                      {row.is_excluded ? (
                        <Badge
                          className="rounded-md border-amber-200 bg-amber-50 text-amber-800 hover:bg-amber-50"
                          variant="outline"
                        >
                          Исключён
                        </Badge>
                      ) : (
                        <span className="text-sm tabular-nums">{formatMoney(row.withholding)}</span>
                      )}
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex justify-end gap-2">
                        {canWrite ? (
                          <DropdownMenu>
                            <DropdownMenuTrigger asChild>
                              <Button
                                disabled={noBalance}
                                size="sm"
                                title={noBalance ? "Нет баланса для операций" : undefined}
                              >
                                <Banknote size={16} aria-hidden="true" />
                                Операция
                              </Button>
                            </DropdownMenuTrigger>
                            <DropdownMenuContent align="end">
                              <DropdownMenuItem
                                onClick={() => setPendingOperation({ row, type: "payout" })}
                              >
                                Выдать депозит
                              </DropdownMenuItem>
                              <DropdownMenuItem
                                onClick={() => setPendingOperation({ row, type: "writeoff" })}
                              >
                                Списать депозит
                              </DropdownMenuItem>
                            </DropdownMenuContent>
                          </DropdownMenu>
                        ) : null}
                        <Button
                          onClick={() => setHistoryEmployee(row)}
                          size="sm"
                          variant="outline"
                        >
                          <History size={16} aria-hidden="true" />
                          История
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}
      </section>

      <DepositOperationDialog
        onClose={() => setPendingOperation(null)}
        operation={pendingOperation}
        queryClient={queryClient}
      />

      <DepositHistoryDialog
        employee={historyEmployee}
        onClose={() => setHistoryEmployee(null)}
      />
    </div>
  );
}

function DepositOperationDialog({
  onClose,
  operation,
  queryClient,
}: {
  onClose: () => void;
  operation: PendingOperation | null;
  queryClient: ReturnType<typeof useQueryClient>;
}) {
  const [amount, setAmount] = useState("");
  const [comment, setComment] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const type = operation?.type ?? "payout";
  const balance = operation ? balanceNumber(operation.row) : 0;
  const normalized = normalizeDecimalInput(amount);
  const amountNumber = Number(normalized);
  const amountValid = validNonNegativeDecimalInput(normalized) && amountNumber > 0;
  const withinBalance = amountValid && amountNumber <= balance + 1e-9;
  const reasonValid = type !== "writeoff" || comment.trim().length > 0;

  useEffect(() => {
    if (!operation) {
      return;
    }
    // Выдача по умолчанию — весь остаток; списание — пустая сумма.
    setAmount(operation.type === "payout" ? String(balanceNumber(operation.row)) : "");
    setComment("");
    setSubmitted(false);
    setConfirmOpen(false);
    setFormError(null);
  }, [operation]);

  const mutation = useMutation({
    mutationFn: async () => {
      if (!operation) {
        throw new Error("Нет операции");
      }
      if (operation.type === "payout") {
        return postDepositPayout(operation.row.id, {
          amount: normalized,
          comment: comment.trim() || null,
        });
      }
      return postDepositWriteoff(operation.row.id, {
        amount: normalized,
        reason: comment.trim() || null,
      });
    },
    onSuccess: async () => {
      toast.success(type === "payout" ? "Депозит выдан" : "Депозит списан");
      setConfirmOpen(false);
      onClose();
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: [DEPOSITS_QUERY_KEY] }),
        queryClient.invalidateQueries({ queryKey: [DEPOSIT_HISTORY_QUERY_KEY] }),
      ]);
    },
    onError: (error) => {
      const message = apiErrorMessage(error, "Не удалось выполнить операцию");
      setFormError(message);
      toast.error(message);
    },
  });

  function submit() {
    setSubmitted(true);
    setFormError(null);
    if (!amountValid || !withinBalance || !reasonValid) {
      return;
    }
    setConfirmOpen(true);
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
            <DialogTitle>{OPERATION_TITLE[type]}</DialogTitle>
            <DialogDescription>
              {operation
                ? `${operation.row.full_name} · баланс ${formatMoney(operation.row.balance)}`
                : "Операция с депозитом"}
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-4">
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
              ) : submitted && !withinBalance ? (
                <span className="text-sm text-destructive">
                  Сумма больше текущего баланса ({formatMoney(balance)}).
                </span>
              ) : null}
            </Label>

            <Label className="grid gap-2">
              <span>{type === "writeoff" ? "Причина" : "Комментарий"}</span>
              <Textarea
                disabled={mutation.isPending}
                onChange={(event) => setComment(event.target.value)}
                placeholder={type === "writeoff" ? "Укажите причину списания" : "Необязательно"}
                value={comment}
              />
              {submitted && !reasonValid ? (
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
            <Button
              disabled={mutation.isPending}
              onClick={onClose}
              type="button"
              variant="outline"
            >
              Отмена
            </Button>
            <Button disabled={mutation.isPending} onClick={submit} type="button">
              {mutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <Banknote size={16} aria-hidden="true" />
              )}
              {OPERATION_TITLE[type]}
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
                ? `${OPERATION_TITLE[operation.type]} на ${formatMoney(
                    normalized,
                  )} для «${operation.row.full_name}»? Баланс депозита уменьшится.`
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
              Подтвердить
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

function DepositHistoryDialog({
  employee,
  onClose,
}: {
  employee: DepositListItem | null;
  onClose: () => void;
}) {
  const historyQuery = useQuery({
    queryKey: [DEPOSIT_HISTORY_QUERY_KEY, employee?.id],
    queryFn: () => getDepositTransactions(employee!.id),
    enabled: Boolean(employee),
    staleTime: 30_000,
  });

  const transactions = historyQuery.data ?? [];

  return (
    <Dialog
      onOpenChange={(open) => {
        if (!open) {
          onClose();
        }
      }}
      open={Boolean(employee)}
    >
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>История депозита</DialogTitle>
          <DialogDescription>
            {employee ? `${employee.full_name} · баланс ${formatMoney(employee.balance)}` : ""}
          </DialogDescription>
        </DialogHeader>

        {historyQuery.isLoading ? (
          <div className="flex items-center justify-center py-8 text-muted-foreground">
            <LoaderCircle className="h-5 w-5 animate-spin" aria-hidden="true" />
          </div>
        ) : historyQuery.isError ? (
          <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {apiErrorMessage(historyQuery.error, "Не удалось загрузить историю")}
          </div>
        ) : transactions.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted-foreground">История пуста.</p>
        ) : (
          <div className="max-h-[420px] overflow-y-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Дата</TableHead>
                  <TableHead>Тип</TableHead>
                  <TableHead className="text-right">Сумма</TableHead>
                  <TableHead>Комментарий</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {transactions.map((transaction) => (
                  <DepositHistoryRow key={transaction.id} transaction={transaction} />
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function DepositHistoryRow({ transaction }: { transaction: DepositTransaction }) {
  const positive = isPositiveDepositTx(transaction.transaction_type);
  return (
    <TableRow>
      <TableCell className="tabular-nums">{formatDateTime(transaction.created_at)}</TableCell>
      <TableCell>
        <Badge
          className={cn(
            "rounded-md shadow-none",
            positive
              ? "border-emerald-200 bg-emerald-50 text-emerald-800 hover:bg-emerald-50"
              : "border-rose-200 bg-rose-50 text-rose-800 hover:bg-rose-50",
          )}
          variant="outline"
        >
          {depositTxLabel(transaction.transaction_type)}
        </Badge>
      </TableCell>
      <TableCell
        className={cn(
          "text-right font-medium tabular-nums",
          positive ? "text-emerald-700" : "text-rose-700",
        )}
      >
        {positive ? "+" : "−"}
        {formatMoney(transaction.amount)}
      </TableCell>
      <TableCell className="text-sm text-muted-foreground">
        {transaction.comment || transaction.reason || "—"}
      </TableCell>
    </TableRow>
  );
}

function SummaryCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border bg-card p-4">
      <div className="text-sm text-muted-foreground">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums">{value}</div>
    </div>
  );
}

function ProgressBar({ value }: { value: number }) {
  return (
    <div className="h-2 overflow-hidden rounded-full bg-muted">
      <div className="h-full rounded-full bg-primary" style={{ width: `${value}%` }} />
    </div>
  );
}

function DepositsTableSkeleton() {
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
