import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  History,
  Link as LinkIcon,
  Plus,
  RefreshCw,
  Save,
  Settings,
  SlidersHorizontal,
} from "lucide-react";
import { toast } from "sonner";

import { BooleanWidget, NumberWidget, PercentWidget } from "@/components/settings-widgets";
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
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { PageHeader } from "@/components/ui-app/PageHeader";
import { getAuthSnapshot, subscribeAuth } from "@/lib/auth";
import { EMPLOYEE_CATEGORY_LABELS } from "@/lib/i18n/employee";
import {
  getPayrollDeductions,
  getPayrollRates,
  getPayrollRevenueShares,
  getPayrollSeniorityPremiums,
  putPayrollDeduction,
  putPayrollRate,
  putPayrollRateAvailability,
  putPayrollRevenueShare,
  putPayrollSeniorityPremium,
  type PayrollDeductionCategory,
  type PayrollDeductionCategoryPayload,
  type PayrollRate,
  type PayrollRevenueShare,
  type PayrollRevenueSharePayload,
  type PayrollSeniorityPremium,
  type PayrollSeniorityPremiumPayload,
} from "@/lib/api";
import { cn } from "@/lib/utils";

type PayrollConfigurationRouteProps = {
  onNavigate: (path: string) => void;
};

type PendingRate = {
  record: PayrollRate;
  amount: string;
  effective_from: string;
  effective_to: string | null;
  is_enabled: boolean;
};

type RateSaveRequest = {
  record: PayrollRate;
  amount: number | null;
  effective_from: string;
  effective_to: string | null;
  is_enabled: boolean;
};

type HistoryDrawer = "rates" | null;

