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
  cancelScheduledDepositPayout,
  getDeposits,
  getDepositTransactions,
  getScheduledPayoutEnabled,
  postDepositPayout,
  postDepositWriteoff,
  scheduleDepositPayout,
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
  // Гранулярные права: выдача и списание депозита — отдельно.
  const canPayout = permissions.hasPermission("payroll.production_deposits.payout");
  const canWriteoff = permissions.hasPermission("payroll.production_deposits.write_off");
  const canWrite = canPayout || canWriteoff;
  // Права на каналы немедленной выдачи.
  const channelPerms = {
    cash_tk: permissions.hasPermission("finance.payout_channel.cash_tk"),
    cash_safe: permissions.hasPermission("finance.payout_channel.safe"),
    bank_draft: permissions.hasPermission("finance.payout_channel.bank_draft"),
  };
  const [categoryFilter, setCategoryFilter] = useState<CategoryFilter>("all");
  const [search, setSearch] = useState("");
  const [pendingOperation, setPendingOperation] = useState<PendingOperation | null>(null);
  const [historyEmployee, setHistoryEmployee] = useState<DepositListItem | null>(null);

  const depositsQuery = useQuery({
    queryKey: [DEPOSITS_QUERY_KEY],
    queryFn: getDeposits,
    staleTime: 60_000,
  });

  const scheduledEnabledQuery = useQuery({
    queryKey: ["deposit-scheduled-enabled"],
    queryFn: getScheduledPayoutEnabled,
    staleTime: 5 * 60_000,
  });
  const scheduledEnabled = scheduledEnabledQuery.data ?? false;

  const cancelScheduleMutation = useMutation({
    mutationFn: (employeeId: string) => cancelScheduledDepositPayout(employeeId),
    onSuccess: async () => {
      toast.success("Запланированная выдача отменена");
      await queryClient.invalidateQueries({ queryKey: [DEPOSITS_QUERY_KEY] });
    },
    onError: (error) =>
      toast.error(apiErrorMessage(error, "Не удалось отменить выдачу")),
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
                      {row.scheduled_payout_pending ? (
                        <Badge
                          className="mt-1 rounded-md border-sky-200 bg-sky-50 text-sky-800 hover:bg-sky-50"
                          variant="outline"
                        >
                          Выдача в ближайшей ведомости
                          {row.scheduled_payout_amount
                            ? ` · ${formatMoney(row.scheduled_payout_amount)}`
                            : " · весь остаток"}
                        </Badge>
                      ) : null}
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
                      {Number(row.surplus ?? 0) > 0 ? (
                        <Badge
                          className="ml-auto mt-1 block w-fit rounded-md border-amber-200 bg-amber-50 text-amber-800 hover:bg-amber-50"
                          variant="outline"
                        >
                          Излишек {formatMoney(row.surplus)}
                        </Badge>
                      ) : null}
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
                              {canPayout ? (
                                <DropdownMenuItem
                                  onClick={() => setPendingOperation({ row, type: "payout" })}
                                >
                                  Выдать депозит
                                </DropdownMenuItem>
                              ) : null}
                              {canWriteoff ? (
                                <DropdownMenuItem
                                  onClick={() => setPendingOperation({ row, type: "writeoff" })}
                                >
                                  Списать депозит
                                </DropdownMenuItem>
                              ) : null}
                              {canPayout && row.scheduled_payout_pending ? (
                                <DropdownMenuItem
                                  disabled={cancelScheduleMutation.isPending}
                                  onClick={() => cancelScheduleMutation.mutate(row.id)}
                                >
                                  Отменить запланированную выдачу
                                </DropdownMenuItem>
                              ) : null}
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
        channelPerms={channelPerms}
        onClose={() => setPendingOperation(null)}
        operation={pendingOperation}
        queryClient={queryClient}
        scheduledEnabled={scheduledEnabled}
      />

      <DepositHistoryDialog
        employee={historyEmployee}
        onClose={() => setHistoryEmployee(null)}
      />
    </div>
  );
}

