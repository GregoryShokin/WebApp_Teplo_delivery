import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, LoaderCircle, Save, WalletCards } from "lucide-react";
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
  apiErrorMessage,
  getDepositTransactions,
  patchDepositConfig,
  postDepositPayout,
  postDepositWriteoff,
  type DepositConfigPatch,
  type DepositListItem,
  type Employee,
} from "@/lib/api";
import { cn } from "@/lib/utils";

import {
  depositRuleValue,
  depositSourceLabel,
  formatDate,
  formatDateTime,
  formatMoney,
  formatPercentValue,
  inferredOverrideValue,
  isDepositTargetPosition,
  normalizeDecimalInput,
  progressValue,
  transactionTypeLabel,
  validNonNegativeDecimalInput,
  type DepositRulesByKey,
} from "./deposit-utils";

type EmployeeDepositSectionProps = {
  deposit: DepositListItem | null | undefined;
  employee: Employee;
  isLoading: boolean;
  rules: DepositRulesByKey;
};

type DepositConfigDraft = {
  targetOverride: string;
  withholdingOverride: string;
  excluded: boolean;
  excludedUntil: string;
  excludedReason: string;
};

export function EmployeeDepositSection({
  deposit,
  employee,
  isLoading,
  rules,
}: EmployeeDepositSectionProps) {
  const [dialogOpen, setDialogOpen] = useState(false);
  if (!isDepositTargetPosition(employee.position)) {
    return null;
  }

  return (
    <div className="grid gap-3 rounded-lg border bg-card p-4">
      <div className="text-sm font-medium">Депозит</div>
      {isLoading ? (
        <div className="rounded-md border border-dashed px-3 py-4 text-sm text-muted-foreground">
          Загрузка депозита...
        </div>
      ) : deposit ? (
        <>
          <div className="rounded-md border bg-background px-3 py-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="text-sm text-muted-foreground">Текущий баланс</div>
              <div className="font-semibold tabular-nums">
                {formatMoney(deposit.balance)} / цель {formatMoney(deposit.target)}
              </div>
            </div>
            <DepositBalanceBreakdown className="mt-2" deposit={deposit} />
            <ProgressBar value={progressValue(deposit.progress_pct)} className="mt-3" />
          </div>
          {deposit.is_excluded ? (
            <div className="grid gap-1 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
              <Badge className="w-fit rounded-md border-amber-200 bg-amber-100 text-amber-800 shadow-none">
                Исключён из удержания
              </Badge>
              <span>
                {deposit.excluded_until ? `до ${formatDate(deposit.excluded_until)}` : "бессрочно"}
              </span>
            </div>
          ) : null}
          <Button
            className="w-fit"
            onClick={() => setDialogOpen(true)}
            type="button"
            variant="outline"
          >
            <WalletCards size={16} aria-hidden="true" />
            Индивидуальный депозит
          </Button>
        </>
      ) : (
        <div className="rounded-md border border-dashed px-3 py-4 text-sm text-muted-foreground">
          Данные депозита появятся после синхронизации.
        </div>
      )}

      <IndividualDepositDialog
        deposit={deposit ?? null}
        employeeName={employee.full_name}
        onOpenChange={setDialogOpen}
        open={dialogOpen}
        rules={rules}
      />
    </div>
  );
}