export function PayrollConfigurationRoute({ onNavigate }: PayrollConfigurationRouteProps) {
  const queryClient = useQueryClient();
  const auth = useAuthSnapshot();
  const canWrite = Boolean(
    auth.user?.roles.some(
      (role) => role === "finance_manager" || role === "owner" || role === "admin",
    ),
  );
  const [advanced, setAdvanced] = useState(false);
  const [pendingRate, setPendingRate] = useState<PendingRate | null>(null);
  const [historyDrawer, setHistoryDrawer] = useState<HistoryDrawer>(null);
  const [revenueDraft, setRevenueDraft] = useState<PayrollRevenueSharePayload | null>(null);
  const [deductionDraft, setDeductionDraft] = useState<PayrollDeductionCategoryPayload | null>(
    null,
  );
  const [premiumDraft, setPremiumDraft] = useState<PayrollSeniorityPremiumPayload | null>(null);

  const ratesQuery = useQuery({
    queryKey: ["payroll-config", "rates"],
    queryFn: () => getPayrollRates(false, true),
  });
  const ratesHistoryQuery = useQuery({
    queryKey: ["payroll-config", "rates", "history"],
    queryFn: () => getPayrollRates(true),
    enabled: historyDrawer === "rates",
  });
  const revenueQuery = useQuery({
    queryKey: ["payroll-config", "revenue-share"],
    queryFn: () => getPayrollRevenueShares(),
  });
  const deductionsQuery = useQuery({
    queryKey: ["payroll-config", "deductions"],
    queryFn: () => getPayrollDeductions(),
  });
  const premiumsQuery = useQuery({
    queryKey: ["payroll-config", "seniority-premium"],
    queryFn: () => getPayrollSeniorityPremiums(),
  });

  const rateMutation = useMutation({
    mutationFn: async (request: RateSaveRequest) => {
      await putPayrollRateAvailability(request.record.position_group, request.record.category, {
        is_enabled: request.is_enabled,
      });
      return putPayrollRate({
        position_group: request.record.position_group,
        category: request.record.category,
        station: request.record.station,
        rate_type: request.record.rate_type,
        amount: request.amount,
        is_active: true,
        effective_from: request.effective_from,
        effective_to: request.effective_to,
      });
    },
    onSuccess: async () => {
      toast.success("Ставка сохранена");
      setPendingRate(null);
      await invalidatePayrollConfig(queryClient);
    },
    onError: () => toast.error("Не удалось сохранить ставку"),
  });

  const revenueMutation = useMutation({
    mutationFn: putPayrollRevenueShare,
    onSuccess: async () => {
      toast.success("Правило процента сохранено");
      setRevenueDraft(null);
      await invalidatePayrollConfig(queryClient);
    },
    onError: () => toast.error("Не удалось сохранить процент от выручки"),
  });

  const deductionMutation = useMutation({
    mutationFn: putPayrollDeduction,
    onSuccess: async () => {
      toast.success("Причина удержания сохранена");
      setDeductionDraft(null);
      await invalidatePayrollConfig(queryClient);
    },
    onError: () => toast.error("Не удалось сохранить удержание"),
  });

  const premiumMutation = useMutation({
    mutationFn: putPayrollSeniorityPremium,
    onSuccess: async () => {
      toast.success("Надбавка сохранена");
      setPremiumDraft(null);
      await invalidatePayrollConfig(queryClient);
    },
    onError: () => toast.error("Не удалось сохранить надбавку"),
  });

  const rates = ratesQuery.data ?? [];
  const revenueShares = revenueQuery.data ?? [];
  const deductions = deductionsQuery.data ?? [];
  const premiums = premiumsQuery.data ?? [];

  const isLoading =
    ratesQuery.isLoading ||
    revenueQuery.isLoading ||
    deductionsQuery.isLoading ||
    premiumsQuery.isLoading;
  const hasError =
    ratesQuery.isError || revenueQuery.isError || deductionsQuery.isError || premiumsQuery.isError;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Исходные данные"
        description="Конфигурация payroll-формул: ставки, проценты, удержания и надбавки с историей версий."
        action={
          <div className="flex flex-wrap gap-2">
            <Button
              onClick={() => setAdvanced((value) => !value)}
              title="Расширенный режим"
              variant={advanced ? "default" : "outline"}
            >
              <SlidersHorizontal size={16} aria-hidden="true" />
              Расширенный режим
            </Button>
            <Button
              onClick={() => void invalidatePayrollConfig(queryClient)}
              title="Обновить"
              variant="outline"
            >
              <RefreshCw size={16} aria-hidden="true" />
              Обновить
            </Button>
          </div>
        }
      />

      {!canWrite ? (
        <div className="rounded-md border bg-muted/40 px-3 py-2 text-sm text-muted-foreground">
          Режим просмотра. Изменения доступны финансовому менеджеру и владельцу.
        </div>
      ) : null}

      {isLoading ? (
        <div className="rounded-lg border bg-card px-4 py-8 text-sm text-muted-foreground">
          Загрузка исходных данных...
        </div>
      ) : null}

      {hasError ? (
        <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
          Не удалось загрузить payroll-конфигурацию
        </div>
      ) : null}

      <Tabs defaultValue="rates" className="space-y-4">
        <TabsList className="h-auto flex-wrap justify-start">
          <TabsTrigger value="rates">Ставки</TabsTrigger>
          <TabsTrigger value="revenue">Проценты от выручки</TabsTrigger>
          <TabsTrigger value="deductions">Удержания</TabsTrigger>
          <TabsTrigger value="premiums">Надбавки</TabsTrigger>
          <TabsTrigger value="fund">Накопительный фонд</TabsTrigger>
        </TabsList>

        <TabsContent value="rates" className="mt-0">
          <RatesSection
            advanced={advanced}
            canWrite={canWrite}
            onOpenHistory={() => setHistoryDrawer("rates")}
            onPendingRate={setPendingRate}
            rates={rates}
          />
        </TabsContent>

        <TabsContent value="revenue" className="mt-0">
          <RevenueShareSection
            advanced={advanced}
            canWrite={canWrite}
            onAdd={() =>
              setRevenueDraft({
                position_group: "Производство",
                category: "",
                percent: 0,
                effective_from: todayKey(),
                effective_to: null,
              })
            }
            onEdit={setRevenueDraft}
            revenueShares={revenueShares}
          />
        </TabsContent>

        <TabsContent value="deductions" className="mt-0">
          <DeductionsSection
            advanced={advanced}
            canWrite={canWrite}
            deductions={deductions}
            onAdd={() =>
              setDeductionDraft({
                code: "",
                display_name: "",
                description: "",
                type: "withholding",
                default_amount: null,
                effective_from: todayKey(),
                effective_to: null,
              })
            }
            onEdit={setDeductionDraft}
          />
        </TabsContent>

        <TabsContent value="premiums" className="mt-0">
          <PremiumsSection
            advanced={advanced}
            canWrite={canWrite}
            onEdit={setPremiumDraft}
            premiums={premiums}
          />
        </TabsContent>

        <TabsContent value="fund" className="mt-0">
          <FundSection onNavigate={onNavigate} />
        </TabsContent>
      </Tabs>

      <RateConfirmDialog
        isSaving={rateMutation.isPending}
        onConfirm={(payload) => rateMutation.mutate(payload)}
        onOpenChange={(open) => {
          if (!open) {
            setPendingRate(null);
          }
        }}
        pendingRate={pendingRate}
        setPendingRate={setPendingRate}
      />

      <RevenueShareDialog
        advanced={advanced}
        draft={revenueDraft}
        isSaving={revenueMutation.isPending}
        onChange={setRevenueDraft}
        onOpenChange={(open) => {
          if (!open) {
            setRevenueDraft(null);
          }
        }}
        onSave={(payload) => revenueMutation.mutate(payload)}
      />

      <DeductionDialog
        advanced={advanced}
        draft={deductionDraft}
        isSaving={deductionMutation.isPending}
        onChange={setDeductionDraft}
        onOpenChange={(open) => {
          if (!open) {
            setDeductionDraft(null);
          }
        }}
        onSave={(payload) => deductionMutation.mutate(payload)}
      />

      <PremiumDialog
        advanced={advanced}
        draft={premiumDraft}
        isSaving={premiumMutation.isPending}
        onChange={setPremiumDraft}
        onOpenChange={(open) => {
          if (!open) {
            setPremiumDraft(null);
          }
        }}
        onSave={(payload) => premiumMutation.mutate(payload)}
      />

      <RateHistoryDrawer
        history={ratesHistoryQuery.data ?? []}
        isLoading={ratesHistoryQuery.isLoading}
        onOpenChange={(open) => {
          if (!open) {
            setHistoryDrawer(null);
          }
        }}
        open={historyDrawer === "rates"}
      />
    </div>
  );
}