function DepositOperationDialog({
  channelPerms,
  onClose,
  operation,
  queryClient,
  scheduledEnabled,
}: {
  channelPerms: { cash_tk: boolean; cash_safe: boolean; bank_draft: boolean };
  onClose: () => void;
  operation: PendingOperation | null;
  queryClient: ReturnType<typeof useQueryClient>;
  scheduledEnabled: boolean;
}) {
  const [amount, setAmount] = useState("");
  const [comment, setComment] = useState("");
  const [payoutMethod, setPayoutMethod] = useState<
    "cash_tk" | "cash_safe" | "bank_draft" | "bank_draft_sber"
  >("cash_tk");
  // Режим выдачи депозита: отложенная (в ближайшей ведомости, по умолчанию при включённом
  // флаге) или немедленная. Удержание/списание режима не имеют.
  const [payoutMode, setPayoutMode] = useState<"scheduled" | "immediate">("scheduled");
  // Наличный режим (этап 4): «выдать сразу» или «завести резерв» (передать в кассу / на Сейф).
  // Только для наличных каналов; банк-каналы всегда идут черновиком.
  const [cashMode, setCashMode] = useState<"immediate" | "reserve">("immediate");
  const [submitted, setSubmitted] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const type = operation?.type ?? "payout";
  const isScheduled = type === "payout" && scheduledEnabled && payoutMode === "scheduled";
  const isCashChannel = payoutMethod === "cash_tk" || payoutMethod === "cash_safe";
  // Наличный резерв доступен только для наличного канала при немедленной (не отложенной) выдаче.
  const useCashReserve =
    type === "payout" && !isScheduled && isCashChannel && cashMode === "reserve";
  // Каналы немедленной выдачи, доступные по правам.
  const allowedChannels = (
    [
      ["cash_tk", channelPerms.cash_tk],
      ["cash_safe", channelPerms.cash_safe],
      ["bank_draft", channelPerms.bank_draft],
    ] as const
  )
    .filter(([, ok]) => ok)
    .map(([key]) => key);
  const balance = operation ? balanceNumber(operation.row) : 0;
  const normalized = normalizeDecimalInput(amount);
  const amountNumber = Number(normalized);
  const amountProvided = normalized.trim().length > 0;
  // В отложенном режиме сумма необязательна (пусто = весь остаток на момент выплаты).
  const amountValid = isScheduled
    ? !amountProvided || (validNonNegativeDecimalInput(normalized) && amountNumber > 0)
    : validNonNegativeDecimalInput(normalized) && amountNumber > 0;
  const withinBalance = !amountProvided || amountNumber <= balance + 1e-9;
  const reasonValid = type !== "writeoff" || comment.trim().length > 0;

  useEffect(() => {
    if (!operation) {
      return;
    }
    // Выдача по умолчанию — весь остаток; списание — пустая сумма.
    setAmount(operation.type === "payout" ? String(balanceNumber(operation.row)) : "");
    setComment("");
    setPayoutMethod(allowedChannels[0] ?? "cash_tk");
    setCashMode("immediate");
    // При включённой отложенной выдаче режим по умолчанию — «в ближайшей ведомости».
    setPayoutMode(scheduledEnabled ? "scheduled" : "immediate");
    setSubmitted(false);
    setConfirmOpen(false);
    setFormError(null);
  }, [operation, scheduledEnabled]);

  const mutation = useMutation({
    mutationFn: async () => {
      if (!operation) {
        throw new Error("Нет операции");
      }
      if (operation.type === "payout") {
        if (isScheduled) {
          // Способ выплаты (банк/наличка) определится при самой выплате ЗП (run-level сплит),
          // поэтому счёт здесь не выбираем и не передаём.
          return scheduleDepositPayout(operation.row.id, {
            amount: amountProvided ? normalized : null,
          });
        }
        return postDepositPayout(operation.row.id, {
          amount: normalized,
          comment: comment.trim() || null,
          payout_method: payoutMethod,
          payout_mode: useCashReserve ? "reserve" : "immediate",
        });
      }
      return postDepositWriteoff(operation.row.id, {
        amount: normalized,
        reason: comment.trim() || null,
      });
    },
    onSuccess: async () => {
      const isBankDraft = type === "payout" && !isScheduled && payoutMethod.startsWith("bank_draft");
      const reserveMsg =
        payoutMethod === "cash_tk"
          ? "Передано в кассу — выдать во вкладке «К выдаче»"
          : "Резерв на Сейфе создан — выплатить в «Активных платежах»";
      toast.success(
        type === "writeoff"
          ? "Депозит списан"
          : isScheduled
            ? "Выдача запланирована в ближайшей ведомости"
            : useCashReserve
              ? reserveMsg
              : isBankDraft
                ? "Черновик отправлен в банк — платёж появится в «Активных платежах»"
                : "Депозит выдан",
      );
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
            {type === "payout" && scheduledEnabled ? (
              <Label className="grid gap-2">
                <span>Способ выдачи</span>
                <div className="grid grid-cols-2 gap-2">
                  <Button
                    disabled={mutation.isPending}
                    onClick={() => setPayoutMode("scheduled")}
                    type="button"
                    variant={payoutMode === "scheduled" ? "default" : "outline"}
                  >
                    В ближайшей ведомости
                  </Button>
                  <Button
                    disabled={mutation.isPending}
                    onClick={() => setPayoutMode("immediate")}
                    type="button"
                    variant={payoutMode === "immediate" ? "default" : "outline"}
                  >
                    Выдать сразу
                  </Button>
                </div>
                <span className="text-xs text-muted-foreground">
                  {isScheduled
                    ? "Выдача попадёт в столбец «Выдача депозита» ближайшей ведомости и будет выплачена вместе с зарплатой (наличными или по банку — определится при выплате). Удержание продолжит копить депозит заново."
                    : "Немедленная выдача денег прямо сейчас."}
                </span>
              </Label>
            ) : null}

            <Label className="grid gap-2">
              <span>{isScheduled ? "Сумма ₽ (пусто = весь остаток)" : "Сумма ₽"}</span>
              <Input
                disabled={mutation.isPending}
                min={0}
                onChange={(event) => setAmount(event.target.value)}
                placeholder={isScheduled ? "Весь накопленный остаток" : undefined}
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

            {type === "payout" && !isScheduled ? (
              <Label className="grid gap-2">
                <span>Счёт выдачи</span>
                <select
                  className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm disabled:opacity-50"
                  disabled={mutation.isPending}
                  onChange={(event) =>
                    setPayoutMethod(
                      event.target.value as
                        | "cash_tk"
                        | "cash_safe"
                        | "bank_draft"
                        | "bank_draft_sber",
                    )
                  }
                  value={payoutMethod}
                >
                  {allowedChannels.includes("cash_tk") ? (
                    <option value="cash_tk">Торговая касса Черникова</option>
                  ) : null}
                  {allowedChannels.includes("cash_safe") ? (
                    <option value="cash_safe">Сейф</option>
                  ) : null}
                  {allowedChannels.includes("bank_draft") ? (
                    <option value="bank_draft">Банк-черновик (через Сейф)</option>
                  ) : null}
                  {allowedChannels.includes("bank_draft") ? (
                    <option value="bank_draft_sber">Сбербанк → Сейф (черновик)</option>
                  ) : null}
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

            {type === "payout" && !isScheduled && isCashChannel ? (
              <Label className="grid gap-2">
                <span>Как выдать</span>
                <div className="grid grid-cols-2 gap-2">
                  <Button
                    disabled={mutation.isPending}
                    onClick={() => setCashMode("immediate")}
                    type="button"
                    variant={cashMode === "immediate" ? "default" : "outline"}
                  >
                    Выдать сразу
                  </Button>
                  <Button
                    disabled={mutation.isPending}
                    onClick={() => setCashMode("reserve")}
                    type="button"
                    variant={cashMode === "reserve" ? "default" : "outline"}
                  >
                    {payoutMethod === "cash_tk" ? "Передать в кассу" : "Создать резерв"}
                  </Button>
                </div>
                <span className="text-xs text-muted-foreground">
                  {cashMode === "immediate"
                    ? "Деньги выданы прямо сейчас, депозит списывается сразу."
                    : payoutMethod === "cash_tk"
                      ? "Резерв в кассе: появится во вкладке «К выдаче» и в «Активных платежах». Депозит спишется при выдаче."
                      : "Резерв на Сейфе: появится в «Активных платежах». Депозит спишется при выплате."}
                </span>
              </Label>
            ) : null}

            {!isScheduled ? (
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
            ) : null}

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
              {isScheduled ? "Запланировать выдачу" : OPERATION_TITLE[type]}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog onOpenChange={setConfirmOpen} open={confirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Подтвердить операцию?</AlertDialogTitle>
            <AlertDialogDescription>
              {!operation
                ? ""
                : isScheduled
                  ? `Запланировать выдачу депозита ${
                      amountProvided ? `на ${formatMoney(normalized)}` : "(весь остаток)"
                    } для «${operation.row.full_name}» в ближайшей ведомости?`
                  : `${OPERATION_TITLE[operation.type]} на ${formatMoney(
                      normalized,
                    )} для «${operation.row.full_name}»? Баланс депозита уменьшится.`}
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
