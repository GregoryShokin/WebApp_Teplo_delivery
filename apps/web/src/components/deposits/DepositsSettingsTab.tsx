import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Save } from "lucide-react";
import { toast } from "sonner";

import { BooleanWidget } from "@/components/settings-widgets";
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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  apiErrorMessage,
  getDeposits,
  getSettings,
  postDepositInitialBalance,
  updateSetting,
  type DepositListItem,
  type EmployeeCategory,
} from "@/lib/api";
import { EMPLOYEE_CATEGORY_LABELS } from "@/lib/i18n/employee";

import {
  categoryRuleKey,
  depositCategoryOrder,
  depositRuleKeyByCategory,
  extractDepositSettings,
  formatMoney,
  formatPercentValue,
  isDepositTargetPosition,
  normalizeDecimalInput,
  validNonNegativeDecimalInput,
  type DepositRulesByKey,
} from "./deposit-utils";

type DepositsSettingsTabProps = {
  canWrite: boolean;
};

type RuleDraft = {
  category: EmployeeCategory;
  target: string;
  withholding: string;
};

type PendingInitialBalance = {
  employee: DepositListItem;
  amount: string;
};

export function DepositsSettingsTab({ canWrite }: DepositsSettingsTabProps) {
  const queryClient = useQueryClient();
  const settingsQuery = useQuery({
    queryKey: ["settings", "deposits"],
    queryFn: () => getSettings(),
  });
  const depositsQuery = useQuery({
    queryKey: ["deposits"],
    queryFn: getDeposits,
  });
  const depositSettings = useMemo(
    () => extractDepositSettings(settingsQuery.data),
    [settingsQuery.data],
  );
  const [autoEnabled, setAutoEnabled] = useState(false);
  const [initialAutoEnabled, setInitialAutoEnabled] = useState(false);
  const [ruleDrafts, setRuleDrafts] = useState<RuleDraft[]>([]);
  const [initialRuleDrafts, setInitialRuleDrafts] = useState<RuleDraft[]>([]);
  const [initialBalanceInputs, setInitialBalanceInputs] = useState<Record<string, string>>({});
  const [pendingInitialBalance, setPendingInitialBalance] = useState<PendingInitialBalance | null>(
    null,
  );

  useEffect(() => {
    const nextDrafts = buildRuleDrafts(depositSettings.rules);
    setAutoEnabled(depositSettings.autoEnabled);
    setInitialAutoEnabled(depositSettings.autoEnabled);
    setRuleDrafts(nextDrafts);
    setInitialRuleDrafts(nextDrafts);
  }, [depositSettings.autoEnabled, depositSettings.rules]);

  const ruleDraftsValid = ruleDrafts.every(
    (draft) =>
      validNonNegativeDecimalInput(draft.target) && validNonNegativeDecimalInput(draft.withholding),
  );
  const rulesDirty = draftSnapshot(ruleDrafts) !== draftSnapshot(initialRuleDrafts);
  const autoDirty = autoEnabled !== initialAutoEnabled;

  const deposits = useMemo(
    () =>
      [...(depositsQuery.data ?? [])]
        .filter((deposit) => isDepositTargetPosition(deposit.position))
        .sort((left, right) => left.full_name.localeCompare(right.full_name, "ru")),
    [depositsQuery.data],
  );
  const summary = useMemo(() => buildSummary(deposits, autoEnabled), [deposits, autoEnabled]);

  const settingsMutation = useMutation({
    mutationFn: async () => {
      if (autoDirty) {
        await updateSetting("payroll.deposit_auto_withholding_enabled", autoEnabled);
      }
      if (rulesDirty) {
        await updateSetting(
          "payroll.category_rules",
          buildRulesPayload(depositSettings.rules, ruleDrafts),
        );
      }
    },
    onSuccess: async () => {
      toast.success("Правила сохранены");
      await queryClient.invalidateQueries({ queryKey: ["settings"] });
      await queryClient.invalidateQueries({ queryKey: ["settings", "deposits"] });
      await queryClient.invalidateQueries({ queryKey: ["deposits"] });
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось сохранить правила"));
    },
  });

  const saveDisabled =
    !canWrite || (!rulesDirty && !autoDirty) || !ruleDraftsValid || settingsMutation.isPending;

  const initialBalanceMutation = useMutation({
    mutationFn: (payload: PendingInitialBalance) =>
      postDepositInitialBalance(payload.employee.id, normalizeDecimalInput(payload.amount)),
    onSuccess: async (_result, payload) => {
      toast.success("Баланс установлен");
      setInitialBalanceInputs((current) => ({ ...current, [payload.employee.id]: "" }));
      setPendingInitialBalance(null);
      await queryClient.invalidateQueries({ queryKey: ["deposits"] });
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось установить баланс"));
    },
  });

  function updateRuleDraft(
    category: EmployeeCategory,
    field: "target" | "withholding",
    value: string,
  ) {
    const key = categoryRuleKey(category);
    setRuleDrafts((current) =>
      current.map((draft) =>
        categoryRuleKey(draft.category) === key ? { ...draft, [field]: value } : draft,
      ),
    );
  }

  return (
    <div className="space-y-4">
      <DepositSummary summary={summary} />

      <section className="space-y-4 rounded-lg border bg-card p-4">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <h2 className="text-lg font-semibold tracking-normal">Общие правила удержания</h2>
            <p className="mt-1 text-sm leading-6 text-muted-foreground">
              Цели и суммы удержаний по категориям сотрудников.
            </p>
          </div>
          {settingsQuery.isFetching ? <Badge variant="secondary">Обновление</Badge> : null}
        </div>

        <div className="flex items-start justify-between gap-4 rounded-md border bg-background px-3 py-3">
          <div className="min-w-0">
            <Label>Автоматическое удержание депозитов</Label>
            <p className="mt-1 text-sm leading-6 text-muted-foreground">
              Когда включено — при расчёте ЗП у поваров и кассиров автоматически удерживается сумма
              из правил ниже.
            </p>
          </div>
          <BooleanWidget
            disabled={!canWrite || settingsQuery.isLoading || settingsMutation.isPending}
            onChange={(value) => setAutoEnabled(Boolean(value))}
            value={autoEnabled}
          />
        </div>

        <div className="overflow-x-auto">
          <table className="min-w-[620px] border-separate border-spacing-0 text-sm">
            <thead>
              <tr>
                <th className="border-b p-3 text-left font-medium text-muted-foreground">
                  Категория
                </th>
                <th className="border-b p-3 text-left font-medium text-muted-foreground">
                  Целевой депозит
                </th>
                <th className="border-b p-3 text-left font-medium text-muted-foreground">
                  Удержание за расчёт
                </th>
              </tr>
            </thead>
            <tbody>
              {ruleDrafts.map((draft) => (
                <tr key={draft.category}>
                  <td className="border-b p-3 font-medium">
                    <div className="flex flex-wrap items-center gap-2">
                      {draft.category}
                      <span className="text-muted-foreground">
                        {EMPLOYEE_CATEGORY_LABELS[draft.category]}
                      </span>
                      {draft.category === "freelancer" ? (
                        <Badge variant="outline">устар.</Badge>
                      ) : null}
                    </div>
                  </td>
                  <td className="border-b p-2">
                    <Input
                      disabled={!canWrite || settingsMutation.isPending}
                      min={0}
                      onChange={(event) =>
                        updateRuleDraft(draft.category, "target", event.target.value)
                      }
                      type="number"
                      value={draft.target}
                    />
                  </td>
                  <td className="border-b p-2">
                    <Input
                      disabled={!canWrite || settingsMutation.isPending}
                      min={0}
                      onChange={(event) =>
                        updateRuleDraft(draft.category, "withholding", event.target.value)
                      }
                      type="number"
                      value={draft.withholding}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {!ruleDraftsValid ? (
          <div className="text-sm text-destructive">Проверьте суммы: нужны числа от 0.</div>
        ) : null}
        {settingsQuery.isError ? (
          <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {apiErrorMessage(settingsQuery.error, "Не удалось загрузить настройки депозитов")}
          </div>
        ) : null}

        <div className="flex justify-end">
          <Button disabled={saveDisabled} onClick={() => settingsMutation.mutate()}>
            {settingsMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : (
              <Save size={16} aria-hidden="true" />
            )}
            Сохранить
          </Button>
        </div>
      </section>

      <section className="space-y-4 rounded-lg border bg-card p-4">
        <div>
          <h2 className="text-lg font-semibold tracking-normal">Начальные балансы депозитов</h2>
          <p className="mt-1 text-sm leading-6 text-muted-foreground">
            Стартовые суммы для сотрудников целевых должностей.
          </p>
        </div>

        <div className="overflow-x-auto">
          <table className="min-w-[820px] border-separate border-spacing-0 text-sm">
            <thead>
              <tr>
                <th className="border-b p-3 text-left font-medium text-muted-foreground">ФИО</th>
                <th className="border-b p-3 text-left font-medium text-muted-foreground">
                  Должность
                </th>
                <th className="border-b p-3 text-left font-medium text-muted-foreground">
                  Категория
                </th>
                <th className="border-b p-3 text-right font-medium text-muted-foreground">
                  Исторический баланс
                </th>
                <th className="border-b p-3 text-left font-medium text-muted-foreground">
                  Начальный баланс
                </th>
                <th className="border-b p-3 text-right font-medium text-muted-foreground">
                  Действие
                </th>
              </tr>
            </thead>
            <tbody>
              {deposits.map((deposit) => {
                const inputValue = initialBalanceInputs[deposit.id] ?? "";
                const inputValid =
                  inputValue.trim() !== "" && validNonNegativeDecimalInput(inputValue);
                return (
                  <tr key={deposit.id}>
                    <td className="border-b p-3 font-medium">{deposit.full_name}</td>
                    <td className="border-b p-3">{deposit.position}</td>
                    <td className="border-b p-3">
                      {deposit.category ? EMPLOYEE_CATEGORY_LABELS[deposit.category] : "Не задана"}
                    </td>
                    <td className="border-b p-3 text-right tabular-nums">
                      {formatMoney(deposit.initial_balance)}
                    </td>
                    <td className="border-b p-2">
                      <Input
                        disabled={!canWrite || initialBalanceMutation.isPending}
                        min={0}
                        onChange={(event) =>
                          setInitialBalanceInputs((current) => ({
                            ...current,
                            [deposit.id]: event.target.value,
                          }))
                        }
                        placeholder="0"
                        type="number"
                        value={inputValue}
                      />
                    </td>
                    <td className="border-b p-2 text-right">
                      <Button
                        disabled={
                          !canWrite || !inputValid || initialBalanceMutation.isPending
                        }
                        onClick={() =>
                          setPendingInitialBalance({ employee: deposit, amount: inputValue })
                        }
                        size="sm"
                        variant="outline"
                      >
                        Установить
                      </Button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        {depositsQuery.isLoading ? (
          <EmptyLine text="Загрузка сотрудников..." />
        ) : deposits.length === 0 ? (
          <EmptyLine text="Повара и кассиры не найдены." />
        ) : null}
        {depositsQuery.isError ? (
          <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {apiErrorMessage(depositsQuery.error, "Не удалось загрузить депозиты")}
          </div>
        ) : null}
      </section>

      <AlertDialog
        open={Boolean(pendingInitialBalance)}
        onOpenChange={(open) => {
          if (!open) {
            setPendingInitialBalance(null);
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Установить начальный баланс?</AlertDialogTitle>
            <AlertDialogDescription>
              {pendingInitialBalance
                ? `Установить ${formatMoney(pendingInitialBalance.amount)} для ${pendingInitialBalance.employee.full_name}? Это создаст транзакцию accrual с пометкой initial.`
                : ""}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={initialBalanceMutation.isPending}>
              Отмена
            </AlertDialogCancel>
            <AlertDialogAction
              disabled={!pendingInitialBalance || initialBalanceMutation.isPending}
              onClick={(event) => {
                event.preventDefault();
                if (pendingInitialBalance) {
                  initialBalanceMutation.mutate(pendingInitialBalance);
                }
              }}
            >
              Установить
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function DepositSummary({ summary }: { summary: ReturnType<typeof buildSummary> }) {
  const rows = [
    { label: "Сотрудников с включённым удержанием", value: summary.withholdingEnabled },
    { label: "Сумма всех балансов", value: formatMoney(summary.totalBalance) },
    {
      label: "Средний % достижения цели",
      value: `${formatPercentValue(summary.averageProgress)}%`,
    },
    {
      label: "Сотрудников исключённых из удержания",
      value: `${summary.excludedTotal} · до даты ${summary.excludedTemporary} · бессрочно ${summary.excludedPermanent}`,
    },
  ];
  return (
    <section className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
      {rows.map((row) => (
        <div className="rounded-lg border bg-card p-4" key={row.label}>
          <div className="text-sm leading-5 text-muted-foreground">{row.label}</div>
          <div className="mt-2 text-xl font-semibold tabular-nums">{row.value}</div>
        </div>
      ))}
    </section>
  );
}

function EmptyLine({ text }: { text: string }) {
  return (
    <div className="rounded-md border border-dashed px-3 py-6 text-center text-sm text-muted-foreground">
      {text}
    </div>
  );
}

function buildRuleDrafts(rules: DepositRulesByKey): RuleDraft[] {
  const categories = [...depositCategoryOrder];
  if (rules[depositRuleKeyByCategory.freelancer]) {
    categories.push("freelancer");
  }
  return categories.map((category) => {
    const rule = rules[depositRuleKeyByCategory[category]] ?? {};
    return {
      category,
      target: valueToInput(rule.deposit_target),
      withholding: valueToInput(rule.deposit_withholding),
    };
  });
}

function buildRulesPayload(rules: DepositRulesByKey, drafts: RuleDraft[]) {
  const nextRules: DepositRulesByKey = Object.fromEntries(
    Object.entries(rules).map(([key, rule]) => [key, { ...rule }]),
  );
  drafts.forEach((draft) => {
    const key = depositRuleKeyByCategory[draft.category];
    nextRules[key] = {
      ...(nextRules[key] ?? {}),
      deposit_target: decimalPayloadValue(draft.target),
      deposit_withholding: decimalPayloadValue(draft.withholding),
    };
  });
  return nextRules;
}

function buildSummary(deposits: DepositListItem[], autoEnabled: boolean) {
  const totalBalance = deposits.reduce((sum, deposit) => sum + numericValue(deposit.balance), 0);
  const progressSum = deposits.reduce(
    (sum, deposit) => sum + numericValue(deposit.progress_pct),
    0,
  );
  const excluded = deposits.filter((deposit) => deposit.is_excluded);
  return {
    averageProgress: deposits.length > 0 ? progressSum / deposits.length : 0,
    excludedPermanent: excluded.filter((deposit) => !deposit.excluded_until).length,
    excludedTemporary: excluded.filter((deposit) => deposit.excluded_until).length,
    excludedTotal: excluded.length,
    totalBalance,
    withholdingEnabled: autoEnabled
      ? deposits.filter(
          (deposit) =>
            !deposit.is_excluded &&
            numericValue(deposit.withholding) > 0 &&
            numericValue(deposit.balance) < numericValue(deposit.target),
        ).length
      : 0,
  };
}

function draftSnapshot(drafts: RuleDraft[]) {
  return JSON.stringify(
    drafts.map((draft) => ({
      category: draft.category,
      target: normalizeDecimalInput(draft.target),
      withholding: normalizeDecimalInput(draft.withholding),
    })),
  );
}

function valueToInput(value: unknown) {
  if (value === null || value === undefined) {
    return "";
  }
  return String(value);
}

function decimalPayloadValue(value: string) {
  const normalized = normalizeDecimalInput(value);
  return normalized === "" ? null : normalized;
}

function numericValue(value: string | number | null | undefined) {
  const amount = Number(value);
  return Number.isFinite(amount) ? amount : 0;
}
