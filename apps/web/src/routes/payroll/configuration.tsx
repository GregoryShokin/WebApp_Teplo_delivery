import { Fragment, useEffect, useMemo, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, History, Plus, RefreshCw, Save, SlidersHorizontal, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { DepositsSettingsTab } from "@/components/deposits/DepositsSettingsTab";
import { BooleanWidget, NumberWidget, PercentWidget } from "@/components/settings-widgets";
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
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { PageHeader } from "@/components/ui-app/PageHeader";
import { EMPLOYEE_CATEGORY_LABELS } from "@/lib/i18n/employee";
import { usePermissions } from "@/lib/permissions";
import { InventoryPositionsSection } from "@/routes/payroll/inventory-positions";
import {
  apiErrorMessage,
  apiErrorStatus,
  createEmployeeDismissalReason,
  getFundInitialBalanceRoster,
  getFundTiers,
  getPayrollCategoryCoefficients,
  getPayrollDeductions,
  getEmployeeDismissalReasons,
  getPayrollRates,
  getPayrollRevenueTiers,
  getPayrollSeniorityPremiums,
  patchFundExclusion,
  putPayrollCategoryCoefficients,
  putPayrollDeduction,
  putFundTiers,
  putPayrollRate,
  putPayrollRateAvailability,
  putPayrollRevenueTiers,
  putPayrollSeniorityPremium,
  setFundInitialBalance,
  updateEmployeeDismissalReason,
  type EmployeeDismissalReason,
  type EmployeeDismissalReasonCreatePayload,
  type FundRosterRow,
  type FundTierItem,
  type PayrollCategoryCoefficient,
  type PayrollCategoryCoefficientPayload,
  type PayrollDeductionCategory,
  type PayrollDeductionCategoryPayload,
  type PayrollRate,
  type PayrollRevenueTier,
  type PayrollRevenueTierPayload,
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

type RevenueTierDraft = {
  min_revenue: string;
  max_revenue: string;
  rate_percent: number;
};

type CategoryCoefficientDraft = {
  category: PayrollCategoryCoefficient["category"];
  coefficient: string;
};

type TerminationReasonDraft = {
  id?: string;
  code: string;
  label: string;
  requires_comment: boolean;
  is_active: boolean;
  sort_order: number;
};

type HistoryDrawer = "rates" | null;
type FundTierDraft = {
  id: string;
  minMonths: string;
  ratePercent: string;
};
type FundRosterFilter = "all" | "active" | "excluded";
type PendingInitialBalance = {
  row: FundRosterRow;
  amount: number;
};
type PendingFundExclusion = {
  row: FundRosterRow;
  until: string;
  reason: string;
};

export function PayrollConfigurationRoute({ onNavigate }: PayrollConfigurationRouteProps) {
  void onNavigate;
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const canWrite = permissions.canPerformAction("payroll.source-data.edit");
  const [advanced, setAdvanced] = useState(false);
  const [pendingRate, setPendingRate] = useState<PendingRate | null>(null);
  const [historyDrawer, setHistoryDrawer] = useState<HistoryDrawer>(null);
  const [deductionDraft, setDeductionDraft] = useState<PayrollDeductionCategoryPayload | null>(
    null,
  );
  const initialTab =
    new URLSearchParams(window.location.search).get("tab") === "inventory" ? "inventory" : "rates";
  const [terminationReasonDraft, setTerminationReasonDraft] =
    useState<TerminationReasonDraft | null>(null);

  const ratesQuery = useQuery({
    queryKey: ["payroll-config", "rates"],
    queryFn: () => getPayrollRates(false, true),
  });
  const ratesHistoryQuery = useQuery({
    queryKey: ["payroll-config", "rates", "history"],
    queryFn: () => getPayrollRates(true),
    enabled: historyDrawer === "rates",
  });
  const revenueTiersQuery = useQuery({
    queryKey: ["payroll-config", "revenue-tiers"],
    queryFn: () => getPayrollRevenueTiers(),
  });
  const categoryCoefficientsQuery = useQuery({
    queryKey: ["payroll-config", "category-coefficients"],
    queryFn: () => getPayrollCategoryCoefficients(),
  });
  const deductionsQuery = useQuery({
    queryKey: ["payroll-config", "deductions"],
    queryFn: () => getPayrollDeductions(),
  });
  const premiumsQuery = useQuery({
    queryKey: ["payroll-config", "seniority-premium"],
    queryFn: () => getPayrollSeniorityPremiums(),
  });
  const premiumsHistoryQuery = useQuery({
    queryKey: ["payroll-config", "seniority-premium", "history"],
    queryFn: () => getPayrollSeniorityPremiums(true),
  });
  const terminationReasonsQuery = useQuery({
    queryKey: ["employees", "dismissal-reasons", "all"],
    queryFn: () => getEmployeeDismissalReasons(true),
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

  const revenueTiersMutation = useMutation({
    mutationFn: putPayrollRevenueTiers,
    onSuccess: async () => {
      toast.success("Пороги выручки сохранены");
      await invalidatePayrollConfig(queryClient);
    },
    onError: () => toast.error("Не удалось сохранить пороги выручки"),
  });

  const categoryCoefficientsMutation = useMutation({
    mutationFn: putPayrollCategoryCoefficients,
    onSuccess: async () => {
      toast.success("Коэффициенты категорий сохранены");
      await invalidatePayrollConfig(queryClient);
    },
    onError: () => toast.error("Не удалось сохранить коэффициенты категорий"),
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
    mutationFn: async (payloads: PayrollSeniorityPremiumPayload[]) => {
      const saved: PayrollSeniorityPremium[] = [];
      for (const payload of payloads) {
        saved.push(await putPayrollSeniorityPremium(payload));
      }
      return saved;
    },
    onSuccess: async () => {
      toast.success("Версия надбавок сохранена");
      await invalidatePayrollConfig(queryClient);
    },
    onError: () => toast.error("Не удалось сохранить надбавки"),
  });
  const terminationReasonMutation = useMutation({
    mutationFn: (draft: TerminationReasonDraft) => {
      if (draft.id) {
        return updateEmployeeDismissalReason(draft.id, {
          label: draft.label,
          requires_comment: draft.requires_comment,
          is_active: draft.is_active,
          sort_order: draft.sort_order,
        });
      }
      const payload: EmployeeDismissalReasonCreatePayload = {
        label: draft.label,
        requires_comment: draft.requires_comment,
        is_active: draft.is_active,
        sort_order: draft.sort_order,
      };
      const code = draft.code.trim();
      if (code) {
        payload.code = code;
      }
      return createEmployeeDismissalReason(payload);
    },
    onSuccess: async () => {
      toast.success("Причина увольнения сохранена");
      setTerminationReasonDraft(null);
      await invalidateTerminationReasons(queryClient);
    },
    onError: () => toast.error("Не удалось сохранить причину увольнения"),
  });

  const rates = ratesQuery.data ?? [];
  const revenueTiers = revenueTiersQuery.data ?? [];
  const categoryCoefficients = categoryCoefficientsQuery.data ?? [];
  const deductions = deductionsQuery.data ?? [];
  const premiums = premiumsQuery.data ?? [];
  const premiumsHistory = premiumsHistoryQuery.data ?? [];
  const terminationReasons = terminationReasonsQuery.data ?? [];

  const isLoading =
    ratesQuery.isLoading ||
    revenueTiersQuery.isLoading ||
    categoryCoefficientsQuery.isLoading ||
    deductionsQuery.isLoading ||
    premiumsQuery.isLoading ||
    premiumsHistoryQuery.isLoading ||
    terminationReasonsQuery.isLoading;
  const hasError =
    ratesQuery.isError ||
    revenueTiersQuery.isError ||
    categoryCoefficientsQuery.isError ||
    deductionsQuery.isError ||
    premiumsQuery.isError ||
    premiumsHistoryQuery.isError ||
    terminationReasonsQuery.isError;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Исходные данные"
        description="Конфигурация payroll-формул и рабочих справочников: ставки, проценты, удержания, увольнения и надбавки."
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
              onClick={() => {
                void invalidatePayrollConfig(queryClient);
                void invalidateTerminationReasons(queryClient);
              }}
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
          Не удалось загрузить исходные данные
        </div>
      ) : null}

      <Tabs defaultValue={initialTab} className="space-y-4">
        <TabsList className="h-auto flex-wrap justify-start">
          <TabsTrigger value="rates">Ставки</TabsTrigger>
          <TabsTrigger value="revenue">Проценты от выручки</TabsTrigger>
          <TabsTrigger value="deductions">Удержания</TabsTrigger>
          <TabsTrigger value="termination">Причины увольнения</TabsTrigger>
          <TabsTrigger value="premiums">Надбавки</TabsTrigger>
          <TabsTrigger value="fund">Накопительный фонд</TabsTrigger>
          <TabsTrigger value="deposits">Депозиты</TabsTrigger>
          <TabsTrigger value="inventory">Ревизии</TabsTrigger>
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
          <RevenuePercentSection
            advanced={advanced}
            categoryCoefficients={categoryCoefficients}
            canWrite={canWrite}
            isSavingCoefficients={categoryCoefficientsMutation.isPending}
            isSavingTiers={revenueTiersMutation.isPending}
            onSaveCoefficients={(payload) => categoryCoefficientsMutation.mutate(payload)}
            onSaveTiers={(payload) => revenueTiersMutation.mutate(payload)}
            revenueTiers={revenueTiers}
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

        <TabsContent value="termination" className="mt-0">
          <TerminationReasonsSection
            advanced={advanced}
            canWrite={canWrite}
            onAdd={() =>
              setTerminationReasonDraft({
                code: "",
                label: "",
                requires_comment: false,
                is_active: true,
                sort_order: nextSortOrder(terminationReasons),
              })
            }
            onEdit={(reason) =>
              setTerminationReasonDraft({
                id: reason.id,
                code: reason.code,
                label: reason.label,
                requires_comment: reason.requires_comment,
                is_active: reason.is_active,
                sort_order: reason.sort_order,
              })
            }
            reasons={terminationReasons}
          />
        </TabsContent>

        <TabsContent value="premiums" className="mt-0">
          <PremiumsSection
            advanced={advanced}
            canWrite={canWrite}
            history={premiumsHistory}
            isSaving={premiumMutation.isPending}
            onSave={(payloads) => premiumMutation.mutate(payloads)}
            premiums={premiums}
          />
        </TabsContent>

        <TabsContent value="fund" className="mt-0">
          <FundSection canWrite={canWrite} />
        </TabsContent>

        <TabsContent value="deposits" className="mt-0">
          <DepositsSettingsTab canWrite={canWrite} />
        </TabsContent>

        <TabsContent value="inventory" className="mt-0">
          <InventoryPositionsSection canWrite={canWrite} />
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

      <TerminationReasonDialog
        advanced={advanced}
        draft={terminationReasonDraft}
        isSaving={terminationReasonMutation.isPending}
        onChange={setTerminationReasonDraft}
        onOpenChange={(open) => {
          if (!open) {
            setTerminationReasonDraft(null);
          }
        }}
        onSave={(payload) => terminationReasonMutation.mutate(payload)}
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
                    const hidden =
                      !showAllCategories && !rate.is_enabled && rate.category !== "category_4";
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

function RevenuePercentSection({
  advanced,
  canWrite,
  categoryCoefficients,
  isSavingCoefficients,
  isSavingTiers,
  onSaveCoefficients,
  onSaveTiers,
  revenueTiers,
}: {
  advanced: boolean;
  canWrite: boolean;
  categoryCoefficients: PayrollCategoryCoefficient[];
  isSavingCoefficients: boolean;
  isSavingTiers: boolean;
  onSaveCoefficients: (payload: PayrollCategoryCoefficientPayload[]) => void;
  onSaveTiers: (payload: PayrollRevenueTierPayload[]) => void;
  revenueTiers: PayrollRevenueTier[];
}) {
  const [tierEffectiveFrom, setTierEffectiveFrom] = useState(todayKey());
  const [coefficientEffectiveFrom, setCoefficientEffectiveFrom] = useState(todayKey());
  const [tierDrafts, setTierDrafts] = useState<RevenueTierDraft[]>([]);
  const [coefficientDrafts, setCoefficientDrafts] = useState<CategoryCoefficientDraft[]>([]);

  useEffect(() => {
    setTierDrafts(
      [...revenueTiers]
        .sort((left, right) => left.min_revenue - right.min_revenue)
        .map((tier) => ({
          min_revenue: String(tier.min_revenue),
          max_revenue: tier.max_revenue === null ? "" : String(tier.max_revenue),
          rate_percent: tier.rate_percent,
        })),
    );
  }, [revenueTiers]);

  useEffect(() => {
    setCoefficientDrafts(
      PAYROLL_RATE_CATEGORIES.map((category) => {
        const current = categoryCoefficients.find((item) => item.category === category);
        return {
          category: category as PayrollCategoryCoefficient["category"],
          coefficient: current ? String(current.coefficient) : "",
        };
      }),
    );
  }, [categoryCoefficients]);

  const tierPayloads = useMemo(
    () => buildRevenueTierPayloads(tierDrafts, tierEffectiveFrom),
    [tierDrafts, tierEffectiveFrom],
  );
  const coefficientPayloads = useMemo(
    () => buildCategoryCoefficientPayloads(coefficientDrafts, coefficientEffectiveFrom),
    [coefficientDrafts, coefficientEffectiveFrom],
  );

  return (
    <section className="space-y-4 rounded-lg border bg-card p-4">
      <SectionHeader
        description="Дневной пул выручки распределяется между участниками смены по коэффициенту категории и отработанным часам."
        title="Проценты от выручки"
      />

      <div className="space-y-3 border-t pt-4">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h3 className="text-base font-semibold tracking-normal">Tier-таблица выручки</h3>
            <p className="mt-1 text-sm text-muted-foreground">
              Объём от-до и ставка дневного пула.
            </p>
          </div>
          <div className="flex flex-wrap items-end gap-2">
            {advanced ? (
              <LabeledInput
                label="Дата начала версии"
                onChange={(value) => setTierEffectiveFrom(String(value))}
                type="date"
                value={tierEffectiveFrom}
              />
            ) : null}
            <Button
              disabled={!canWrite || isSavingTiers || !tierPayloads}
              onClick={() => tierPayloads && onSaveTiers(tierPayloads)}
            >
              <Save size={16} aria-hidden="true" />
              Сохранить пороги
            </Button>
          </div>
        </div>

        <div className="overflow-x-auto">
          <table className="min-w-[680px] border-separate border-spacing-0 text-sm">
            <thead>
              <tr>
                <th className="border-b p-3 text-left font-medium text-muted-foreground">
                  Выручка от
                </th>
                <th className="border-b p-3 text-left font-medium text-muted-foreground">
                  Выручка до
                </th>
                <th className="border-b p-3 text-left font-medium text-muted-foreground">
                  Процент
                </th>
              </tr>
            </thead>
            <tbody>
              {tierDrafts.map((tier, index) => (
                <tr key={`${index}-${tier.min_revenue}`}>
                  <td className="border-b p-2">
                    <Input
                      disabled={!canWrite}
                      min={0}
                      onChange={(event) =>
                        updateTierDraft(setTierDrafts, index, "min_revenue", event.target.value)
                      }
                      type="number"
                      value={tier.min_revenue}
                    />
                  </td>
                  <td className="border-b p-2">
                    <Input
                      disabled={!canWrite}
                      min={0}
                      onChange={(event) =>
                        updateTierDraft(setTierDrafts, index, "max_revenue", event.target.value)
                      }
                      placeholder="без лимита"
                      type="number"
                      value={tier.max_revenue}
                    />
                  </td>
                  <td className="border-b p-2">
                    <PercentWidget
                      disabled={!canWrite}
                      onChange={(value) =>
                        updateTierDraft(setTierDrafts, index, "rate_percent", Number(value) || 0)
                      }
                      value={tier.rate_percent}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {tierDrafts.length === 0 ? <EmptyConfigLine text="Пороги выручки пока не заданы." /> : null}
        {!tierPayloads ? (
          <div className="text-sm text-destructive">Проверьте границы tier-таблицы.</div>
        ) : null}
      </div>

      <div className="space-y-3 border-t pt-4">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h3 className="text-base font-semibold tracking-normal">Коэффициенты категорий</h3>
            <p className="mt-1 text-sm text-muted-foreground">
              Категории для распределения процентного пула.
            </p>
          </div>
          <div className="flex flex-wrap items-end gap-2">
            {advanced ? (
              <LabeledInput
                label="Дата начала версии"
                onChange={(value) => setCoefficientEffectiveFrom(String(value))}
                type="date"
                value={coefficientEffectiveFrom}
              />
            ) : null}
            <Button
              disabled={!canWrite || isSavingCoefficients || !coefficientPayloads}
              onClick={() => coefficientPayloads && onSaveCoefficients(coefficientPayloads)}
            >
              <Save size={16} aria-hidden="true" />
              Сохранить коэффициенты
            </Button>
          </div>
        </div>

        <div className="overflow-x-auto">
          <table className="min-w-[520px] border-separate border-spacing-0 text-sm">
            <thead>
              <tr>
                <th className="border-b p-3 text-left font-medium text-muted-foreground">
                  Категория
                </th>
                <th className="border-b p-3 text-left font-medium text-muted-foreground">
                  Коэффициент
                </th>
              </tr>
            </thead>
            <tbody>
              {coefficientDrafts.map((row, index) => (
                <tr key={row.category}>
                  <td className="border-b p-3 font-medium">{categoryLabel(row.category)}</td>
                  <td className="border-b p-2">
                    <Input
                      disabled={!canWrite}
                      min={0}
                      onChange={(event) =>
                        updateCoefficientDraft(setCoefficientDrafts, index, event.target.value)
                      }
                      step={0.1}
                      type="number"
                      value={row.coefficient}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!coefficientPayloads ? (
          <div className="text-sm text-destructive">Проверьте коэффициенты категорий.</div>
        ) : null}
      </div>

      <div className="space-y-3 border-t pt-4">
        <h3 className="text-base font-semibold tracking-normal">Распределение процентного пула:</h3>
        <ol className="list-decimal space-y-1 pl-5 text-sm text-muted-foreground">
          <li>Процентный пул = Дневная выручка × Tier rate (см. таблицу tier выше)</li>
          <li>Вес сотрудника = коэффициент его категории × часы смены</li>
          <li>Доля сотрудника = Процентный пул × его вес ÷ сумма весов всех сотрудников смены</li>
        </ol>
        <p className="text-sm text-muted-foreground">
          Коэффициенты нужны для справедливого распределения: сотрудник старшей категории получит
          больше из общего пула, чем стажёр или младшая категория.
        </p>
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

function TerminationReasonsSection({
  advanced,
  canWrite,
  onAdd,
  onEdit,
  reasons,
}: {
  advanced: boolean;
  canWrite: boolean;
  onAdd: () => void;
  onEdit: (reason: EmployeeDismissalReason) => void;
  reasons: EmployeeDismissalReason[];
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
        description="Справочник причин, которые выбираются при увольнении сотрудника в разделе «Штат»."
        title="Причины увольнения"
      />
      <div className="grid gap-2">
        {reasons.map((reason) => (
          <div
            className="grid gap-3 rounded-md border px-3 py-3 lg:grid-cols-[1fr_auto] lg:items-center"
            key={reason.id}
          >
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <div className="font-medium">{reason.label}</div>
                <Badge variant={reason.is_active ? "secondary" : "outline"}>
                  {reason.is_active ? "Активна" : "Скрыта"}
                </Badge>
                {reason.requires_comment ? (
                  <Badge variant="secondary">Комментарий обязателен</Badge>
                ) : null}
              </div>
              {advanced ? (
                <RawMeta value={`${reason.code} · порядок ${reason.sort_order}`} />
              ) : null}
            </div>
            <Button disabled={!canWrite} onClick={() => onEdit(reason)} size="sm" variant="outline">
              <Save size={16} aria-hidden="true" />
              Изменить
            </Button>
          </div>
        ))}
        {reasons.length === 0 ? (
          <EmptyConfigLine text="Причины увольнения пока не заданы." />
        ) : null}
      </div>
    </section>
  );
}

function PremiumsSection({
  advanced,
  canWrite,
  history,
  isSaving,
  onSave,
  premiums,
}: {
  advanced: boolean;
  canWrite: boolean;
  history: PayrollSeniorityPremium[];
  isSaving: boolean;
  onSave: (payloads: PayrollSeniorityPremiumPayload[]) => void;
  premiums: PayrollSeniorityPremium[];
}) {
  const rows = seniorityAllowanceRows();
  const [amounts, setAmounts] = useState<Record<SeniorityAllowanceKey, string>>(() =>
    seniorityAllowanceDraftFromPremiums(premiums),
  );
  const [effectiveFrom, setEffectiveFrom] = useState(todayKey());
  const [confirmOpen, setConfirmOpen] = useState(false);
  const effectiveDates = Array.from(
    new Set(premiums.map((premium) => premium.effective_from).filter(Boolean)),
  );
  const currentLabel =
    effectiveDates.length === 1 ? formatDateOnly(effectiveDates[0]) : "разные даты";
  const dateIsValid = effectiveFrom >= todayKey();
  const amountValues = rows.map((row) => parseNonNegativeNumber(amounts[row.key] ?? ""));
  const canSubmit = canWrite && dateIsValid && amountValues.every((value) => value !== undefined);

  useEffect(() => {
    setAmounts(seniorityAllowanceDraftFromPremiums(premiums));
  }, [premiums]);

  function buildPayloads(): PayrollSeniorityPremiumPayload[] {
    return rows.map((row) => ({
      position: row.position,
      role: row.role,
      amount: parseNonNegativeNumber(amounts[row.key] ?? "") ?? 0,
      effective_from: effectiveFrom,
    }));
  }

  const groupedHistory = groupSeniorityAllowanceHistory(history);

  return (
    <section className="space-y-4 rounded-lg border bg-card p-4">
      <SectionHeader
        description="Фиксированные суммы за полную 12-часовую смену. В расчёте они применяются по должности, роли и дате смены."
        title="Надбавки старший/зам"
      />
      <div className="text-sm text-muted-foreground">Действуют с {currentLabel}</div>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_260px]">
        <div className="grid gap-5">
          {(["Повар", "Кассир"] as const).map((position) => (
            <div className="space-y-3" key={position}>
              <div className="text-sm font-medium">
                {position === "Повар" ? "Повара" : "Администраторы (кассиры)"}
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                {rows
                  .filter((row) => row.position === position)
                  .map((row) => (
                    <div className="grid gap-2" key={row.key}>
                      <Label htmlFor={`seniority-${row.key}`}>{row.label}</Label>
                      <div className="flex items-center gap-2">
                        <Input
                          disabled={!canWrite || isSaving}
                          id={`seniority-${row.key}`}
                          min="0"
                          onChange={(event) =>
                            setAmounts((current) => ({
                              ...current,
                              [row.key]: event.target.value,
                            }))
                          }
                          type="number"
                          value={amounts[row.key] ?? ""}
                        />
                        <span className="text-sm text-muted-foreground">₽</span>
                      </div>
                    </div>
                  ))}
              </div>
            </div>
          ))}
        </div>

        <div className="space-y-3">
          <div className="grid gap-2">
            <Label htmlFor="seniority-effective-from">Действуют с</Label>
            <Input
              disabled={!canWrite || isSaving}
              id="seniority-effective-from"
              min={todayKey()}
              onChange={(event) => setEffectiveFrom(event.target.value)}
              type="date"
              value={effectiveFrom}
            />
            {!dateIsValid ? (
              <div className="text-sm text-destructive">Дата не должна быть раньше сегодня.</div>
            ) : null}
          </div>
          <Button
            className="w-full"
            disabled={!canSubmit || isSaving}
            onClick={() => setConfirmOpen(true)}
          >
            <Save size={16} aria-hidden="true" />
            {isSaving ? "Сохранение..." : "Сохранить новую версию"}
          </Button>
          {advanced ? <RawMeta value="position × role fixed amount" /> : null}
        </div>
      </div>

      <details className="rounded-md border px-3 py-2">
        <summary className="flex cursor-pointer items-center gap-2 text-sm font-medium">
          <History size={16} aria-hidden="true" />
          История
        </summary>
        <div className="mt-3 grid gap-2">
          {groupedHistory.length === 0 ? (
            <EmptyConfigLine text="История надбавок пока не задана." />
          ) : (
            groupedHistory.map((version) => (
              <div
                className="flex flex-col gap-1 rounded-md bg-muted/30 px-3 py-2 text-sm sm:flex-row sm:items-center sm:justify-between"
                key={version.effective_from}
              >
                <span>
                  {formatDateOnly(version.effective_from)} —{" "}
                  {version.isCurrent ? "текущее" : "закрыто"}
                </span>
                <span className="tabular-nums text-muted-foreground">
                  {version.amounts.map((amount) => formatMoney(amount)).join(" / ")}
                </span>
              </div>
            ))
          )}
        </div>
      </details>

      <AlertDialog onOpenChange={setConfirmOpen} open={confirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Создать новую версию ставок?</AlertDialogTitle>
            <AlertDialogDescription>
              Создать версию ставок с {formatDateOnly(effectiveFrom)}? Прежняя версия будет
              закрыта.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isSaving}>Отмена</AlertDialogCancel>
            <AlertDialogAction
              disabled={!canSubmit || isSaving}
              onClick={() => {
                onSave(buildPayloads());
                setConfirmOpen(false);
              }}
            >
              Сохранить новую версию
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}

function FundSection({ canWrite }: { canWrite: boolean }) {
  const queryClient = useQueryClient();
  const currentYear = new Date().getFullYear();
  const [tierDrafts, setTierDrafts] = useState<FundTierDraft[]>([]);
  const [tierMessage, setTierMessage] = useState<string | null>(null);
  const [rosterFilter, setRosterFilter] = useState<FundRosterFilter>("all");
  const [balanceDrafts, setBalanceDrafts] = useState<Record<string, string>>({});
  const [pendingInitialBalance, setPendingInitialBalance] = useState<PendingInitialBalance | null>(
    null,
  );
  const [pendingFundExclusion, setPendingFundExclusion] =
    useState<PendingFundExclusion | null>(null);
  const [pendingFundReturn, setPendingFundReturn] = useState<FundRosterRow | null>(null);

  const tiersQuery = useQuery({
    queryKey: ["fund-tiers"],
    queryFn: getFundTiers,
  });
  const rosterQuery = useQuery({
    queryKey: ["fund-roster", currentYear],
    queryFn: () => getFundInitialBalanceRoster(currentYear),
  });

  useEffect(() => {
    if (tiersQuery.data) {
      setTierDrafts(fundTiersToDrafts(tiersQuery.data.tiers));
      setTierMessage(null);
    }
  }, [tiersQuery.data]);

  const tierValidation = useMemo(() => validateFundTierDrafts(tierDrafts), [tierDrafts]);
  const serverTierKey = useMemo(
    () => fundTierKey(tiersQuery.data?.tiers ?? []),
    [tiersQuery.data?.tiers],
  );
  const draftTierKey = useMemo(
    () => (tierValidation.payload ? fundTierKey(tierValidation.payload) : ""),
    [tierValidation.payload],
  );
  const hasTierChanges = Boolean(tierValidation.payload) && draftTierKey !== serverTierKey;

  const tiersMutation = useMutation({
    mutationFn: putFundTiers,
    onSuccess: async () => {
      toast.success("Ставки фонда обновлены");
      setTierMessage(null);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["fund-tiers"] }),
        queryClient.invalidateQueries({ queryKey: ["fund-roster"] }),
      ]);
    },
    onError: (error) => {
      setTierMessage(apiErrorMessage(error, "Не удалось сохранить ставки фонда"));
    },
  });

  const initialBalanceMutation = useMutation({
    mutationFn: ({ amount, employeeId }: { employeeId: string; amount: number }) =>
      setFundInitialBalance(employeeId, { amount }),
    onSuccess: async (_, variables) => {
      toast.success("Начальный баланс установлен");
      setBalanceDrafts((current) => {
        const next = { ...current };
        delete next[variables.employeeId];
        return next;
      });
      await queryClient.invalidateQueries({ queryKey: ["fund-roster"] });
    },
    onError: async (error) => {
      if (apiErrorStatus(error) === 409) {
        toast.error("Баланс уже был установлен другим пользователем");
        await queryClient.invalidateQueries({ queryKey: ["fund-roster"] });
        return;
      }
      toast.error(apiErrorMessage(error, "Не удалось установить начальный баланс"));
    },
  });

  const exclusionMutation = useMutation({
    mutationFn: ({
      employeeId,
      excluded,
      reason,
      until,
    }: {
      employeeId: string;
      excluded: boolean;
      until?: string | null;
      reason?: string | null;
    }) =>
      patchFundExclusion(employeeId, {
        fund_excluded: excluded,
        fund_excluded_until: until,
        fund_excluded_reason: reason,
      }),
    onSuccess: async (_, variables) => {
      toast.success(
        variables.excluded ? "Сотрудник исключён из фонда" : "Сотрудник возвращён в фонд",
      );
      await queryClient.invalidateQueries({ queryKey: ["fund-roster"] });
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось обновить исключение из фонда"));
    },
  });

  const rosterRows = rosterQuery.data ?? [];
  const filteredRows = rosterRows.filter((row) => {
    const isExcluded = row.fund_exclusion.is_currently_excluded;
    if (rosterFilter === "active") {
      return !isExcluded;
    }
    if (rosterFilter === "excluded") {
      return isExcluded;
    }
    return true;
  });

  return (
    <div className="space-y-5">
      <section className="space-y-4 rounded-lg border bg-card p-4">
        <SectionHeader
          description="Процент отчислений в зависимости от стажа."
          title="Ставки по стажу"
          action={
            <div className="flex flex-wrap gap-2">
              <Button
                disabled={!canWrite || !hasTierChanges || tiersMutation.isPending}
                onClick={() => {
                  if (tierValidation.payload) {
                    tiersMutation.mutate(tierValidation.payload);
                  }
                }}
              >
                <Save size={16} aria-hidden="true" />
                Сохранить
              </Button>
              <Button
                disabled={!hasTierChanges || tiersMutation.isPending}
                onClick={() => {
                  setTierDrafts(fundTiersToDrafts(tiersQuery.data?.tiers ?? []));
                  setTierMessage(null);
                }}
                variant="outline"
              >
                Отмена
              </Button>
            </div>
          }
        />

        <div className="overflow-x-auto">
          <table className="min-w-[560px] border-separate border-spacing-0 text-sm">
            <thead>
              <tr>
                <th className="border-b p-3 text-left font-medium text-muted-foreground">
                  От стажа (месяцев)
                </th>
                <th className="border-b p-3 text-left font-medium text-muted-foreground">
                  Процент
                </th>
                <th className="w-16 border-b p-3" />
              </tr>
            </thead>
            <tbody>
              {tierDrafts.map((tier, index) => {
                const rowError = tierValidation.errors[tier.id];
                return (
                  <Fragment key={tier.id}>
                    <tr>
                      <td className="border-b p-2">
                        <Input
                          disabled={!canWrite}
                          min={0}
                          max={600}
                          onChange={(event) =>
                            updateFundTierDraft(setTierDrafts, index, {
                              minMonths: event.target.value,
                            })
                          }
                          type="number"
                          value={tier.minMonths}
                        />
                      </td>
                      <td className="border-b p-2">
                        <div className="flex items-center gap-2">
                          <Input
                            disabled={!canWrite}
                            min={0}
                            max={100}
                            onChange={(event) =>
                              updateFundTierDraft(setTierDrafts, index, {
                                ratePercent: event.target.value,
                              })
                            }
                            step="0.001"
                            type="number"
                            value={tier.ratePercent}
                          />
                          <span className="text-sm text-muted-foreground">%</span>
                        </div>
                      </td>
                      <td className="border-b p-2 text-right">
                        <Button
                          disabled={!canWrite}
                          onClick={() => {
                            if (tierDrafts.length <= 1) {
                              setTierMessage("Должен остаться минимум один порог фонда");
                              return;
                            }
                            setTierDrafts((drafts) =>
                              drafts.filter((_, draftIndex) => draftIndex !== index),
                            );
                          }}
                          size="icon"
                          title="Удалить порог"
                          type="button"
                          variant="ghost"
                        >
                          <Trash2 aria-hidden="true" />
                        </Button>
                      </td>
                    </tr>
                    {rowError ? (
                      <tr>
                        <td className="border-b px-2 pb-3 text-sm text-destructive" colSpan={3}>
                          {rowError}
                        </td>
                      </tr>
                    ) : null}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3">
          <Button
            disabled={!canWrite || tierDrafts.length >= 10}
            onClick={() =>
              setTierDrafts((drafts) => [
                ...drafts,
                { id: draftId(), minMonths: "", ratePercent: "" },
              ])
            }
            variant="outline"
          >
            <Plus size={16} aria-hidden="true" />
            Добавить порог
          </Button>
          {tiersQuery.isLoading ? (
            <span className="text-sm text-muted-foreground">Загрузка ставок...</span>
          ) : null}
        </div>
        {tierValidation.formError ? (
          <div className="text-sm text-destructive">{tierValidation.formError}</div>
        ) : null}
        {tiersQuery.isError ? (
          <div className="text-sm text-destructive">Не удалось загрузить ставки фонда</div>
        ) : null}
        {tierMessage ? <div className="text-sm text-destructive">{tierMessage}</div> : null}
      </section>

      <section className="space-y-4 rounded-lg border bg-card p-4">
        <SectionHeader
          description="Единоразовый ввод текущего накопления сотрудника. После установки изменить нельзя."
          title={`Начальные балансы за ${currentYear} год`}
          action={
            <div className="w-full min-w-[220px] sm:w-[260px]">
              <Select
                onValueChange={(value) => setRosterFilter(value as FundRosterFilter)}
                value={rosterFilter}
              >
                <SelectTrigger>
                  <SelectValue placeholder="Фильтр" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">Все</SelectItem>
                  <SelectItem value="active">Активные начисления</SelectItem>
                  <SelectItem value="excluded">Исключённые</SelectItem>
                </SelectContent>
              </Select>
            </div>
          }
        />

        {rosterQuery.isError ? (
          <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
            Не удалось загрузить сотрудников фонда
          </div>
        ) : rosterQuery.isLoading ? (
          <EmptyConfigLine text="Загрузка сотрудников..." />
        ) : rosterRows.length === 0 ? (
          <EmptyConfigLine text="Активных сотрудников с фондом нет" />
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-[1040px] border-separate border-spacing-0 text-sm">
              <thead>
                <tr>
                  <th className="border-b p-3 text-left font-medium text-muted-foreground">ФИО</th>
                  <th className="border-b p-3 text-left font-medium text-muted-foreground">
                    Должность
                  </th>
                  <th className="border-b p-3 text-left font-medium text-muted-foreground">Стаж</th>
                  <th className="border-b p-3 text-left font-medium text-muted-foreground">%</th>
                  <th className="border-b p-3 text-left font-medium text-muted-foreground">
                    Исключён из фонда
                  </th>
                  <th className="border-b p-3 text-left font-medium text-muted-foreground">
                    Баланс
                  </th>
                  <th className="w-32 border-b p-3" />
                </tr>
              </thead>
              <tbody>
                {filteredRows.map((row) => {
                  const account = row.fund_account;
                  const isInitialSet = Boolean(account?.is_initial_set);
                  const draftValue = balanceDrafts[row.employee_id] ?? "";
                  const amount = parseBalanceAmount(draftValue);
                  const fundExclusion = row.fund_exclusion;
                  const isFundExcluded = fundExclusion.is_currently_excluded;
                  return (
                    <tr key={row.employee_id}>
                      <td className="border-b p-3 font-medium">{row.full_name}</td>
                      <td className="border-b p-3">{row.position ?? "—"}</td>
                      <td className="border-b p-3">
                        {row.hire_date === null ? (
                          <span
                            className="text-muted-foreground"
                            title="Не указана дата приёма. Установите её в карточке сотрудника."
                          >
                            —
                          </span>
                        ) : (
                          `${row.tenure_months ?? 0}м`
                        )}
                      </td>
                      <td className="border-b p-3">
                        {row.hire_date === null ? (
                          <span
                            className="text-muted-foreground"
                            title="Не указана дата приёма. Установите её в карточке сотрудника."
                          >
                            —
                          </span>
                        ) : (
                          formatFundPercent(row.current_rate_percent)
                        )}
                      </td>
                      <td className="border-b p-3">
                        <div className="flex min-w-[210px] items-center gap-2">
                          <Switch
                            aria-label={`Исключить ${row.full_name} из накопительного фонда`}
                            checked={isFundExcluded}
                            disabled={!canWrite || exclusionMutation.isPending}
                            onCheckedChange={(checked) => {
                              if (checked) {
                                setPendingFundExclusion({
                                  row,
                                  until: fundExclusion.fund_excluded_until ?? "",
                                  reason: fundExclusion.fund_excluded_reason ?? "",
                                });
                                return;
                              }
                              setPendingFundReturn(row);
                            }}
                            title="Прекратит начисление накопительного фонда. Накопленное остаётся и будет выплачено 15 января."
                          />
                          {isFundExcluded ? (
                            <Badge
                              className="border-amber-300 bg-amber-100 text-amber-900 hover:bg-amber-100"
                              title={fundExclusion.fund_excluded_reason ?? undefined}
                              variant="outline"
                            >
                              {fundExclusion.fund_excluded_until
                                ? `Исключён до ${formatDateOnly(
                                    fundExclusion.fund_excluded_until,
                                  )}`
                                : "Исключён бессрочно"}
                            </Badge>
                          ) : (
                            <span className="text-xs text-muted-foreground">Активно</span>
                          )}
                        </div>
                      </td>
                      <td className="border-b p-3">
                        {isInitialSet && account ? (
                          <div>
                            <div className="font-medium tabular-nums">
                              {formatMoney(account.accumulated)}
                            </div>
                            <div className="text-xs text-muted-foreground">
                              установлен{" "}
                              {account.initial_set_at
                                ? formatDate(account.initial_set_at)
                                : "без даты"}
                            </div>
                          </div>
                        ) : (
                          <div className="flex items-center gap-2">
                            <Input
                              className="w-36"
                              disabled={!canWrite}
                              min={0}
                              onChange={(event) =>
                                setBalanceDrafts((current) => ({
                                  ...current,
                                  [row.employee_id]: event.target.value,
                                }))
                              }
                              placeholder="0"
                              step="0.01"
                              type="number"
                              value={draftValue}
                            />
                            <span className="text-muted-foreground">₽</span>
                          </div>
                        )}
                      </td>
                      <td className="border-b p-3 text-right">
                        {isInitialSet ? (
                          <span
                            className="inline-flex h-9 w-9 items-center justify-center rounded-md border text-emerald-700"
                            title="Начальный баланс установлен"
                          >
                            <Check aria-hidden="true" className="h-4 w-4" />
                          </span>
                        ) : (
                          <Button
                            disabled={
                              !canWrite || amount === undefined || initialBalanceMutation.isPending
                            }
                            onClick={() => {
                              if (amount !== undefined) {
                                setPendingInitialBalance({ row, amount });
                              }
                            }}
                            size="sm"
                          >
                            <Save size={16} aria-hidden="true" />
                            Сохранить
                          </Button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {filteredRows.length === 0 ? (
              <div className="px-3 py-6 text-center text-sm text-muted-foreground">
                Нет сотрудников для выбранного фильтра.
              </div>
            ) : null}
          </div>
        )}
      </section>

      <AlertDialog
        onOpenChange={(open) => {
          if (!open) {
            setPendingInitialBalance(null);
          }
        }}
        open={Boolean(pendingInitialBalance)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Установить начальный баланс?</AlertDialogTitle>
            <AlertDialogDescription>
              {pendingInitialBalance
                ? `Установить начальный баланс ${formatMoney(
                    pendingInitialBalance.amount,
                  )} для ${pendingInitialBalance.row.full_name}? Изменить будет нельзя.`
                : "Подтвердите установку начального баланса."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Отмена</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                const pending = pendingInitialBalance;
                setPendingInitialBalance(null);
                if (pending) {
                  initialBalanceMutation.mutate({
                    employeeId: pending.row.employee_id,
                    amount: pending.amount,
                  });
                }
              }}
            >
              Установить
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <Dialog
        onOpenChange={(open) => {
          if (!open) {
            setPendingFundExclusion(null);
          }
        }}
        open={Boolean(pendingFundExclusion)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Исключить из накопительного фонда</DialogTitle>
            <DialogDescription>
              {pendingFundExclusion
                ? `${pendingFundExclusion.row.full_name}: начисления прекратятся с текущей даты.`
                : "Начисления прекратятся с текущей даты."}
            </DialogDescription>
          </DialogHeader>
          {pendingFundExclusion ? (
            <div className="grid gap-4">
              <div className="grid gap-2">
                <Label htmlFor="fund-exclusion-until">Исключить до</Label>
                <Input
                  id="fund-exclusion-until"
                  onChange={(event) =>
                    setPendingFundExclusion({
                      ...pendingFundExclusion,
                      until: event.target.value,
                    })
                  }
                  type="date"
                  value={pendingFundExclusion.until}
                />
                <div className="text-xs text-muted-foreground">Пусто = бессрочно</div>
              </div>
              <div className="grid gap-2">
                <Label htmlFor="fund-exclusion-reason">Причина</Label>
                <Textarea
                  id="fund-exclusion-reason"
                  maxLength={1000}
                  onChange={(event) =>
                    setPendingFundExclusion({
                      ...pendingFundExclusion,
                      reason: event.target.value,
                    })
                  }
                  value={pendingFundExclusion.reason}
                />
              </div>
            </div>
          ) : null}
          <DialogFooter>
            <Button
              onClick={() => setPendingFundExclusion(null)}
              type="button"
              variant="outline"
            >
              Отмена
            </Button>
            <Button
              disabled={exclusionMutation.isPending}
              onClick={() => {
                const pending = pendingFundExclusion;
                setPendingFundExclusion(null);
                if (pending) {
                  exclusionMutation.mutate({
                    employeeId: pending.row.employee_id,
                    excluded: true,
                    until: pending.until || null,
                    reason: pending.reason.trim() || null,
                  });
                }
              }}
              type="button"
            >
              Исключить
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog
        onOpenChange={(open) => {
          if (!open) {
            setPendingFundReturn(null);
          }
        }}
        open={Boolean(pendingFundReturn)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Вернуть сотрудника в начисление фонда?</AlertDialogTitle>
            <AlertDialogDescription>
              {pendingFundReturn
                ? `Вернуть ${pendingFundReturn.full_name} в начисление фонда?`
                : "Подтвердите возврат в начисление фонда."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Отмена</AlertDialogCancel>
            <AlertDialogAction
              disabled={exclusionMutation.isPending}
              onClick={() => {
                const pending = pendingFundReturn;
                setPendingFundReturn(null);
                if (pending) {
                  exclusionMutation.mutate({
                    employeeId: pending.employee_id,
                    excluded: false,
                  });
                }
              }}
            >
              Вернуть
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
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

function TerminationReasonDialog({
  advanced,
  draft,
  isSaving,
  onChange,
  onOpenChange,
  onSave,
}: {
  advanced: boolean;
  draft: TerminationReasonDraft | null;
  isSaving: boolean;
  onChange: (value: TerminationReasonDraft | null) => void;
  onOpenChange: (open: boolean) => void;
  onSave: (payload: TerminationReasonDraft) => void;
}) {
  const canSave = Boolean(draft?.label.trim()) && !isSaving;
  return (
    <Dialog onOpenChange={onOpenChange} open={Boolean(draft)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Причина увольнения</DialogTitle>
          <DialogDescription>Справочник для формы увольнения сотрудника.</DialogDescription>
        </DialogHeader>
        {draft ? (
          <div className="grid gap-4">
            <LabeledInput
              label="Название"
              onChange={(value) => onChange({ ...draft, label: String(value) })}
              value={draft.label}
            />
            {advanced ? (
              draft.id ? (
                <div className="rounded-md bg-muted px-3 py-2 text-sm font-mono">{draft.code}</div>
              ) : (
                <LabeledInput
                  label="Код"
                  onChange={(value) => onChange({ ...draft, code: String(value) })}
                  value={draft.code}
                />
              )
            ) : null}
            <LabeledInput
              label="Порядок"
              onChange={(value) =>
                onChange({
                  ...draft,
                  sort_order: Math.max(0, Number.parseInt(String(value), 10) || 0),
                })
              }
              type="number"
              value={String(draft.sort_order)}
            />
            <div className="flex items-center justify-between gap-4 rounded-md border px-3 py-3">
              <Label>Комментарий обязателен</Label>
              <BooleanWidget
                onChange={(value) => onChange({ ...draft, requires_comment: Boolean(value) })}
                value={draft.requires_comment}
              />
            </div>
            <div className="flex items-center justify-between gap-4 rounded-md border px-3 py-3">
              <Label>Активна</Label>
              <BooleanWidget
                onChange={(value) => onChange({ ...draft, is_active: Boolean(value) })}
                value={draft.is_active}
              />
            </div>
          </div>
        ) : null}
        <DialogFooter>
          <Button onClick={() => onOpenChange(false)} variant="outline">
            Отмена
          </Button>
          <Button disabled={!canSave} onClick={() => draft && onSave(draft)}>
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

function fundTiersToDrafts(tiers: FundTierItem[]): FundTierDraft[] {
  return [...tiers]
    .sort((left, right) => Number(left.min_months) - Number(right.min_months))
    .map((tier) => ({
      id: draftId(),
      minMonths: String(tier.min_months),
      ratePercent: formatDraftNumber(Number(tier.rate) * 100),
    }));
}

function validateFundTierDrafts(drafts: FundTierDraft[]): {
  errors: Record<string, string>;
  formError: string | null;
  payload: FundTierItem[] | null;
} {
  const errors: Record<string, string> = {};
  let formError: string | null = null;
  const parsedRows: Array<{ id: string; minMonths: number; ratePercent: number }> = [];

  if (drafts.length === 0) {
    formError = "Добавьте хотя бы один порог фонда";
  }
  if (drafts.length > 10) {
    formError = "Можно указать не больше 10 порогов фонда";
  }

  for (const draft of drafts) {
    const minMonths = Number(draft.minMonths);
    const ratePercent = Number(draft.ratePercent);
    if (
      draft.minMonths.trim() === "" ||
      !Number.isInteger(minMonths) ||
      minMonths < 0 ||
      minMonths > 600
    ) {
      errors[draft.id] = "Укажите стаж целым числом от 0 до 600 месяцев";
      continue;
    }
    if (
      draft.ratePercent.trim() === "" ||
      !Number.isFinite(ratePercent) ||
      ratePercent < 0 ||
      ratePercent > 100
    ) {
      errors[draft.id] = "Укажите процент от 0 до 100";
      continue;
    }
    parsedRows.push({ id: draft.id, minMonths, ratePercent });
  }

  const monthCounts = new Map<number, number>();
  parsedRows.forEach((row) =>
    monthCounts.set(row.minMonths, (monthCounts.get(row.minMonths) ?? 0) + 1),
  );
  parsedRows.forEach((row) => {
    if ((monthCounts.get(row.minMonths) ?? 0) > 1) {
      errors[row.id] = "Порог стажа должен быть уникальным";
    }
  });

  const sortedRows = [...parsedRows].sort((left, right) => left.minMonths - right.minMonths);
  sortedRows.forEach((row, index) => {
    const previous = sortedRows[index - 1];
    if (previous && row.ratePercent < previous.ratePercent) {
      errors[row.id] = "Процент не может уменьшаться с ростом стажа";
    }
  });

  const hasErrors = formError !== null || Object.keys(errors).length > 0;
  return {
    errors,
    formError,
    payload: hasErrors
      ? null
      : sortedRows.map((row) => ({
          min_months: row.minMonths,
          rate: Number((row.ratePercent / 100).toFixed(8)),
        })),
  };
}

function updateFundTierDraft(
  setTierDrafts: (updater: (value: FundTierDraft[]) => FundTierDraft[]) => void,
  index: number,
  patch: Partial<Omit<FundTierDraft, "id">>,
) {
  setTierDrafts((drafts) =>
    drafts.map((draft, draftIndex) => (draftIndex === index ? { ...draft, ...patch } : draft)),
  );
}

function fundTierKey(tiers: FundTierItem[]) {
  return JSON.stringify(
    [...tiers]
      .map((tier) => ({
        min_months: Number(tier.min_months),
        rate: Number(Number(tier.rate).toFixed(8)),
      }))
      .sort((left, right) => left.min_months - right.min_months),
  );
}

function draftId() {
  return `draft-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function formatDraftNumber(value: number) {
  if (!Number.isFinite(value)) {
    return "";
  }
  return Number(value.toFixed(6)).toString();
}

function parseBalanceAmount(value: string) {
  if (value.trim() === "") {
    return undefined;
  }
  const amount = Number(value);
  return Number.isFinite(amount) && amount >= 0 ? amount : undefined;
}

function formatFundPercent(value: string | null) {
  const percent = Number(value);
  if (!Number.isFinite(percent)) {
    return "—";
  }
  return `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 3 }).format(percent)}%`;
}

async function invalidatePayrollConfig(queryClient: ReturnType<typeof useQueryClient>) {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: ["payroll-config", "rates"] }),
    queryClient.invalidateQueries({ queryKey: ["payroll-config", "availability"] }),
    queryClient.invalidateQueries({ queryKey: ["payroll-config", "revenue-tiers"] }),
    queryClient.invalidateQueries({ queryKey: ["payroll-config", "category-coefficients"] }),
    queryClient.invalidateQueries({ queryKey: ["payroll-config", "deductions"] }),
    queryClient.invalidateQueries({ queryKey: ["payroll-config", "seniority-premium"] }),
    queryClient.invalidateQueries({ queryKey: ["fund-tiers"] }),
    queryClient.invalidateQueries({ queryKey: ["fund-roster"] }),
  ]);
}

async function invalidateTerminationReasons(queryClient: ReturnType<typeof useQueryClient>) {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: ["employees", "dismissal-reasons"] }),
    queryClient.invalidateQueries({ queryKey: ["employees", "dismissal-reasons", "all"] }),
  ]);
}

function nextSortOrder(reasons: EmployeeDismissalReason[]) {
  return (Math.max(0, ...reasons.map((reason) => reason.sort_order)) || 0) + 10;
}

const PAYROLL_RATE_CATEGORIES = [
  "category_1",
  "category_2",
  "category_3",
  "category_4",
  "intern",
  "freelancer",
];

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

function buildRevenueTierPayloads(
  drafts: RevenueTierDraft[],
  effectiveFrom: string,
): PayrollRevenueTierPayload[] | null {
  const rows: PayrollRevenueTierPayload[] = [];
  for (const draft of drafts) {
    const minRevenue = parseNonNegativeNumber(draft.min_revenue);
    const maxRevenue = parseOptionalNonNegativeNumber(draft.max_revenue);
    if (minRevenue === undefined || maxRevenue === undefined || draft.rate_percent < 0) {
      return null;
    }
    rows.push({
      min_revenue: minRevenue,
      max_revenue: maxRevenue,
      rate_percent: draft.rate_percent,
      effective_from: effectiveFrom,
      effective_to: null,
    });
  }
  const sortedRows = rows.sort((left, right) => left.min_revenue - right.min_revenue);
  const hasOverlap = sortedRows.some((row, index) => {
    const next = sortedRows[index + 1];
    return Boolean(next && row.max_revenue !== null && row.max_revenue > next.min_revenue);
  });
  const hasOpenMiddleTier = sortedRows.some(
    (row, index) => row.max_revenue === null && index !== sortedRows.length - 1,
  );
  return hasOverlap || hasOpenMiddleTier ? null : sortedRows;
}

function buildCategoryCoefficientPayloads(
  drafts: CategoryCoefficientDraft[],
  effectiveFrom: string,
): PayrollCategoryCoefficientPayload[] | null {
  const rows: PayrollCategoryCoefficientPayload[] = [];
  for (const draft of drafts) {
    const coefficient = parseNonNegativeNumber(draft.coefficient);
    if (coefficient === undefined) {
      return null;
    }
    rows.push({
      category: draft.category,
      coefficient,
      effective_from: effectiveFrom,
      effective_to: null,
    });
  }
  return rows;
}

function updateTierDraft(
  setTierDrafts: (updater: (value: RevenueTierDraft[]) => RevenueTierDraft[]) => void,
  index: number,
  key: keyof RevenueTierDraft,
  value: string | number,
) {
  setTierDrafts((drafts) =>
    drafts.map((draft, draftIndex) => (draftIndex === index ? { ...draft, [key]: value } : draft)),
  );
}

function updateCoefficientDraft(
  setCoefficientDrafts: (
    updater: (value: CategoryCoefficientDraft[]) => CategoryCoefficientDraft[],
  ) => void,
  index: number,
  value: string,
) {
  setCoefficientDrafts((drafts) =>
    drafts.map((draft, draftIndex) =>
      draftIndex === index ? { ...draft, coefficient: value } : draft,
    ),
  );
}

function parseNonNegativeNumber(value: string) {
  const numberValue = Number(value);
  if (!Number.isFinite(numberValue) || numberValue < 0 || value.trim() === "") {
    return undefined;
  }
  return numberValue;
}

function parseOptionalNonNegativeNumber(value: string) {
  if (value.trim() === "") {
    return null;
  }
  return parseNonNegativeNumber(value);
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

type SeniorityAllowanceKey =
  | "cook-senior"
  | "cook-deputy_senior"
  | "cashier-senior"
  | "cashier-deputy_senior";

type SeniorityAllowanceRow = {
  key: SeniorityAllowanceKey;
  position: PayrollSeniorityPremium["position"];
  role: PayrollSeniorityPremium["role"];
  label: string;
};

function seniorityAllowanceRows(): SeniorityAllowanceRow[] {
  return [
    {
      key: "cook-senior",
      position: "Повар",
      role: "senior",
      label: "Старший повар",
    },
    {
      key: "cook-deputy_senior",
      position: "Повар",
      role: "deputy_senior",
      label: "Зам старшего повара",
    },
    {
      key: "cashier-senior",
      position: "Кассир",
      role: "senior",
      label: "Старший администратор",
    },
    {
      key: "cashier-deputy_senior",
      position: "Кассир",
      role: "deputy_senior",
      label: "Зам старшего администратора",
    },
  ];
}

function seniorityAllowanceDraftFromPremiums(
  premiums: PayrollSeniorityPremium[],
): Record<SeniorityAllowanceKey, string> {
  return Object.fromEntries(
    seniorityAllowanceRows().map((row) => {
      const premium = premiums.find(
        (item) => item.position === row.position && item.role === row.role,
      );
      return [row.key, String(premium?.amount ?? 0)];
    }),
  ) as Record<SeniorityAllowanceKey, string>;
}

function groupSeniorityAllowanceHistory(history: PayrollSeniorityPremium[]) {
  const rows = seniorityAllowanceRows();
  const grouped = new Map<string, PayrollSeniorityPremium[]>();
  for (const premium of history) {
    const group = grouped.get(premium.effective_from) ?? [];
    group.push(premium);
    grouped.set(premium.effective_from, group);
  }
  return Array.from(grouped.entries())
    .sort(([left], [right]) => right.localeCompare(left))
    .map(([effective_from, items]) => ({
      effective_from,
      isCurrent: items.some((item) => item.effective_to === null),
      amounts: rows.map(
        (row) =>
          items.find((item) => item.position === row.position && item.role === row.role)?.amount ??
          0,
      ),
    }));
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

function formatMoney(value: number | string | null) {
  if (value === null) {
    return "Без суммы";
  }
  const numberValue = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(numberValue)) {
    return String(value);
  }
  return new Intl.NumberFormat("ru-RU", {
    maximumFractionDigits: 0,
    style: "currency",
    currency: "RUB",
  }).format(numberValue);
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

function formatDateOnly(value: string) {
  const [year, month, day] = value.split("-").map(Number);
  if (!year || !month || !day) {
    return value;
  }
  return new Intl.DateTimeFormat("ru-RU").format(new Date(year, month - 1, day));
}
