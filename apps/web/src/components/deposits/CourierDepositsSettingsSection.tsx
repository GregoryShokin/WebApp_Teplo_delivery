import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { History, LoaderCircle, MoreHorizontal, Save, WalletCards } from "lucide-react";
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
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { EmptyState } from "@/components/ui-app/EmptyState";
import {
  apiErrorMessage,
  getCourierDepositSettings,
  getCourierDeposits,
  putCourierDepositOpening,
  putCourierDepositSettings,
  type CourierDepositRow,
  type CourierDepositSettings,
} from "@/lib/api";
import { usePermissions } from "@/lib/permissions";
import { cn } from "@/lib/utils";

import { CourierDepositHistoryDrawer } from "./CourierDepositHistoryDrawer";
import {
  COURIER_CATEGORY_LABELS,
  centsFromRubInput,
  formatCents,
  formatDate,
  formatPercent,
  isNonNegativeRubInput,
  progressValue,
  rubInputFromCents,
  statusLabel,
  todayKey,
} from "./courier-deposit-utils";

type CourierDepositsSettingsSectionProps = {
  canWrite: boolean;
};

type SettingsDraft = {
  targetAmount: string;
  targetAmountSenior: string;
  withholdPrimary: string;
  withholdSecondary: string;
};

type PendingOpeningUpdate = {
  amount: string;
  openingDate: string;
  row: CourierDepositRow;
};

const DEFAULT_SETTINGS: CourierDepositSettings = {
  target_amount: 5000,
  target_amount_senior: 5000,
  withhold_primary: 500,
  withhold_secondary: 200,
  auto_withhold_enabled: false,
};