function RatesSection({
  advanced,
  canWrite,
  onOpenHistory,
  onPendingRate,
  rates,
}: {
  advanced: boolean;
  canWrite: boolean;
  onOpenHistory: () => void;
  onPendingRate: (rate: PendingRate) => void;
  rates: PayrollRate[];
}) {
  const [showAllCategories, setShowAllCategories] = useState(false);
  const positions = useMemo(() => sortedUnique(rates.map((rate) => rate.position_group)), [rates]);
  const categories = PAYROLL_RATE_CATEGORIES;

  return (
    <section className="space-y-4 rounded-lg border bg-card p-4">
      <SectionHeader
        action={
          <div className="flex flex-wrap gap-2">
            <Button
              onClick={() => setShowAllCategories(false)}
              variant={showAllCategories ? "outline" : "default"}
            >
              Только включённые
            </Button>
            <Button
              onClick={() => setShowAllCategories(true)}
              variant={showAllCategories ? "default" : "outline"}
            >
              Показать все категории
            </Button>
            <Button onClick={onOpenHistory} variant="outline">
              <History size={16} aria-hidden="true" />
              История изменений
            </Button>
          </div>
        }
        description="Матрица дневных ставок полной смены. Изменение создаёт новую версию с выбранной даты."
        title="Ставки по должностям и категориям"
      />

      <div className="overflow-x-auto">
        <table className="min-w-[760px] border-separate border-spacing-0 text-sm">
          <thead>
            <tr>
              <th className="sticky left-0 z-10 border-b bg-card p-3 text-left font-medium text-muted-foreground">
                Должность
              </th>
              {categories.map((category) => (
                <th
                  className="border-b p-3 text-right font-medium text-muted-foreground"
                  key={category}
                >
                  {categoryLabel(category)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {positions.map((position) => {
              const rowCells = categories.map(
                (category) =>
                  findRate(rates, position, category) ?? emptyRateCell(rates, position, category),
              );
              const enabledCount = rowCells.filter((rate) => rate.is_enabled).length;
              if (!showAllCategories && enabledCount === 0) {
                return (
                  <tr key={position}>
                    <td className="sticky left-0 z-10 border-b bg-card p-3 font-medium">
                      {position}
                    </td>
                    <td className="border-b p-2" colSpan={categories.length}>
                      <EmptyConfigLine text="У этой должности нет настроенных ставок. Включите хотя бы одну категорию." />
                    </td>
                  </tr>
                );
              }
              return (
                <tr key={position}>
                  <td className="sticky left-0 z-10 border-b bg-card p-3 font-medium">
                    {position}
                  </td>
                  {rowCells.map((rate) => {
                    const hidden = !showAllCategories && !rate.is_enabled;
                    return (
                      <td className="min-w-[136px] border-b p-2 text-right" key={rate.category}>
                        {hidden ? (
                          <span className="block min-h-12 text-muted-foreground"> </span>
                        ) : (
                          <RateCell
                            advanced={advanced}
                            canWrite={canWrite}
                            onEdit={() =>
                              onPendingRate({
                                record: rate,
                                amount: rate.amount === null ? "" : String(rate.amount),
                                effective_from: todayKey(),
                                effective_to: null,
                                is_enabled: rate.is_enabled,
                              })
                            }
                            rate={rate}
                          />
                        )}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function RateCell({
  advanced,
  canWrite,
  onEdit,
  rate,
}: {
  advanced: boolean;
  canWrite: boolean;
  onEdit: () => void;
  rate: PayrollRate;
}) {
  return (
    <button
      className={cn(
        "grid min-h-12 w-full gap-1 rounded-md border px-3 py-2 text-right transition-colors",
        "hover:border-primary/60 hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        !rate.is_enabled && "border-dashed bg-muted/35 text-muted-foreground",
        !canWrite && "cursor-not-allowed opacity-70 hover:border-input hover:bg-background",
      )}
      disabled={!canWrite}
      onClick={onEdit}
      type="button"
    >
      <span className="font-medium tabular-nums">
        {rate.amount === null ? "—" : formatMoney(rate.amount)}
      </span>
      {!rate.is_enabled ? (
        <span className="flex justify-end">
          <Badge variant="secondary">Отключена</Badge>
        </span>
      ) : null}
      {advanced ? (
        <span className="text-[11px] leading-4 text-muted-foreground">
          {rate.effective_from
            ? `${formatDate(rate.effective_from)} → ${
                rate.effective_to ? formatDate(rate.effective_to) : "сейчас"
              }`
            : "нет записи"}
          {!rate.is_active ? " · заглушка" : ""}
        </span>
      ) : null}
    </button>
  );
}

function RevenueShareSection({
  advanced,
  canWrite,
  onAdd,
  onEdit,
  revenueShares,
}: {
  advanced: boolean;
  canWrite: boolean;
  onAdd: () => void;
  onEdit: (payload: PayrollRevenueSharePayload) => void;
  revenueShares: PayrollRevenueShare[];
}) {
  return (
    <section className="space-y-4 rounded-lg border bg-card p-4">
      <SectionHeader
        action={
          <Button disabled={!canWrite} onClick={onAdd}>
            <Plus size={16} aria-hidden="true" />
            Добавить правило
          </Button>
        }
        description="Правила процента от дневной выручки. Текущие seed-данные сохранены как пороги из исходного листа."
        title="Проценты от выручки"
      />
      <div className="grid gap-2">
        {revenueShares.map((rule) => (
          <div
            className="grid gap-3 rounded-md border px-3 py-3 sm:grid-cols-[1fr_auto_auto] sm:items-center"
            key={rule.id}
          >
            <div className="min-w-0">
              <div className="font-medium">{rule.position_group}</div>
              <div className="text-sm text-muted-foreground">{rule.category}</div>
              {advanced ? <RawMeta value={`${rule.position_group} / ${rule.category}`} /> : null}
            </div>
            <div className="text-lg font-semibold tabular-nums">{formatPercent(rule.percent)}</div>
            <Button
              disabled={!canWrite}
              onClick={() =>
                onEdit({
                  position_group: rule.position_group,
                  category: rule.category,
                  percent: rule.percent,
                  effective_from: todayKey(),
                  effective_to: null,
                })
              }
              size="sm"
              variant="outline"
            >
              <Save size={16} aria-hidden="true" />
              Новая версия
            </Button>
          </div>
        ))}
        {revenueShares.length === 0 ? <EmptyConfigLine text="Правила пока не заданы." /> : null}
      </div>
    </section>
  );
}

function DeductionsSection({
  advanced,
  canWrite,
  deductions,
  onAdd,
  onEdit,
}: {
  advanced: boolean;
  canWrite: boolean;
  deductions: PayrollDeductionCategory[];
  onAdd: () => void;
  onEdit: (payload: PayrollDeductionCategoryPayload) => void;
}) {
  return (
    <section className="space-y-4 rounded-lg border bg-card p-4">
      <SectionHeader
        action={
          <Button disabled={!canWrite} onClick={onAdd}>
            <Plus size={16} aria-hidden="true" />
            Добавить причину
          </Button>
        }
        description="Справочник причин удержаний, штрафов и списания депозитов."
        title="Удержания"
      />
      <div className="grid gap-2">
        {deductions.map((deduction) => (
          <div
            className="grid gap-3 rounded-md border px-3 py-3 lg:grid-cols-[1fr_auto_auto] lg:items-center"
            key={deduction.id}
          >
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <div className="font-medium">{deduction.display_name}</div>
                <Badge variant="secondary">{deductionTypeLabel(deduction.type)}</Badge>
              </div>
              {deduction.description ? (
                <div className="mt-1 text-sm text-muted-foreground">{deduction.description}</div>
              ) : null}
              {advanced ? <RawMeta value={deduction.code} /> : null}
            </div>
            <div className="text-right font-medium tabular-nums">
              {deduction.default_amount === null
                ? "Без суммы"
                : formatMoney(deduction.default_amount)}
            </div>
            <Button
              disabled={!canWrite}
              onClick={() =>
                onEdit({
                  code: deduction.code,
                  display_name: deduction.display_name,
                  description: deduction.description ?? "",
                  type: deduction.type,
                  default_amount: deduction.default_amount,
                  effective_from: todayKey(),
                  effective_to: null,
                })
              }
              size="sm"
              variant="outline"
            >
              <Save size={16} aria-hidden="true" />
              Новая версия
            </Button>
          </div>
        ))}
        {deductions.length === 0 ? (
          <EmptyConfigLine text="Причины удержаний пока не заданы." />
        ) : null}
      </div>
    </section>
  );
}

function PremiumsSection({
  advanced,
  canWrite,
  onEdit,
  premiums,
}: {
  advanced: boolean;
  canWrite: boolean;
  onEdit: (payload: PayrollSeniorityPremiumPayload) => void;
  premiums: PayrollSeniorityPremium[];
}) {
  const rows = ["senior", "deputy_senior"] as const;
  return (
    <section className="space-y-4 rounded-lg border bg-card p-4">
      <SectionHeader
        description="Процентные надбавки к базовой ставке. В исходном листе найдены фиксированные суммы, поэтому seed требует подтверждения владельца."
        title="Надбавки"
      />
      <div className="grid gap-2 md:grid-cols-2">
        {rows.map((role) => {
          const premium = premiums.find((item) => item.role === role);
          return (
            <div className="rounded-md border px-3 py-3" key={role}>
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="font-medium">{seniorityRoleLabel(role)}</div>
                  <div className="mt-1 text-sm text-muted-foreground">
                    {premium ? formatPercent(premium.percent_of_base) : "Не задано"}
                  </div>
                </div>
                <Button
                  disabled={!canWrite}
                  onClick={() =>
                    onEdit({
                      role,
                      percent_of_base: premium?.percent_of_base ?? 0,
                      effective_from: todayKey(),
                      effective_to: null,
                    })
                  }
                  size="sm"
                  variant="outline"
                >
                  <Save size={16} aria-hidden="true" />
                  Новая версия
                </Button>
              </div>
              {advanced ? <RawMeta value={role} /> : null}
            </div>
          );
        })}
      </div>
    </section>
  );
}

function FundSection({ onNavigate }: { onNavigate: (path: string) => void }) {
  return (
    <section className="space-y-4 rounded-lg border bg-card p-4">
      <SectionHeader
        description="Ставка отчислений и дата выплаты накопительного фонда уже ведутся как общие настройки зарплаты."
        title="Накопительный фонд"
      />
      <div className="grid gap-4 md:grid-cols-[1fr_auto] md:items-center">
        <div className="text-sm leading-6 text-muted-foreground">
          Здесь нет дублирования: текущие параметры фонда остаются в разделе «Настройки», а расчёт
          зарплаты использует единый источник для выплат 15 января и ставок по стажу.
        </div>
        <Button onClick={() => onNavigate("/settings")} variant="outline">
          <Settings size={16} aria-hidden="true" />
          Открыть настройки
          <LinkIcon size={16} aria-hidden="true" />
        </Button>
      </div>
    </section>
  );
}

function RateConfirmDialog({
  isSaving,
  onConfirm,
  onOpenChange,
  pendingRate,
  setPendingRate,
}: {
  isSaving: boolean;
  onConfirm: (payload: RateSaveRequest) => void;
  onOpenChange: (open: boolean) => void;
  pendingRate: PendingRate | null;
  setPendingRate: (value: PendingRate | null) => void;
}) {
  const record = pendingRate?.record;
  const parsedAmount = pendingRate ? parseRateAmount(pendingRate.amount) : undefined;
  const amountIsValid = parsedAmount !== undefined;
  return (
    <Dialog onOpenChange={onOpenChange} open={Boolean(pendingRate)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {record?.is_enabled ? "Ставка категории" : "Включить категорию и задать ставку"}
          </DialogTitle>
          <DialogDescription>
            Сохранение обновит доступность комбинации и создаст новую версию ставки с выбранной
            даты.
          </DialogDescription>
        </DialogHeader>
        {pendingRate && record ? (
          <div className="grid gap-4">
            <div className="rounded-md bg-muted px-3 py-2 text-sm">
              {record.position_group}, {categoryLabel(record.category)}
            </div>
            <LabeledInput
              label="Ставка"
              onChange={(value) => setPendingRate({ ...pendingRate, amount: String(value) })}
              type="number"
              value={pendingRate.amount}
            />
            {!amountIsValid ? (
              <div className="text-sm text-destructive">Введите неотрицательное число.</div>
            ) : null}
            <LabeledInput
              label="Дата начала действия"
              onChange={(value) =>
                setPendingRate({ ...pendingRate, effective_from: String(value) })
              }
              type="date"
              value={pendingRate.effective_from}
            />
            <div className="flex items-center justify-between gap-4 rounded-md border px-3 py-3">
              <Label>Включить эту комбинацию</Label>
              <BooleanWidget
                onChange={(value) => setPendingRate({ ...pendingRate, is_enabled: Boolean(value) })}
                value={pendingRate.is_enabled}
              />
            </div>
          </div>
        ) : null}
        <DialogFooter>
          <Button onClick={() => onOpenChange(false)} type="button" variant="outline">
            Отмена
          </Button>
          <Button
            disabled={!pendingRate || !amountIsValid || isSaving}
            onClick={() => {
              if (!pendingRate || parsedAmount === undefined) {
                return;
              }
              onConfirm({
                record: pendingRate.record,
                amount: parsedAmount,
                effective_from: pendingRate.effective_from,
                effective_to: pendingRate.effective_to,
                is_enabled: pendingRate.is_enabled,
              });
            }}
          >
            <Save size={16} aria-hidden="true" />
            Сохранить
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function RevenueShareDialog({
  advanced,
  draft,
  isSaving,
  onChange,
  onOpenChange,
  onSave,
}: {
  advanced: boolean;
  draft: PayrollRevenueSharePayload | null;
  isSaving: boolean;
  onChange: (value: PayrollRevenueSharePayload | null) => void;
  onOpenChange: (open: boolean) => void;
  onSave: (payload: PayrollRevenueSharePayload) => void;
}) {
  return (
    <Dialog onOpenChange={onOpenChange} open={Boolean(draft)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Правило процента от выручки</DialogTitle>
          <DialogDescription>Сохранение создаёт новую версию правила.</DialogDescription>
        </DialogHeader>
        {draft ? (
          <div className="grid gap-4">
            <LabeledInput
              label="Должность или группа"
              onChange={(value) => onChange({ ...draft, position_group: String(value) })}
              value={draft.position_group}
            />
            <LabeledInput
              label="Категория или порог"
              onChange={(value) => onChange({ ...draft, category: String(value) })}
              value={draft.category}
            />
            <div className="grid gap-2">
              <Label>Процент</Label>
              <PercentWidget
                onChange={(value) => onChange({ ...draft, percent: Number(value) || 0 })}
                value={draft.percent}
              />
            </div>
            {advanced ? (
              <EffectiveInputs
                effectiveFrom={draft.effective_from}
                effectiveTo={draft.effective_to}
                onChange={(effective_from, effective_to) =>
                  onChange({ ...draft, effective_from, effective_to })
                }
              />
            ) : null}
          </div>
        ) : null}
        <DialogFooter>
          <Button onClick={() => onOpenChange(false)} variant="outline">
            Отмена
          </Button>
          <Button disabled={!draft || isSaving} onClick={() => draft && onSave(draft)}>
            <Save size={16} aria-hidden="true" />
            Сохранить
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function DeductionDialog({
  advanced,
  draft,
  isSaving,
  onChange,
  onOpenChange,
  onSave,
}: {
  advanced: boolean;
  draft: PayrollDeductionCategoryPayload | null;
  isSaving: boolean;
  onChange: (value: PayrollDeductionCategoryPayload | null) => void;
  onOpenChange: (open: boolean) => void;
  onSave: (payload: PayrollDeductionCategoryPayload) => void;
}) {
  return (
    <Dialog onOpenChange={onOpenChange} open={Boolean(draft)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Причина удержания</DialogTitle>
          <DialogDescription>Справочник штрафов, удержаний и списания депозитов.</DialogDescription>
        </DialogHeader>
        {draft ? (
          <div className="grid gap-4">
            <LabeledInput
              label="Название"
              onChange={(value) => onChange({ ...draft, display_name: String(value) })}
              value={draft.display_name}
            />
            <LabeledInput
              label="Код"
              onChange={(value) => onChange({ ...draft, code: String(value) })}
              value={draft.code}
            />
            <div className="grid gap-2">
              <Label>Тип</Label>
              <select
                className="h-10 rounded-md border border-input bg-background px-3 text-sm"
                onChange={(event) =>
                  onChange({
                    ...draft,
                    type: event.target.value as PayrollDeductionCategoryPayload["type"],
                  })
                }
                value={draft.type}
              >
                <option value="fine">Штраф</option>
                <option value="withholding">Удержание</option>
                <option value="deposit_writeoff">Списание депозита</option>
              </select>
            </div>
            <div className="grid gap-2">
              <Label>Сумма по умолчанию</Label>
              <NumberWidget
                onChange={(value) =>
                  onChange({ ...draft, default_amount: value === "" ? null : Number(value) })
                }
                unit="₽"
                value={draft.default_amount ?? ""}
              />
            </div>
            <LabeledInput
              label="Описание"
              onChange={(value) => onChange({ ...draft, description: String(value) })}
              value={draft.description ?? ""}
            />
            {advanced ? (
              <EffectiveInputs
                effectiveFrom={draft.effective_from}
                effectiveTo={draft.effective_to}
                onChange={(effective_from, effective_to) =>
                  onChange({ ...draft, effective_from, effective_to })
                }
              />
            ) : null}
          </div>
        ) : null}
        <DialogFooter>
          <Button onClick={() => onOpenChange(false)} variant="outline">
            Отмена
          </Button>
          <Button disabled={!draft || isSaving} onClick={() => draft && onSave(draft)}>
            <Save size={16} aria-hidden="true" />
            Сохранить
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function PremiumDialog({
  advanced,
  draft,
  isSaving,
  onChange,
  onOpenChange,
  onSave,
}: {
  advanced: boolean;
  draft: PayrollSeniorityPremiumPayload | null;
  isSaving: boolean;
  onChange: (value: PayrollSeniorityPremiumPayload | null) => void;
  onOpenChange: (open: boolean) => void;
  onSave: (payload: PayrollSeniorityPremiumPayload) => void;
}) {
  return (
    <Dialog onOpenChange={onOpenChange} open={Boolean(draft)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Надбавка</DialogTitle>
          <DialogDescription>Новая версия процентной надбавки к базовой ставке.</DialogDescription>
        </DialogHeader>
        {draft ? (
          <div className="grid gap-4">
            <div className="rounded-md bg-muted px-3 py-2 text-sm">
              {seniorityRoleLabel(draft.role)}
            </div>
            <div className="grid gap-2">
              <Label>Процент к окладу</Label>
              <PercentWidget
                onChange={(value) => onChange({ ...draft, percent_of_base: Number(value) || 0 })}
                value={draft.percent_of_base}
              />
            </div>
            {advanced ? (
              <EffectiveInputs
                effectiveFrom={draft.effective_from}
                effectiveTo={draft.effective_to}
                onChange={(effective_from, effective_to) =>
                  onChange({ ...draft, effective_from, effective_to })
                }
              />
            ) : null}
          </div>
        ) : null}
        <DialogFooter>
          <Button onClick={() => onOpenChange(false)} variant="outline">
            Отмена
          </Button>
          <Button disabled={!draft || isSaving} onClick={() => draft && onSave(draft)}>
            <Save size={16} aria-hidden="true" />
            Сохранить
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function RateHistoryDrawer({
  history,
  isLoading,
  onOpenChange,
  open,
}: {
  history: PayrollRate[];
  isLoading: boolean;
  onOpenChange: (open: boolean) => void;
  open: boolean;
}) {
  return (
    <Sheet onOpenChange={onOpenChange} open={open}>
      <SheetContent className="w-full overflow-y-auto sm:max-w-2xl">
        <SheetHeader>
          <SheetTitle>История ставок</SheetTitle>
          <SheetDescription>Все версии ставок по должностям и категориям.</SheetDescription>
        </SheetHeader>
        <div className="mt-6 grid gap-3">
          {history.map((rate) => (
            <div
              className="rounded-md border px-3 py-3"
              key={rate.id ?? `${rate.position_group}-${rate.category}-${rate.effective_from}`}
            >
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="font-medium">
                    {rate.position_group}, {categoryLabel(rate.category)}
                  </div>
                  <div className="mt-1 text-sm text-muted-foreground">
                    {rate.effective_from ? formatDate(rate.effective_from) : "без даты"} →{" "}
                    {rate.effective_to ? formatDate(rate.effective_to) : "сейчас"}
                  </div>
                </div>
                <div className="font-semibold tabular-nums">{formatMoney(rate.amount)}</div>
              </div>
            </div>
          ))}
          {isLoading ? <EmptyConfigLine text="Загрузка истории..." /> : null}
          {!isLoading && history.length === 0 ? <EmptyConfigLine text="История пуста." /> : null}
        </div>
      </SheetContent>
    </Sheet>
  );
}

function SectionHeader({
  action,
  description,
  title,
}: {
  action?: ReactNode;
  description: string;
  title: string;
}) {
  return (
    <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
      <div className="min-w-0">
        <h2 className="text-lg font-semibold tracking-normal">{title}</h2>
        <p className="mt-1 text-sm leading-6 text-muted-foreground">{description}</p>
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  );
}

function EffectiveInputs({
  effectiveFrom,
  effectiveTo,
  onChange,
}: {
  effectiveFrom: string;
  effectiveTo: string | null;
  onChange: (effectiveFrom: string, effectiveTo: string | null) => void;
}) {
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <LabeledInput
        label="Дата начала действия"
        onChange={(value) => onChange(String(value), effectiveTo)}
        type="date"
        value={effectiveFrom}
      />
      <LabeledInput
        label="Дата окончания"
        onChange={(value) => onChange(effectiveFrom, value ? String(value) : null)}
        type="date"
        value={effectiveTo ?? ""}
      />
    </div>
  );
}

function LabeledInput({
  label,
  onChange,
  type = "text",
  value,
}: {
  label: string;
  onChange: (value: string) => void;
  type?: string;
  value: string;
}) {
  return (
    <div className="grid gap-2">
      <Label>{label}</Label>
      <Input onChange={(event) => onChange(event.target.value)} type={type} value={value} />
    </div>
  );
}

function RawMeta({ value }: { value: string }) {
  return (
    <div className="mt-2 break-all font-mono text-[11px] leading-4 text-muted-foreground">
      {value}
    </div>
  );
}

function EmptyConfigLine({ text }: { text: string }) {
  return (
    <div className="rounded-md border border-dashed px-3 py-6 text-center text-sm text-muted-foreground">
      {text}
    </div>
  );
}

function useAuthSnapshot() {
  const [auth, setAuth] = useState(getAuthSnapshot);

  useEffect(() => {
    const unsubscribe = subscribeAuth(setAuth);
    return () => {
      unsubscribe();
    };
  }, []);

  return auth;
}

async function invalidatePayrollConfig(queryClient: ReturnType<typeof useQueryClient>) {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: ["payroll-config", "rates"] }),
    queryClient.invalidateQueries({ queryKey: ["payroll-config", "availability"] }),
    queryClient.invalidateQueries({ queryKey: ["payroll-config", "revenue-share"] }),
    queryClient.invalidateQueries({ queryKey: ["payroll-config", "deductions"] }),
    queryClient.invalidateQueries({ queryKey: ["payroll-config", "seniority-premium"] }),
  ]);
}

const PAYROLL_RATE_CATEGORIES = ["category_1", "category_2", "category_3", "intern", "freelancer"];

function findRate(rates: PayrollRate[], position: string, category: string) {
  return rates.find(
    (rate) =>
      rate.position_group === position && rate.category === category && rate.rate_type === "daily",
  );
}

function emptyRateCell(rates: PayrollRate[], position: string, category: string): PayrollRate {
  const station =
    rates.find((rate) => rate.position_group === position && rate.station)?.station ?? null;
  return {
    id: null,
    position_group: position,
    category,
    station,
    rate_type: "daily",
    amount: null,
    is_active: true,
    is_enabled: false,
    effective_from: null,
    effective_to: null,
    created_at: null,
  };
}

function sortedUnique(values: string[]) {
  return Array.from(new Set(values)).sort((a, b) => a.localeCompare(b, "ru"));
}

function categoryLabel(category: string) {
  return EMPLOYEE_CATEGORY_LABELS[category as keyof typeof EMPLOYEE_CATEGORY_LABELS] ?? category;
}

function parseRateAmount(value: string): number | null | undefined {
  if (value.trim() === "") {
    return null;
  }
  const amount = Number(value);
  if (!Number.isFinite(amount) || amount < 0) {
    return undefined;
  }
  return amount;
}

function deductionTypeLabel(type: PayrollDeductionCategory["type"]) {
  if (type === "fine") {
    return "Штраф";
  }
  if (type === "deposit_writeoff") {
    return "Списание депозита";
  }
  return "Удержание";
}

function seniorityRoleLabel(role: PayrollSeniorityPremium["role"]) {
  return role === "senior" ? "Старший" : "Заместитель старшего";
}

function todayKey() {
  const today = new Date();
  const year = today.getFullYear();
  const month = `${today.getMonth() + 1}`.padStart(2, "0");
  const day = `${today.getDate()}`.padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function formatMoney(value: number | null) {
  if (value === null) {
    return "Без суммы";
  }
  return new Intl.NumberFormat("ru-RU", {
    maximumFractionDigits: 0,
    style: "currency",
    currency: "RUB",
  }).format(value);
}

function formatPercent(value: number) {
  return new Intl.NumberFormat("ru-RU", {
    maximumFractionDigits: 2,
    style: "percent",
  }).format(value);
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("ru-RU", { timeZone: "Europe/Moscow" }).format(new Date(value));
}
