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
  getScheduledPayoutEnabled,
  patchDepositConfig,
  postDepositPayout,
  postDepositWriteoff,
  scheduleDepositPayout,
  type DepositConfigPatch,
  type DepositListItem,
  type DepositPayoutMethod,
  type Employee,
} from "@/lib/api";
import { usePermissions } from "@/lib/permissions";
import { cn } from "@/lib/utils";

import {
  depositRuleValue,
  depositSourceLabel,
  formatDate,
  formatDateTime,
  formatMoney,
  formatMoneyPrecise,
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
  const [surplusOpen, setSurplusOpen] = useState(false);
  if (!isDepositTargetPosition(employee.position)) {
    return null;
  }
  // Излишек над целью (например после понижения индивидуальной цели) — долг перед сотрудником.
  const surplus = Number(deposit?.surplus ?? 0);

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
                {formatMoneyPrecise(deposit.balance)} / цель {formatMoney(deposit.target)}
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
          {surplus > 0 ? (
            <div className="grid gap-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
              <Badge className="w-fit rounded-md border-amber-200 bg-amber-100 text-amber-800 shadow-none">
                Излишек {formatMoneyPrecise(deposit.surplus)}
              </Badge>
              <span>Собрано больше цели — долг перед сотрудником, его нужно выдать.</span>
              <Button
                className="w-fit"
                onClick={() => setSurplusOpen(true)}
                size="sm"
                type="button"
                variant="outline"
              >
                Выдать излишек
              </Button>
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
        onSurplusDetected={() => setSurplusOpen(true)}
        open={dialogOpen}
        rules={rules}
      />

      <SurplusPayoutDialog
        deposit={deposit ?? null}
        employeeName={employee.full_name}
        onOpenChange={setSurplusOpen}
        open={surplusOpen}
      />
    </div>
  );
}

function IndividualDepositDialog({
  deposit,
  employeeName,
  onOpenChange,
  onSurplusDetected,
  open,
  rules,
}: {
  deposit: DepositListItem | null;
  employeeName: string;
  onOpenChange: (open: boolean) => void;
  onSurplusDetected: () => void;
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
  // «Пол» индивидуального депозита: цель ниже дефолта категории — только с отдельным правом.
  const canBelowCategory = usePermissions().hasPermission(
    "payroll.production_deposits.target_below_category",
  );
  const targetChanged = draft.targetOverride !== initialDraft.targetOverride;
  const normalizedTarget = normalizeDecimalInput(draft.targetOverride);
  const defaultTargetNumber = Number(defaultTarget ?? Number.NaN);
  const targetBelowDefault =
    targetChanged &&
    normalizedTarget !== "" &&
    Number.isFinite(defaultTargetNumber) &&
    defaultTargetNumber > 0 &&
    Number(normalizedTarget) < defaultTargetNumber;
  const floorBlocked = targetBelowDefault && !canBelowCategory;
  const canSave = Boolean(deposit) && dirty && numbersValid && reasonValid && !floorBlocked;
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
      // Понизили цель ниже уже собранного → появился излишек (долг перед сотрудником):
      // сразу открываем окно выдачи (ведомость/счёт).
      const newTargetNumber =
        normalizedTarget !== "" ? Number(normalizedTarget) : defaultTargetNumber;
      const surplusAfterSave =
        targetChanged && Number.isFinite(newTargetNumber) ? balance - newTargetNumber : 0;
      await queryClient.invalidateQueries({ queryKey: ["deposits"] });
      allowCloseRef.current = true;
      onOpenChange(false);
      if (surplusAfterSave > 0) {
        onSurplusDetected();
      }
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
                        Баланс: {formatMoneyPrecise(deposit.balance)}
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
                  {floorBlocked ? (
                    <div className="text-sm text-destructive">
                      Индивидуальная цель ниже дефолта категории (
                      {formatMoney(defaultTarget)}). Сохранение заблокировано — нужно право
                      «Ставить индивидуальную цель депозита ниже дефолта категории».
                    </div>
                  ) : targetBelowDefault ? (
                    <div className="text-sm text-amber-700">
                      Цель ниже дефолта категории ({formatMoney(defaultTarget)}) — будет
                      применена по вашему праву. Если собрано больше новой цели, излишек
                      нужно будет выдать.
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
                              {formatMoneyPrecise(transaction.amount)}
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
              Выплатить {formatMoneyPrecise(deposit?.balance)} сотруднику {employeeName}? Депозит
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
      из них исторический: {formatMoneyPrecise(historicalBalance)}; накоплено приложением:{" "}
      {formatMoneyPrecise(accumulatedBalance)}
    </div>
  );
}

// Выдача излишка депозита («долг» после понижения индивидуальной цели): в ближайшую
// ведомость (отложенный план) или сразу через счёт по общим правилам выплат — ТК Черникова
// (+iiko), Сейф, банк-черновик Т-Банк/Сбер (транзит на Сейф + черновик + проводка в ДДС).
function SurplusPayoutDialog({
  deposit,
  employeeName,
  onOpenChange,
  open,
}: {
  deposit: DepositListItem | null;
  employeeName: string;
  onOpenChange: (open: boolean) => void;
  open: boolean;
}) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const canPayout = permissions.hasPermission("payroll.production_deposits.payout");
  const channelPerms = {
    cash_tk: permissions.hasPermission("finance.payout_channel.cash_tk"),
    cash_safe: permissions.hasPermission("finance.payout_channel.safe"),
    bank_draft: permissions.hasPermission("finance.payout_channel.bank_draft"),
  };
  const scheduledQuery = useQuery({
    queryKey: ["deposits", "scheduled-payout-enabled"],
    queryFn: getScheduledPayoutEnabled,
    enabled: open,
  });
  const scheduledEnabled = scheduledQuery.data ?? false;
  const [mode, setMode] = useState<"scheduled" | "immediate">("scheduled");
  const [method, setMethod] = useState<DepositPayoutMethod>("cash_tk");

  const employeeId = deposit?.id ?? "";
  const surplusAmount = normalizeDecimalInput(deposit?.surplus ?? "0");
  const surplusNumber = Number(surplusAmount);
  const hasSurplus = Number.isFinite(surplusNumber) && surplusNumber > 0;
  const allowedChannels = (
    [
      ["cash_tk", channelPerms.cash_tk],
      ["cash_safe", channelPerms.cash_safe],
      ["bank_draft", channelPerms.bank_draft],
    ] as const
  )
    .filter(([, ok]) => ok)
    .map(([key]) => key);

  useEffect(() => {
    if (!open) {
      return;
    }
    setMode(scheduledEnabled ? "scheduled" : "immediate");
    setMethod(allowedChannels.includes("cash_tk") ? "cash_tk" : (allowedChannels[0] ?? "cash_tk"));
    // Каналы зависят только от прав — на открытие диалога достаточно scheduledEnabled.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, scheduledEnabled]);

  const mutation = useMutation({
    mutationFn: async () => {
      if (mode === "scheduled") {
        return scheduleDepositPayout(employeeId, { amount: surplusAmount });
      }
      return postDepositPayout(employeeId, {
        amount: surplusAmount,
        comment: "Выдача излишка депозита",
        payout_method: method,
      });
    },
    onSuccess: async () => {
      toast.success(
        mode === "scheduled"
          ? "Излишек включён в ближайшую ведомость"
          : "Излишек выдан",
      );
      await invalidateDepositQueries(queryClient, employeeId);
      onOpenChange(false);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось выдать излишек"));
    },
  });

  const immediatePossible = allowedChannels.length > 0;
  const canSubmit =
    Boolean(deposit) &&
    hasSurplus &&
    canPayout &&
    (mode === "scheduled" ? scheduledEnabled : immediatePossible);

  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Излишек депозита</DialogTitle>
          <DialogDescription>
            У {employeeName} собрано больше текущей цели. Излишек{" "}
            {formatMoney(deposit?.surplus)} — долг перед сотрудником, выберите, как его выдать.
          </DialogDescription>
        </DialogHeader>

        {!canPayout ? (
          <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
            У вас нет права на выдачу депозитов — излишек останется подсвеченным, выдать его
            сможет пользователь с правом «Выдавать депозиты производственного персонала».
          </div>
        ) : (
          <div className="grid gap-3">
            <label className="flex items-start gap-3 rounded-md border px-3 py-2 text-sm">
              <input
                checked={mode === "scheduled"}
                disabled={!scheduledEnabled || mutation.isPending}
                name="surplus-mode"
                onChange={() => setMode("scheduled")}
                type="radio"
              />
              <span className="grid gap-1">
                <span className="font-medium">Включить в ближайшую ведомость</span>
                <span className="text-muted-foreground">
                  Сумма попадёт в столбец «Выдача депозита» и будет выплачена вместе с ЗП.
                  {!scheduledEnabled ? " (Отложенная выдача выключена в настройках.)" : null}
                </span>
              </span>
            </label>
            <label className="flex items-start gap-3 rounded-md border px-3 py-2 text-sm">
              <input
                checked={mode === "immediate"}
                disabled={!immediatePossible || mutation.isPending}
                name="surplus-mode"
                onChange={() => setMode("immediate")}
                type="radio"
              />
              <span className="grid w-full gap-2">
                <span className="font-medium">Выдать сейчас через счёт</span>
                {mode === "immediate" ? (
                  <>
                    <select
                      className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm disabled:opacity-50"
                      disabled={mutation.isPending}
                      onChange={(event) => setMethod(event.target.value as DepositPayoutMethod)}
                      value={method}
                    >
                      {allowedChannels.includes("cash_tk") ? (
                        <option value="cash_tk">Торговая касса Черникова</option>
                      ) : null}
                      {allowedChannels.includes("cash_safe") ? (
                        <option value="cash_safe">Сейф</option>
                      ) : null}
                      {allowedChannels.includes("bank_draft") ? (
                        <option value="bank_draft">Банк-черновик Т-Банк (через Сейф)</option>
                      ) : null}
                      {allowedChannels.includes("bank_draft") ? (
                        <option value="bank_draft_sber">Сбербанк → Сейф (черновик)</option>
                      ) : null}
                    </select>
                    <span className="text-xs text-muted-foreground">
                      {method === "cash_tk"
                        ? "Наличные из кассы Черникова + изъятие в iiko."
                        : method === "cash_safe"
                          ? "Наличные с карты «Сейф». Изъятие в iiko не делается."
                          : "Перевод р/с → Сейф, черновик платежа и проводка в ДДС после оплаты."}
                    </span>
                  </>
                ) : null}
                {!immediatePossible ? (
                  <span className="text-muted-foreground">Нет прав ни на один счёт выдачи.</span>
                ) : null}
              </span>
            </label>
          </div>
        )}

        <DialogFooter>
          <Button
            disabled={mutation.isPending}
            onClick={() => onOpenChange(false)}
            type="button"
            variant="outline"
          >
            Позже
          </Button>
          {canPayout ? (
            <Button disabled={!canSubmit || mutation.isPending} onClick={() => mutation.mutate()}>
              {mutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : null}
              {mode === "scheduled" ? "В ведомость" : "Выдать"}
            </Button>
          ) : null}
        </DialogFooter>
      </DialogContent>
    </Dialog>
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