export function CourierDepositsSettingsSection({
  canWrite,
}: CourierDepositsSettingsSectionProps) {
  const queryClient = useQueryClient();
  const canDeleteTopup = usePermissions().hasPermission("couriers.deposits.delete");
  const [settingsDraft, setSettingsDraft] = useState<SettingsDraft>(() =>
    settingsToDraft(DEFAULT_SETTINGS),
  );
  const [initialSettingsDraft, setInitialSettingsDraft] = useState<SettingsDraft>(() =>
    settingsToDraft(DEFAULT_SETTINGS),
  );
  const [openingDrafts, setOpeningDrafts] = useState<Record<string, string>>({});
  const [openingDateDrafts, setOpeningDateDrafts] = useState<Record<string, string>>({});
  const [pendingOpeningUpdate, setPendingOpeningUpdate] =
    useState<PendingOpeningUpdate | null>(null);
  const [historyCourier, setHistoryCourier] = useState<CourierDepositRow | null>(null);

  const depositsQuery = useQuery({
    queryKey: ["courier-deposits", "settings", "all"],
    queryFn: () => getCourierDeposits({ status: "all", category: "all" }),
    staleTime: 60_000,
  });
  const settingsQuery = useQuery({
    queryKey: ["courier-deposit-settings"],
    queryFn: getCourierDepositSettings,
    staleTime: 60_000,
  });
  const rows = useMemo(
    () =>
      [...(depositsQuery.data ?? [])].sort((left, right) =>
        left.full_name.localeCompare(right.full_name, "ru"),
      ),
    [depositsQuery.data],
  );
  const currentSettings = settingsQuery.data ?? DEFAULT_SETTINGS;
  const settingsValid =
    isNonNegativeRubInput(settingsDraft.targetAmount) &&
    isNonNegativeRubInput(settingsDraft.targetAmountSenior) &&
    isNonNegativeRubInput(settingsDraft.withholdPrimary) &&
    isNonNegativeRubInput(settingsDraft.withholdSecondary);
  const settingsDirty = draftSnapshot(settingsDraft) !== draftSnapshot(initialSettingsDraft);
  const summary = useMemo(() => buildCourierSummary(rows, currentSettings), [rows, currentSettings]);

  useEffect(() => {
    const nextDraft = settingsToDraft(settingsQuery.data ?? DEFAULT_SETTINGS);
    setSettingsDraft(nextDraft);
    setInitialSettingsDraft(nextDraft);
  }, [settingsQuery.data]);

  useEffect(() => {
    const nextBalances: Record<string, string> = {};
    const nextDates: Record<string, string> = {};
    rows.forEach((row) => {
      nextBalances[row.employee_id] = rubInputFromCents(row.opening_balance_cents);
      nextDates[row.employee_id] = row.opening_date || todayKey();
    });
    setOpeningDrafts(nextBalances);
    setOpeningDateDrafts(nextDates);
  }, [rows]);

  const settingsMutation = useMutation({
    mutationFn: () =>
      putCourierDepositSettings({
        auto_withhold_enabled: false,
        target_amount: rubPayloadValue(settingsDraft.targetAmount),
        target_amount_senior: rubPayloadValue(settingsDraft.targetAmountSenior),
        withhold_primary: rubPayloadValue(settingsDraft.withholdPrimary),
        withhold_secondary: rubPayloadValue(settingsDraft.withholdSecondary),
      }),
    onSuccess: async () => {
      toast.success("Настройки сохранены");
      await invalidateCourierDepositQueries(queryClient);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось сохранить настройки"));
    },
  });

  const openingMutation = useMutation({
    mutationFn: (payload: PendingOpeningUpdate) => {
      const amountCents = centsFromRubInput(payload.amount);
      if (amountCents === null) {
        throw new Error("Проверьте сумму");
      }
      return putCourierDepositOpening(payload.row.employee_id, {
        amount_cents: amountCents,
        opening_date: payload.openingDate,
      });
    },
    onSuccess: async () => {
      toast.success("Начальный баланс сохранён");
      setPendingOpeningUpdate(null);
      await invalidateCourierDepositQueries(queryClient);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось сохранить начальный баланс"));
    },
  });

  const saveSettingsDisabled =
    !canWrite ||
    !settingsDirty ||
    !settingsValid ||
    settingsMutation.isPending ||
    settingsQuery.isLoading;

  return (
    <div className="space-y-4">
      <CourierDepositSummary summary={summary} isLoading={depositsQuery.isLoading} />

      <section className="space-y-4 rounded-lg border bg-card p-4">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <h2 className="text-lg font-semibold tracking-normal">Общие правила удержания</h2>
            <p className="mt-1 text-sm leading-6 text-muted-foreground">
              Единые цели и суммы удержаний для курьеров primary и secondary.
            </p>
          </div>
          {settingsQuery.isFetching ? <Badge variant="secondary">Обновление</Badge> : null}
        </div>

        {settingsQuery.isLoading ? (
          <div className="grid gap-3 lg:grid-cols-3 xl:grid-cols-5">
            <Skeleton className="h-20" />
            <Skeleton className="h-20" />
            <Skeleton className="h-20" />
            <Skeleton className="h-20" />
            <Skeleton className="h-20" />
          </div>
        ) : (
          <div className="grid gap-3 lg:grid-cols-3 xl:grid-cols-5">
            <MoneyField
              disabled={!canWrite || settingsMutation.isPending}
              label="Целевой депозит"
              onChange={(value) =>
                setSettingsDraft((current) => ({ ...current, targetAmount: value }))
              }
              value={settingsDraft.targetAmount}
            />
            <MoneyField
              disabled={!canWrite || settingsMutation.isPending}
              label="Целевой депозит старшего курьера"
              onChange={(value) =>
                setSettingsDraft((current) => ({ ...current, targetAmountSenior: value }))
              }
              value={settingsDraft.targetAmountSenior}
            />
            <MoneyField
              disabled={!canWrite || settingsMutation.isPending}
              label="Удержание primary за смену"
              onChange={(value) =>
                setSettingsDraft((current) => ({ ...current, withholdPrimary: value }))
              }
              value={settingsDraft.withholdPrimary}
            />
            <MoneyField
              disabled={!canWrite || settingsMutation.isPending}
              label="Удержание secondary за смену"
              onChange={(value) =>
                setSettingsDraft((current) => ({ ...current, withholdSecondary: value }))
              }
              value={settingsDraft.withholdSecondary}
            />
            <InlineTooltip content="Появится при подключении iiko спецсчёта">
              <div className="flex h-full min-h-20 items-center justify-between rounded-md border bg-muted/30 px-3 py-3">
                <div className="min-w-0">
                  <Label>Автоматическое удержание</Label>
                  <div className="mt-1 text-xs leading-5 text-muted-foreground">MVP: выключено</div>
                </div>
                <Switch checked={false} disabled aria-label="Автоматическое удержание" />
              </div>
            </InlineTooltip>
          </div>
        )}

        {!settingsValid ? (
          <div className="text-sm text-destructive">Проверьте суммы: нужны числа от 0.</div>
        ) : null}
        {settingsQuery.isError ? (
          <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {apiErrorMessage(settingsQuery.error, "Не удалось загрузить настройки курьеров")}
          </div>
        ) : null}

        <div className="flex justify-end">
          <Button disabled={saveSettingsDisabled} onClick={() => settingsMutation.mutate()}>
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
          <h2 className="text-lg font-semibold tracking-normal">Начальные балансы</h2>
          <p className="mt-1 text-sm leading-6 text-muted-foreground">
            Все сотрудники с должностью «Курьер»: дата открытия, стартовая сумма и текущий баланс.
          </p>
        </div>

        {depositsQuery.isLoading ? (
          <CourierTableSkeleton />
        ) : depositsQuery.isError ? (
          <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {apiErrorMessage(depositsQuery.error, "Не удалось загрузить депозиты курьеров")}
          </div>
        ) : rows.length === 0 ? (
          <EmptyState
            icon={<WalletCards size={18} aria-hidden="true" />}
            title="Курьеры не найдены"
            description="Сотрудники с должностью «Курьер» появятся в таблице после синхронизации."
          />
        ) : (
          <div className="space-y-3">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="min-w-[240px]">Курьер</TableHead>
                  <TableHead>Статус</TableHead>
                  <TableHead>Дата открытия</TableHead>
                  <TableHead className="min-w-[160px]">Opening balance ₽</TableHead>
                  <TableHead className="text-right">Текущий баланс ₽</TableHead>
                  <TableHead className="min-w-[160px]">% достижения цели</TableHead>
                  <TableHead className="w-16 text-right">Действия</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((row) => {
                  const amountDraft = openingDrafts[row.employee_id] ?? "";
                  const dateDraft = openingDateDrafts[row.employee_id] ?? row.opening_date;
                  const amountValid = isNonNegativeRubInput(amountDraft);
                  const dirty =
                    centsFromRubInput(amountDraft) !== row.opening_balance_cents ||
                    dateDraft !== row.opening_date;
                  return (
                    <TableRow key={row.employee_id}>
                      <TableCell>
                        <button
                          className="grid text-left text-sm font-medium underline-offset-2 hover:underline"
                          onClick={() => setHistoryCourier(row)}
                          type="button"
                        >
                          {row.full_name}
                          <span className="mt-1 text-xs font-normal text-muted-foreground">
                            {row.category
                              ? COURIER_CATEGORY_LABELS[row.category]
                              : "Категория не задана"}
                          </span>
                        </button>
                      </TableCell>
                      <TableCell>
                        <StatusBadge status={row.status} />
                      </TableCell>
                      <TableCell>
                        <Input
                          className="w-36"
                          disabled={!canWrite || openingMutation.isPending}
                          onChange={(event) =>
                            setOpeningDateDrafts((current) => ({
                              ...current,
                              [row.employee_id]: event.target.value,
                            }))
                          }
                          type="date"
                          value={dateDraft}
                        />
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center gap-2">
                          <Input
                            className="w-32"
                            disabled={!canWrite || openingMutation.isPending}
                            min={0}
                            onChange={(event) =>
                              setOpeningDrafts((current) => ({
                                ...current,
                                [row.employee_id]: event.target.value,
                              }))
                            }
                            type="number"
                            value={amountDraft}
                          />
                          <Button
                            disabled={
                              !canWrite ||
                              !dirty ||
                              !amountValid ||
                              openingMutation.isPending
                            }
                            onClick={() =>
                              setPendingOpeningUpdate({
                                amount: amountDraft,
                                openingDate: dateDraft,
                                row,
                              })
                            }
                            size="sm"
                            title="Сохранить начальный баланс"
                            variant="outline"
                          >
                            <Save size={14} aria-hidden="true" />
                          </Button>
                        </div>
                      </TableCell>
                      <TableCell className="text-right font-medium tabular-nums">
                        {formatCents(row.balance_cents)}
                      </TableCell>
                      <TableCell>
                        <ProgressBar value={progressValue(row)} />
                        <div className="mt-1 text-xs text-muted-foreground">
                          {formatPercent(row.progress_pct)}
                        </div>
                      </TableCell>
                      <TableCell className="text-right">
                        <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <Button size="icon" title="Действия" variant="ghost">
                              <MoreHorizontal size={16} aria-hidden="true" />
                            </Button>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="end">
                            <DropdownMenuItem onClick={() => setHistoryCourier(row)}>
                              <History size={16} aria-hidden="true" />
                              История операций
                            </DropdownMenuItem>
                          </DropdownMenuContent>
                        </DropdownMenu>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
        )}
      </section>

      <AlertDialog
        open={Boolean(pendingOpeningUpdate)}
        onOpenChange={(open) => {
          if (!open) {
            setPendingOpeningUpdate(null);
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Изменить начальный баланс?</AlertDialogTitle>
            <AlertDialogDescription>
              {pendingOpeningUpdate
                ? `Это изменит исторические данные, продолжить? Новый opening balance для ${pendingOpeningUpdate.row.full_name}: ${formatCents(
                    centsFromRubInput(pendingOpeningUpdate.amount) ?? 0,
                  )}, дата открытия ${formatDate(pendingOpeningUpdate.openingDate)}.`
                : ""}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={openingMutation.isPending}>Отмена</AlertDialogCancel>
            <AlertDialogAction
              disabled={!pendingOpeningUpdate || openingMutation.isPending}
              onClick={(event) => {
                event.preventDefault();
                if (pendingOpeningUpdate) {
                  openingMutation.mutate(pendingOpeningUpdate);
                }
              }}
            >
              {openingMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : null}
              Сохранить
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <CourierDepositHistoryDrawer
        canDeleteTopup={canDeleteTopup}
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

function CourierDepositSummary({
  isLoading,
  summary,
}: {
  isLoading: boolean;
  summary: ReturnType<typeof buildCourierSummary>;
}) {
  const rows = [
    { label: "Курьеров с включённым удержанием", value: String(summary.withholdingEnabled) },
    { label: "Сумма всех балансов", value: formatCents(summary.totalBalanceCents) },
    { label: "Средний % достижения цели", value: formatPercent(summary.averageProgress) },
    { label: "Уволенные с остатком", value: String(summary.firedWithBalance) },
  ];

  return (
    <section className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
      {rows.map((row) => (
        <div className="rounded-lg border bg-card p-4" key={row.label}>
          <div className="text-sm leading-5 text-muted-foreground">{row.label}</div>
          {isLoading ? (
            <Skeleton className="mt-3 h-7 w-28" />
          ) : (
            <div className="mt-2 text-xl font-semibold tabular-nums">{row.value}</div>
          )}
        </div>
      ))}
    </section>
  );
}

function MoneyField({
  disabled,
  label,
  onChange,
  value,
}: {
  disabled: boolean;
  label: string;
  onChange: (value: string) => void;
  value: string;
}) {
  return (
    <Label className="grid gap-2 rounded-md border bg-background px-3 py-3">
      <span>{label}</span>
      <div className="flex items-center gap-2">
        <Input
          disabled={disabled}
          min={0}
          onChange={(event) => onChange(event.target.value)}
          type="number"
          value={value}
        />
        <span className="text-sm text-muted-foreground">₽</span>
      </div>
    </Label>
  );
}

function StatusBadge({ status }: { status: string }) {
  const active = status === "active";
  const reserve = status === "requires_setup";
  return (
    <Badge
      className={cn(
        "rounded-md shadow-none",
        active && "border-emerald-200 bg-emerald-50 text-emerald-800 hover:bg-emerald-50",
        reserve && "border-sky-200 bg-sky-50 text-sky-800 hover:bg-sky-50",
        !active && !reserve && "border-muted bg-muted text-muted-foreground hover:bg-muted",
      )}
      variant="outline"
    >
      {statusLabel(status)}
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

function InlineTooltip({ children, content }: { children: ReactNode; content: string }) {
  return (
    <span className="group relative block">
      {children}
      <span className="pointer-events-none absolute left-0 top-full z-50 mt-2 hidden w-72 rounded-md border bg-popover px-3 py-2 text-left text-xs leading-5 text-popover-foreground shadow-lg group-hover:block">
        {content}
      </span>
    </span>
  );
}

function CourierTableSkeleton() {
  return (
    <div className="space-y-3">
      <Skeleton className="h-12 w-full" />
      <Skeleton className="h-12 w-full" />
      <Skeleton className="h-12 w-full" />
      <div className="flex justify-end">
        <LoaderCircle className="h-5 w-5 animate-spin text-muted-foreground" aria-hidden="true" />
      </div>
    </div>
  );
}

function buildCourierSummary(rows: CourierDepositRow[], settings: CourierDepositSettings) {
  const totalBalanceCents = rows.reduce((sum, row) => sum + row.balance_cents, 0);
  const progressSum = rows.reduce((sum, row) => sum + row.progress_pct, 0);
  return {
    averageProgress: rows.length > 0 ? progressSum / rows.length : 0,
    firedWithBalance: rows.filter((row) => row.status !== "active" && row.balance_cents > 0).length,
    totalBalanceCents,
    withholdingEnabled: rows.filter((row) => {
      if (row.status !== "active" || !row.category || row.balance_cents >= row.target_amount_cents) {
        return false;
      }
      return row.category === "primary"
        ? settings.withhold_primary > 0
        : settings.withhold_secondary > 0;
    }).length,
  };
}

function settingsToDraft(settings: CourierDepositSettings): SettingsDraft {
  return {
    targetAmount: String(settings.target_amount),
    targetAmountSenior: String(settings.target_amount_senior),
    withholdPrimary: String(settings.withhold_primary),
    withholdSecondary: String(settings.withhold_secondary),
  };
}

function draftSnapshot(draft: SettingsDraft) {
  return JSON.stringify({
    targetAmount: rubPayloadValue(draft.targetAmount),
    targetAmountSenior: rubPayloadValue(draft.targetAmountSenior),
    withholdPrimary: rubPayloadValue(draft.withholdPrimary),
    withholdSecondary: rubPayloadValue(draft.withholdSecondary),
  });
}

function rubPayloadValue(value: string) {
  const cents = centsFromRubInput(value);
  return cents === null ? 0 : Math.round(cents / 100);
}

async function invalidateCourierDepositQueries(queryClient: ReturnType<typeof useQueryClient>) {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: ["courier-deposits"] }),
    queryClient.invalidateQueries({ queryKey: ["courier-deposit-card"] }),
    queryClient.invalidateQueries({ queryKey: ["courier-deposit-settings"] }),
  ]);
}