function IndividualDepositDialog({
  deposit,
  employeeName,
  onOpenChange,
  open,
  rules,
}: {
  deposit: DepositListItem | null;
  employeeName: string;
  onOpenChange: (open: boolean) => void;
  open: boolean;
  rules: DepositRulesByKey;
}) {
  const queryClient = useQueryClient();
  const allowCloseRef = useRef(false);
  const [draft, setDraft] = useState<DepositConfigDraft>(() => emptyDraft());
  const [initialDraft, setInitialDraft] = useState<DepositConfigDraft>(() => emptyDraft());
  const [discardOpen, setDiscardOpen] = useState(false);
  const [dangerOpen, setDangerOpen] = useState(false);
  const [payoutOpen, setPayoutOpen] = useState(false);
  const [writeoffAmount, setWriteoffAmount] = useState("");
  const [writeoffReason, setWriteoffReason] = useState("");
  const employeeId = deposit?.id ?? "";
  const defaultTarget = depositRuleValue(rules, deposit?.category, "deposit_target");
  const defaultWithholding = depositRuleValue(rules, deposit?.category, "deposit_withholding");
  const balance = Number(deposit?.balance ?? 0);
  const dirty = JSON.stringify(draft) !== JSON.stringify(initialDraft);
  const numbersValid =
    validNonNegativeDecimalInput(draft.targetOverride) &&
    validNonNegativeDecimalInput(draft.withholdingOverride);
  const reasonValid = draft.excludedReason.length <= 500;
  const canSave = Boolean(deposit) && dirty && numbersValid && reasonValid;
  const writeoffValid =
    validNonNegativeDecimalInput(writeoffAmount) &&
    Number(normalizeDecimalInput(writeoffAmount)) > 0 &&
    writeoffReason.trim().length > 0;

  useEffect(() => {
    if (!open || !deposit) {
      return;
    }
    const nextDraft = {
      targetOverride: inferredOverrideValue(
        deposit.target,
        defaultTarget,
        deposit.deposit_target_override,
      ),
      withholdingOverride: inferredOverrideValue(
        deposit.withholding,
        defaultWithholding,
        deposit.deposit_withholding_override,
      ),
      excluded: deposit.is_excluded,
      excludedUntil: deposit.excluded_until ?? "",
      excludedReason: deposit.deposit_excluded_reason ?? "",
    };
    setDraft(nextDraft);
    setInitialDraft(nextDraft);
    setDangerOpen(false);
    setPayoutOpen(false);
    setWriteoffAmount("");
    setWriteoffReason("");
  }, [defaultTarget, defaultWithholding, deposit, open]);

  const transactionsQuery = useQuery({
    queryKey: ["deposits", employeeId, "transactions"],
    queryFn: () => getDepositTransactions(employeeId),
    enabled: open && Boolean(employeeId),
  });

  const configMutation = useMutation({
    mutationFn: (payload: DepositConfigPatch) => patchDepositConfig(employeeId, payload),
    onSuccess: async () => {
      toast.success("Индивидуальный депозит сохранён");
      await queryClient.invalidateQueries({ queryKey: ["deposits"] });
      allowCloseRef.current = true;
      onOpenChange(false);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось сохранить депозит"));
    },
  });

  const payoutMutation = useMutation({
    mutationFn: () =>
      postDepositPayout(employeeId, {
        amount: normalizeDecimalInput(deposit?.balance ?? "0"),
        comment: "Ручная выплата",
      }),
    onSuccess: async () => {
      toast.success("Остаток выплачен");
      setPayoutOpen(false);
      await invalidateDepositQueries(queryClient, employeeId);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось выплатить остаток"));
    },
  });

  const writeoffMutation = useMutation({
    mutationFn: () =>
      postDepositWriteoff(employeeId, {
        amount: normalizeDecimalInput(writeoffAmount),
        reason: writeoffReason.trim(),
      }),
    onSuccess: async () => {
      toast.success("Списание создано");
      setWriteoffAmount("");
      setWriteoffReason("");
      await invalidateDepositQueries(queryClient, employeeId);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось списать депозит"));
    },
  });

  const sortedTransactions = useMemo(
    () =>
      [...(transactionsQuery.data ?? [])].sort((left, right) =>
        String(right.created_at ?? "").localeCompare(String(left.created_at ?? "")),
      ),
    [transactionsQuery.data],
  );

  function handleOpenChange(nextOpen: boolean) {
    if (!nextOpen && dirty && !allowCloseRef.current) {
      setDiscardOpen(true);
      return;
    }
    if (!nextOpen) {
      allowCloseRef.current = false;
    }
    onOpenChange(nextOpen);
  }

  function saveConfig() {
    if (!canSave) {
      return;
    }
    const payload = buildPatch(draft, initialDraft);
    if (Object.keys(payload).length === 0) {
      return;
    }
    configMutation.mutate(payload);
  }

  return (
    <>
      <Dialog onOpenChange={handleOpenChange} open={open}>
        <DialogContent className="stage-manager-dialog max-h-[92vh] max-w-4xl overflow-y-auto p-0 sm:rounded-xl">
          <div className="grid gap-5 p-5 sm:p-6">
            <DialogHeader className="pr-8">
              <DialogTitle>Индивидуальный депозит</DialogTitle>
              <DialogDescription>{employeeName}</DialogDescription>
            </DialogHeader>

            {deposit ? (
              <>
                <section className="grid gap-3 rounded-lg border bg-muted/30 p-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                      <div className="text-sm text-muted-foreground">Текущее состояние</div>
                      <div className="mt-1 text-xl font-semibold tabular-nums">
                        Баланс: {formatMoney(deposit.balance)}
                      </div>
                      <DepositBalanceBreakdown className="mt-2" deposit={deposit} />
                    </div>
                    <Badge variant="secondary">{depositSourceLabel(deposit, rules)}</Badge>
                  </div>
                  <ProgressBar value={progressValue(deposit.progress_pct)} />
                  <div className="text-sm text-muted-foreground">
                    {formatPercentValue(deposit.progress_pct)}% к цели {formatMoney(deposit.target)}
                  </div>
                </section>

                <section className="grid gap-4 rounded-lg border p-4">
                  <div className="grid gap-3 sm:grid-cols-2">
                    <Label className="grid gap-2">
                      <span>Индивидуальная цель депозита</span>
                      <Input
                        min={0}
                        onChange={(event) =>
                          setDraft((current) => ({
                            ...current,
                            targetOverride: event.target.value,
                          }))
                        }
                        placeholder={`Категория-default: ${formatMoney(defaultTarget)}`}
                        type="number"
                        value={draft.targetOverride}
                      />
                    </Label>
                    <Label className="grid gap-2">
                      <span>Индивидуальная сумма удержания</span>
                      <Input
                        min={0}
                        onChange={(event) =>
                          setDraft((current) => ({
                            ...current,
                            withholdingOverride: event.target.value,
                          }))
                        }
                        placeholder={`Категория-default: ${formatMoney(defaultWithholding)}`}
                        type="number"
                        value={draft.withholdingOverride}
                      />
                    </Label>
                  </div>

                  <div className="grid gap-3 rounded-md border bg-background px-3 py-3">
                    <label className="flex items-center justify-between gap-3 text-sm font-medium">
                      <span>Исключить из удержания</span>
                      <input
                        checked={draft.excluded}
                        onChange={(event) =>
                          setDraft((current) => ({
                            ...current,
                            excluded: event.target.checked,
                          }))
                        }
                        type="checkbox"
                      />
                    </label>

                    {draft.excluded ? (
                      <div className="grid gap-3 sm:grid-cols-2">
                        <Label className="grid gap-2">
                          <span>Исключить до даты</span>
                          <Input
                            onChange={(event) =>
                              setDraft((current) => ({
                                ...current,
                                excludedUntil: event.target.value,
                              }))
                            }
                            type="date"
                            value={draft.excludedUntil}
                          />
                        </Label>
                        <Label className="grid gap-2 sm:col-span-2">
                          <span>Причина исключения</span>
                          <textarea
                            className="min-h-24 rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                            maxLength={500}
                            onChange={(event) =>
                              setDraft((current) => ({
                                ...current,
                                excludedReason: event.target.value,
                              }))
                            }
                            value={draft.excludedReason}
                          />
                        </Label>
                      </div>
                    ) : null}
                  </div>

                  <p className="text-sm leading-6 text-muted-foreground">
                    Индивидуальные значения приоритетнее категории. Удержание не происходит, если
                    достигнут целевой депозит.
                  </p>
                  {!numbersValid ? (
                    <div className="text-sm text-destructive">
                      Проверьте суммы: нужны числа от 0.
                    </div>
                  ) : null}
                  {!reasonValid ? (
                    <div className="text-sm text-destructive">
                      Причина исключения не должна превышать 500 символов.
                    </div>
                  ) : null}
                </section>

                <section className="rounded-lg border">
                  <button
                    className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left text-sm font-medium"
                    onClick={() => setDangerOpen((value) => !value)}
                    type="button"
                  >
                    Опасные действия
                    <ChevronDown
                      className={cn("transition-transform", dangerOpen && "rotate-180")}
                      size={16}
                      aria-hidden="true"
                    />
                  </button>
                  {dangerOpen ? (
                    <div className="grid gap-4 border-t p-4">
                      <Button
                        className="w-fit"
                        disabled={balance <= 0 || payoutMutation.isPending}
                        onClick={() => setPayoutOpen(true)}
                        type="button"
                        variant="destructive"
                      >
                        Выплатить остаток
                      </Button>
                      <div className="grid gap-3 sm:grid-cols-[180px_1fr_auto] sm:items-end">
                        <Label className="grid gap-2">
                          <span>Сумма</span>
                          <Input
                            min={0}
                            onChange={(event) => setWriteoffAmount(event.target.value)}
                            type="number"
                            value={writeoffAmount}
                          />
                        </Label>
                        <Label className="grid gap-2">
                          <span>Причина</span>
                          <Input
                            onChange={(event) => setWriteoffReason(event.target.value)}
                            value={writeoffReason}
                          />
                        </Label>
                        <Button
                          disabled={!writeoffValid || writeoffMutation.isPending}
                          onClick={() => writeoffMutation.mutate()}
                          type="button"
                          variant="destructive"
                        >
                          Списать
                        </Button>
                      </div>
                    </div>
                  ) : null}
                </section>

                <section className="space-y-3 rounded-lg border p-4">
                  <h3 className="font-semibold tracking-normal">История транзакций</h3>
                  <div className="overflow-x-auto">
                    <table className="min-w-[620px] border-separate border-spacing-0 text-sm">
                      <thead>
                        <tr>
                          <th className="border-b p-3 text-left font-medium text-muted-foreground">
                            Дата
                          </th>
                          <th className="border-b p-3 text-left font-medium text-muted-foreground">
                            Тип
                          </th>
                          <th className="border-b p-3 text-right font-medium text-muted-foreground">
                            Сумма
                          </th>
                          <th className="border-b p-3 text-left font-medium text-muted-foreground">
                            Комментарий/причина
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {sortedTransactions.map((transaction) => (
                          <tr key={transaction.id}>
                            <td className="border-b p-3">
                              {formatDateTime(transaction.created_at)}
                            </td>
                            <td className="border-b p-3">
                              {transactionTypeLabel(transaction.transaction_type)}
                            </td>
                            <td className="border-b p-3 text-right tabular-nums">
                              {formatMoney(transaction.amount)}
                            </td>
                            <td className="border-b p-3 text-muted-foreground">
                              {transaction.comment ?? transaction.reason ?? "—"}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  {transactionsQuery.isLoading ? (
                    <EmptyLine text="Загрузка истории..." />
                  ) : sortedTransactions.length === 0 ? (
                    <EmptyLine text="История пуста" />
                  ) : null}
                </section>

                <DialogFooter>
                  <Button
                    disabled={configMutation.isPending}
                    onClick={() => handleOpenChange(false)}
                    type="button"
                    variant="outline"
                  >
                    Отмена
                  </Button>
                  <Button disabled={!canSave || configMutation.isPending} onClick={saveConfig}>
                    {configMutation.isPending ? (
                      <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                    ) : (
                      <Save size={16} aria-hidden="true" />
                    )}
                    Сохранить
                  </Button>
                </DialogFooter>
              </>
            ) : (
              <EmptyLine text="Данные депозита не загружены." />
            )}
          </div>
        </DialogContent>
      </Dialog>

      <AlertDialog open={discardOpen} onOpenChange={setDiscardOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Закрыть без сохранения?</AlertDialogTitle>
            <AlertDialogDescription>
              Изменения индивидуального депозита будут потеряны.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Отмена</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                setDiscardOpen(false);
                allowCloseRef.current = true;
                onOpenChange(false);
              }}
            >
              Закрыть
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={payoutOpen} onOpenChange={setPayoutOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Выплатить остаток?</AlertDialogTitle>
            <AlertDialogDescription>
              Выплатить {formatMoney(deposit?.balance)} сотруднику {employeeName}? Депозит
              обнулится.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={payoutMutation.isPending}>Отмена</AlertDialogCancel>
            <AlertDialogAction
              disabled={payoutMutation.isPending || balance <= 0}
              onClick={(event) => {
                event.preventDefault();
                payoutMutation.mutate();
              }}
            >
              Выплатить
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

function DepositBalanceBreakdown({
  className,
  deposit,
}: {
  className?: string;
  deposit: DepositListItem;
}) {
  const historicalBalance = numericValue(deposit.initial_balance);
  const accumulatedBalance = numericValue(deposit.balance) - historicalBalance;
  return (
    <div className={cn("text-xs leading-5 text-muted-foreground", className)}>
      из них исторический: {formatMoney(historicalBalance)}; накоплено приложением:{" "}
      {formatMoney(accumulatedBalance)}
    </div>
  );
}

function ProgressBar({ className, value }: { className?: string; value: number }) {
  return (
    <div className={cn("h-2 overflow-hidden rounded-full bg-muted", className)}>
      <div
        className="h-full rounded-full bg-primary transition-all"
        style={{ width: `${progressValue(value)}%` }}
      />
    </div>
  );
}

function EmptyLine({ text }: { text: string }) {
  return (
    <div className="rounded-md border border-dashed px-3 py-6 text-center text-sm text-muted-foreground">
      {text}
    </div>
  );
}

function emptyDraft(): DepositConfigDraft {
  return {
    targetOverride: "",
    withholdingOverride: "",
    excluded: false,
    excludedUntil: "",
    excludedReason: "",
  };
}

function buildPatch(draft: DepositConfigDraft, initialDraft: DepositConfigDraft) {
  const payload: DepositConfigPatch = {};
  if (draft.targetOverride !== initialDraft.targetOverride) {
    payload.deposit_target_override = decimalOverrideValue(draft.targetOverride);
  }
  if (draft.withholdingOverride !== initialDraft.withholdingOverride) {
    payload.deposit_withholding_override = decimalOverrideValue(draft.withholdingOverride);
  }
  if (draft.excluded !== initialDraft.excluded) {
    payload.deposit_excluded = draft.excluded;
    if (!draft.excluded) {
      payload.deposit_excluded_until = null;
      payload.deposit_excluded_reason = null;
    }
  }
  if (draft.excluded && draft.excludedUntil !== initialDraft.excludedUntil) {
    payload.deposit_excluded_until = draft.excludedUntil || null;
  }
  if (draft.excluded && draft.excludedReason !== initialDraft.excludedReason) {
    payload.deposit_excluded_reason = draft.excludedReason.trim() || null;
  }
  return payload;
}

function decimalOverrideValue(value: string) {
  const normalized = normalizeDecimalInput(value);
  return normalized === "" ? null : normalized;
}

function numericValue(value: string | number | null | undefined) {
  const amount = Number(value);
  return Number.isFinite(amount) ? amount : 0;
}

async function invalidateDepositQueries(
  queryClient: ReturnType<typeof useQueryClient>,
  employeeId: string,
) {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: ["deposits"] }),
    queryClient.invalidateQueries({ queryKey: ["deposits", employeeId, "transactions"] }),
  ]);
}
