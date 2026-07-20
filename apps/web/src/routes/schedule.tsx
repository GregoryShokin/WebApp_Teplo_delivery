import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowUpDown,
  BarChart3,
  Calculator,
  CalendarDays,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  CircleSlash,
  ExternalLink,
  History,
  LoaderCircle,
  Lock,
  Maximize2,
  Minimize2,
  MoreHorizontal,
  Pencil,
  Plus,
  RefreshCw,
  Users,
} from "lucide-react";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type ReactNode,
  type SetStateAction,
} from "react";
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
import { DateRangePopover } from "@/components/ui/date-range-popover";
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
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
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
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { EmptyState } from "@/components/ui-app/EmptyState";
import { PageHeader } from "@/components/ui-app/PageHeader";
import { PayrollDailyLedgerRoute } from "@/routes/payroll/daily-ledger";
import { DishwasherScheduleSection } from "@/routes/schedule/dishwashers";
import { VacationsRoute } from "@/routes/shifts/vacations";
import {
  apiErrorMessage,
  copyWeek,
  createNewVersion,
  createSchedule,
  deleteCashierAllowanceOverride,
  deleteSchedule,
  deleteShift,
  getEmployeesRoster,
  getForecastRange,
  getLatestRun,
  getPlanFact,
  getRun,
  getScheduleLedger,
  getSchedule,
  getLivingSchedule,
  getVacationRoster,
  listRuns,
  listSchedules,
  overrideForecast,
  publishSchedule,
  reopenSchedule,
  recomputeForecast,
  removeForecastOverride,
  resolveCashierAllowance,
  runCostForecast,
  listCashierAllowanceOverrides,
  upsertShift,
  upsertCashierAllowanceOverride,
  type AllowanceAssignmentRead,
  type CashierAllowanceOverridePayload,
  type EmployeeRosterRow,
  type PayrollForecastRunRead,
  type PlanFactDayRowRead,
  type PlanFactDeviationStatus,
  type PlanFactEmployeeRowRead,
  type PlanFactSummaryRead,
  type RevenueForecastRead,
  type RevenueForecastRecomputePayload,
  type ScheduleCreatePayload,
  type ScheduleRead,
  type ShiftCostEstimateRead,
  type ShiftAllowanceOverrideRead,
  type ScheduledShiftRead,
  type ScheduleLedgerEntryRead,
  type ScheduledShiftUpsertPayload,
  type VacationPeriodRead,
  type VacationRosterRow,
} from "@/lib/api";
import {
  rangeForPreset,
  type PeriodPreset,
  type PeriodRange,
} from "@/lib/date-presets";
import { PAYROLL_ROLE_LABELS } from "@/lib/i18n/employee";
import { usePermissions } from "@/lib/permissions";
import { roleColorClasses } from "@/lib/role-colors";
import { sortEmployeesByRoleAndName } from "@/lib/role-sort";
import { cn } from "@/lib/utils";

type ViewMode = "employees" | "stations" | "planFact";
type ScheduleActiveTab = "schedule" | "shifts-ledger" | "vacations" | "dishwashers";
type PlanFactTableMode = "days" | "employees";
type SortDirection = "asc" | "desc";
type DaySortKey = "date" | "hours" | "cost" | "status";
type EmployeeSortKey = "name" | "hours" | "cost" | "status";

type ShiftDialogState = {
  mode: "create" | "edit";
  shift: ScheduledShiftRead | null;
  employeeId: string;
  businessDate: string;
  payrollRole: string | null;
  stationCode: string | null;
  startTime: string;
  endTime: string;
  comment: string;
  allowanceOverrideId: string | null;
  allowanceSelection: "auto" | "senior" | "deputy_senior" | "none";
  allowanceRecipientEmployeeId: string | null;
  allowanceComment: string;
  allowanceDirty: boolean;
  allowanceNoneConfirmOpen: boolean;
  compact: boolean;
};

type CopyWeekState = {
  open: boolean;
  targetMode: "next" | "custom";
  customDate: string;
};

type ForecastDialogState = {
  forecast: RevenueForecastRead;
  amount: string;
  reason: string;
  removeConfirmOpen: boolean;
};

type CostDaySummary = {
  total: number;
  estimateCount: number;
  warningCount: number;
  reasons: string[];
};

type FotStatusLevel = "none" | "ok" | "warning" | "danger";

type ScheduleRouteProps = {
  activeTab: ScheduleActiveTab;
  onNavigate: (path: string) => void;
  useStoredTab?: boolean;
};

const NO_VALUE = "__none";
const DAY_CELL_WIDTH = 128;
const EMPLOYEE_COLUMN_WIDTH = 230;
const STATION_COLUMN_WIDTH = 170;
const FORECAST_BUDGET_LEFT_COLUMN_WIDTH = 140;
const FORECAST_BUDGET_DAY_COLUMN_WIDTH = 90;
const FORECAST_BUDGET_TOTAL_COLUMN_WIDTH = 110;
const FORECAST_BUDGET_COLLAPSED_KEY = "schedule.forecastBudgetCollapsed";
const FORECAST_BUDGET_LEGACY_COLLAPSED_KEYS = [
  "schedule.forecastSummaryCollapsed",
  "schedule.revenueForecastCollapsed",
  "schedule.costForecastCollapsed",
  "schedule.budgetSidebarCollapsed",
];
const SCHEDULE_ACTIVE_TAB_STORAGE_KEY = "schedule.activeTab";
const MOSCOW_OFFSET = "+03:00";
const stationOptions = ["Пицца", "Роллы", "Горячий цех", "Касса"];
const stationOrder = [...stationOptions, "(без станции)"];
const weekdayLabels = ["Вс", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"];

// Категория сотрудника В ГРАФИКЕ определяется по РОЛЯМ, а не по формальной должности:
// управляющий-подменный повар попадает в «Повара», а кассир (роль администратора) —
// в «Администраторы». Так фильтр отражает то, кем человек РАБОТАЕТ в графике.
const SCHEDULE_PRODUCTION_ROLES = new Set(["sushi", "pizza", "shawarma", "prep"]);
const SCHEDULE_CATEGORY_ORDER = ["Администраторы", "Повара"];
function scheduleCategoriesForRoster(row: EmployeeRosterRow): Set<string> {
  const categories = new Set<string>();
  for (const role of row.available_roles) {
    if (SCHEDULE_PRODUCTION_ROLES.has(role.payroll_role)) {
      categories.add("Повара");
    } else if (role.payroll_role === "administrator") {
      categories.add("Администраторы");
    }
  }
  return categories;
}

export function ScheduleRoute({ activeTab, onNavigate, useStoredTab = false }: ScheduleRouteProps) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const canViewSchedule = permissions.hasPermission("source.schedule.read");
  const canEditSchedule = permissions.hasPermission("source.schedule.edit");
  const canViewShiftLedger = permissions.hasPermission("source.shift_ledger.read");
  const canViewVacations = permissions.hasPermission("payroll.vacations.read");
  const canViewDishwashers = canViewSchedule;
  const canEditDishwashers = permissions.hasPermission("source.schedule.dishwashers.edit");
  const canViewRevenue = permissions.hasPermission("source.revenue.read");
  const canEditRevenue = permissions.hasPermission("source.revenue.edit");
  const canViewPayrollSourceData = permissions.hasPermission("source.rates.read");
  const canViewForecastBudget = canViewRevenue || canViewPayrollSourceData;
  const firstAllowedTab = canViewSchedule
    ? "schedule"
    : canViewShiftLedger
      ? "shifts-ledger"
      : canViewVacations
        ? "vacations"
        : canViewDishwashers
          ? "dishwashers"
          : null;
  const canViewActiveTab =
    (activeTab === "schedule" && canViewSchedule) ||
    (activeTab === "shifts-ledger" && canViewShiftLedger) ||
    (activeTab === "vacations" && canViewVacations) ||
    (activeTab === "dishwashers" && canViewDishwashers);
  const [isResolvingStoredTab, setIsResolvingStoredTab] = useState(useStoredTab);
  const storedPeriodPreset = useMemo(readStoredSchedulePreset, []);
  const [periodPreset] = useLocalStorageState<PeriodPreset>(
    "schedule.preset",
    () => storedPeriodPreset ?? "month",
    isPeriodPreset,
    { hydrateFromStorage: false },
  );
  const [periodRange, setPeriodRange] = useLocalStorageState<PeriodRange>(
    "schedule.range",
    () => initialScheduleRange(storedPeriodPreset),
    isPeriodRange,
    { hydrateFromStorage: false },
  );
  // Вид графика: «По сотрудникам» / «По цехам» (переключатель — над таблицей).
  const [viewMode, setViewMode] = useState<ViewMode>("employees");
  // Фильтр ростера по категории роли: "all" | "Повара" | "Администраторы".
  const [positionFilter, setPositionFilter] = useState<string>("all");
  const [isGridFullscreen, setIsGridFullscreen] = useState(false);
  const [planFactTableMode, setPlanFactTableMode] = useState<PlanFactTableMode>("days");

  useEffect(() => {
    if (!isGridFullscreen) {
      return;
    }
    function handleKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setIsGridFullscreen(false);
      }
    }
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [isGridFullscreen]);
  const [selectedScheduleId, setSelectedScheduleId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [createPeriodPreset, setCreatePeriodPreset] = useState<PeriodPreset>("month");
  const [createDraft, setCreateDraft] = useState<ScheduleCreatePayload>(() =>
    defaultScheduleDraft(),
  );
  const [shiftDialog, setShiftDialog] = useState<ShiftDialogState | null>(null);
  const [forecastDialog, setForecastDialog] = useState<ForecastDialogState | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ScheduledShiftRead | null>(null);
  const [publishOpen, setPublishOpen] = useState(false);
  const [newVersionOpen, setNewVersionOpen] = useState(false);
  const [deleteScheduleOpen, setDeleteScheduleOpen] = useState(false);

  useEffect(() => {
    if (!useStoredTab) {
      setIsResolvingStoredTab(false);
      return;
    }
    const storedTab = readStoredScheduleTab();
    if (storedTab && storedTab !== activeTab) {
      onNavigate(scheduleTabPath(storedTab));
      return;
    }
    setIsResolvingStoredTab(false);
  }, [activeTab, onNavigate, useStoredTab]);

  useEffect(() => {
    if (isResolvingStoredTab) {
      return;
    }
    window.localStorage.setItem(SCHEDULE_ACTIVE_TAB_STORAGE_KEY, activeTab);
  }, [activeTab, isResolvingStoredTab]);

  useEffect(() => {
    if (!isResolvingStoredTab && firstAllowedTab && !canViewActiveTab) {
      onNavigate(scheduleTabPath(firstAllowedTab));
    }
  }, [activeTab, canViewActiveTab, firstAllowedTab, isResolvingStoredTab, onNavigate]);
  const [forceRefreshIiko, setForceRefreshIiko] = useState(false);
  const [forceRefreshConfirmOpen, setForceRefreshConfirmOpen] = useState(false);
  const [selectedCostRunId, setSelectedCostRunId] = useState<string | null>(null);
  const [costHistoryOpen, setCostHistoryOpen] = useState(false);
  const [forecastBudgetCollapsed, setForecastBudgetCollapsed] = useLocalStorageState<boolean>(
    FORECAST_BUDGET_COLLAPSED_KEY,
    readForecastBudgetCollapsedDefault,
    isBoolean,
  );
  const warmedScheduleIds = useRef(new Set<string>());
  const [copyDialog, setCopyDialog] = useState<CopyWeekState>(() => ({
    open: false,
    targetMode: "next",
    customDate: toIsoDate(addDays(startOfTuesdayWeek(parseIsoDate(periodRange.from)), 7)),
  }));

  const visibleDays = useMemo(() => buildRangeDays(periodRange), [periodRange]);
  const forecastRange = useMemo(
    () => ({
      from: visibleDays[0],
      to: visibleDays[visibleDays.length - 1],
    }),
    [visibleDays],
  );
  const vacationYear = parseIsoDate(visibleDays[0]).getFullYear();
  const scheduleWindow = periodRange;

  const schedulesQuery = useQuery({
    queryKey: ["schedules", scheduleWindow.from, scheduleWindow.to],
    queryFn: () =>
      listSchedules({
        date_from: scheduleWindow.from,
        date_to: scheduleWindow.to,
      }),
    enabled: canViewSchedule,
  });
  const rosterQuery = useQuery({
    queryKey: ["employees-roster"],
    queryFn: getEmployeesRoster,
    enabled: canViewSchedule,
  });
  const scheduleQuery = useQuery({
    queryKey: ["schedule", selectedScheduleId],
    queryFn: () => getSchedule(selectedScheduleId ?? ""),
    enabled: Boolean(selectedScheduleId && canViewSchedule),
  });
  // Единый «живой» график: один график с состоянием draft/published, без версий и выбора.
  const livingScheduleQuery = useQuery({
    queryKey: ["schedule-living"],
    queryFn: getLivingSchedule,
    enabled: canViewSchedule,
  });
  const ledgerQuery = useQuery({
    queryKey: ["schedule-ledger", forecastRange.from, forecastRange.to],
    queryFn: () =>
      getScheduleLedger({
        date_from: forecastRange.from,
        date_to: forecastRange.to,
      }),
    enabled: canViewSchedule,
  });
  const vacationRosterQuery = useQuery({
    queryKey: ["vacations-roster", vacationYear],
    queryFn: () => getVacationRoster(vacationYear),
    enabled: canViewVacations,
  });
  const forecastQuery = useQuery({
    queryKey: ["forecast", forecastRange.from, forecastRange.to],
    queryFn: () =>
      getForecastRange({
        date_from: forecastRange.from,
        date_to: forecastRange.to,
      }),
    enabled: Boolean(selectedScheduleId && canViewRevenue),
  });
  const latestCostQuery = useQuery({
    queryKey: ["cost-forecast", selectedScheduleId, "latest"],
    queryFn: () => getLatestRun(selectedScheduleId ?? ""),
    enabled: Boolean(selectedScheduleId && canViewPayrollSourceData),
  });
  const selectedCostQuery = useQuery({
    queryKey: ["cost-forecast", selectedScheduleId, selectedCostRunId],
    queryFn: () => getRun(selectedScheduleId ?? "", selectedCostRunId ?? ""),
    enabled: Boolean(selectedScheduleId && selectedCostRunId && canViewPayrollSourceData),
  });
  const costRunsQuery = useQuery({
    queryKey: ["cost-forecast", selectedScheduleId, "runs"],
    queryFn: () => listRuns(selectedScheduleId ?? ""),
    enabled: Boolean(selectedScheduleId && costHistoryOpen && canViewPayrollSourceData),
  });
  const planFactQuery = useQuery({
    queryKey: ["plan-fact", selectedScheduleId],
    queryFn: () => getPlanFact(selectedScheduleId ?? ""),
    enabled: Boolean(selectedScheduleId && canViewSchedule),
  });

  const schedules = useMemo(
    () => [...(schedulesQuery.data ?? [])].sort(compareSchedulesForSelect),
    [schedulesQuery.data],
  );
  const roster = useMemo(
    () => [...(rosterQuery.data ?? [])].sort(compareRosterRows),
    [rosterQuery.data],
  );
  const employeeViewRoster = useMemo(() => sortEmployeesByRoleAndName(roster), [roster]);
  // Категории, реально присутствующие в ростере графика (по ролям): «Администраторы» / «Повара».
  const rosterCategories = useMemo(() => {
    const present = new Set<string>();
    for (const row of employeeViewRoster) {
      for (const category of scheduleCategoriesForRoster(row)) present.add(category);
    }
    return SCHEDULE_CATEGORY_ORDER.filter((category) => present.has(category));
  }, [employeeViewRoster]);
  const visibleRoster = useMemo(
    () =>
      positionFilter === "all"
        ? employeeViewRoster
        : employeeViewRoster.filter((row) =>
            scheduleCategoriesForRoster(row).has(positionFilter),
          ),
    [employeeViewRoster, positionFilter],
  );
  const currentSchedule = selectedScheduleId ? (scheduleQuery.data ?? null) : null;
  const currentScheduleRange: PeriodRange = currentSchedule
    ? {
        from: currentSchedule.date_start,
        to: currentSchedule.date_end,
      }
    : periodRange;
  const cashierOverridesQuery = useQuery({
    queryKey: ["cashier-allowance-overrides", selectedScheduleId],
    queryFn: () => listCashierAllowanceOverrides(selectedScheduleId ?? ""),
    enabled: Boolean(selectedScheduleId && canViewSchedule),
  });
  const cashierAllowanceResolveDays = useMemo(
    () => findCashierAllowanceResolveDays(currentSchedule?.shifts ?? [], roster),
    [currentSchedule?.shifts, roster],
  );
  const cashierAllowanceResolveQueries = useQueries({
    queries: cashierAllowanceResolveDays.map((businessDate) => ({
      queryKey: ["cashier-allowance-resolve", selectedScheduleId, businessDate],
      queryFn: () =>
        resolveCashierAllowance(selectedScheduleId ?? "", { business_date: businessDate }),
      enabled: Boolean(selectedScheduleId && canViewSchedule),
    })),
  });
  const cashierAllowanceByDay = useMemo(
    () =>
      new Map(
        cashierAllowanceResolveQueries
          .map((query) => query.data)
          .filter((item): item is AllowanceAssignmentRead => Boolean(item))
          .map((item) => [item.business_date, item]),
      ),
    [cashierAllowanceResolveQueries],
  );
  const cashierOverridesByDay = useMemo(
    () => indexCashierOverridesByDay(cashierOverridesQuery.data ?? []),
    [cashierOverridesQuery.data],
  );
  const displayedCostRun = selectedCostRunId
    ? (selectedCostQuery.data ?? null)
    : (latestCostQuery.data ?? null);
  const actualRevenueByDay = useMemo(
    () => indexActualRevenueByDay(planFactQuery.data?.by_date ?? []),
    [planFactQuery.data?.by_date],
  );
  const costEstimatesByShiftId = useMemo(
    () => indexCostEstimatesByShift(displayedCostRun?.estimates ?? []),
    [displayedCostRun?.estimates],
  );
  const isDraft = currentSchedule?.status === "draft";
  const isLocked = currentSchedule != null && (!isDraft || !canEditSchedule);
  const selectedWeekStart = toIsoDate(startOfTuesdayWeek(parseIsoDate(periodRange.from)));
  const selectedWeekEnd = toIsoDate(addDays(parseIsoDate(selectedWeekStart), 6));
  const shiftDialogAllowanceAssignment = shiftDialog
    ? cashierAllowanceByDay.get(shiftDialog.businessDate)
    : undefined;
  const shiftDialogAllowanceQuery = shiftDialog
    ? cashierAllowanceResolveQueries[cashierAllowanceResolveDays.indexOf(shiftDialog.businessDate)]
    : undefined;
  const shiftDialogAllowanceLoading =
    Boolean(shiftDialogAllowanceQuery?.isLoading) || cashierOverridesQuery.isLoading;

  useEffect(() => {
    // Единый «живой» график: всегда работаем с ним (авто-выбор версии по периоду убран).
    const livingId = livingScheduleQuery.data?.id ?? null;
    if (livingId && selectedScheduleId !== livingId) {
      setSelectedScheduleId(livingId);
    }
  }, [livingScheduleQuery.data, selectedScheduleId]);

  const createMutation = useMutation({
    mutationFn: createSchedule,
    onSuccess: async (schedule) => {
      toast.success("График создан в черновике");
      setCreateOpen(false);
      setSelectedScheduleId(schedule.id);
      await queryClient.invalidateQueries({ queryKey: ["schedules"] });
      queryClient.setQueryData(["schedule", schedule.id], schedule);
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось создать график")),
  });

  const saveShiftMutation = useMutation({
    mutationFn: (variables: { scheduleId: string; payload: ScheduledShiftUpsertPayload }) =>
      upsertShift(variables.scheduleId, variables.payload),
    // onSuccess НЕ async: submitShiftDialog ждёт mutateAsync(), чтобы решить, когда закрыть
    // диалог, а mutateAsync резолвится только после onSuccess. Если тут await-ить
    // инвалидацию (рефетч кучи активных запросов графика), диалог зависает в "сохранении"
    // на всю цепочку рефетчей, а не на время самого запроса — инвалидация обязана быть
    // fire-and-forget.
    onSuccess: () => {
      toast.success("Смена сохранена");
      void invalidateCurrentSchedule();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить смену")),
  });

  const saveCashierAllowanceOverrideMutation = useMutation({
    mutationFn: (variables: {
      scheduleId: string;
      payload: CashierAllowanceOverridePayload;
      overrideId?: string | null;
    }) =>
      upsertCashierAllowanceOverride(variables.scheduleId, variables.payload, variables.overrideId),
    onSuccess: () => {
      toast.success("Выбор надбавки сохранён");
      void invalidateCashierAllowance();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить выбор надбавки")),
  });

  const removeCashierAllowanceOverrideMutation = useMutation({
    mutationFn: (variables: { scheduleId: string; overrideId: string }) =>
      deleteCashierAllowanceOverride(variables.scheduleId, variables.overrideId),
    onSuccess: () => {
      toast.success("Ручной выбор снят");
      setShiftDialog((current) =>
        current
          ? {
              ...current,
              allowanceOverrideId: null,
              allowanceSelection: "auto",
              allowanceRecipientEmployeeId: null,
              allowanceComment: "",
              allowanceDirty: false,
            }
          : current,
      );
      void invalidateCashierAllowance();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось снять выбор надбавки")),
  });

  const quickCreateShiftMutation = useMutation({
    mutationFn: (variables: { scheduleId: string; employeeId: string; businessDate: string }) =>
      upsertShift(variables.scheduleId, {
        employee_id: variables.employeeId,
        business_date: variables.businessDate,
      }),
    onSuccess: (shift, variables) => {
      queryClient.setQueryData<ScheduleRead | undefined>(
        ["schedule", variables.scheduleId],
        (old) => {
          if (!old) return old;
          const filtered = old.shifts.filter(
            (s) =>
              !(
                s.employee_id === shift.employee_id &&
                s.business_date === shift.business_date
              ) && s.id !== shift.id,
          );
          return { ...old, shifts: [...filtered, shift] };
        },
      );
      queryClient.invalidateQueries({ queryKey: ["plan-fact", variables.scheduleId] });
      queryClient.invalidateQueries({ queryKey: ["cost-forecast", variables.scheduleId] });
      void invalidateCashierAllowance();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось добавить смену")),
  });

  const deleteShiftMutation = useMutation({
    mutationFn: (shift: ScheduledShiftRead) => deleteShift(currentSchedule?.id ?? "", shift.id),
    onSuccess: (_void, shift) => {
      setDeleteTarget(null);
      setShiftDialog(null);
      const scheduleId = currentSchedule?.id;
      if (scheduleId) {
        queryClient.setQueryData<ScheduleRead | undefined>(
          ["schedule", scheduleId],
          (old) => {
            if (!old) return old;
            return { ...old, shifts: old.shifts.filter((s) => s.id !== shift.id) };
          },
        );
        queryClient.invalidateQueries({ queryKey: ["plan-fact", scheduleId] });
        queryClient.invalidateQueries({ queryKey: ["cost-forecast", scheduleId] });
      }
      void invalidateCashierAllowance();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось удалить смену")),
  });

  const publishMutation = useMutation({
    mutationFn: () => publishSchedule(currentSchedule?.id ?? ""),
    onSuccess: async (schedule) => {
      toast.success("График зафиксирован");
      setPublishOpen(false);
      setSelectedScheduleId(schedule.id);
      await queryClient.invalidateQueries({ queryKey: ["schedule-living"] });
      await queryClient.invalidateQueries({ queryKey: ["schedules"] });
      await queryClient.invalidateQueries({ queryKey: ["schedule"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось зафиксировать график")),
  });

  const reopenMutation = useMutation({
    mutationFn: () => reopenSchedule(currentSchedule?.id ?? ""),
    onSuccess: async (schedule) => {
      toast.success("График открыт для редактирования");
      setSelectedScheduleId(schedule.id);
      await queryClient.invalidateQueries({ queryKey: ["schedule-living"] });
      await queryClient.invalidateQueries({ queryKey: ["schedules"] });
      await queryClient.invalidateQueries({ queryKey: ["schedule"] });
    },
    onError: (error) =>
      toast.error(apiErrorMessage(error, "Не удалось открыть график для редактирования")),
  });

  const newVersionMutation = useMutation({
    mutationFn: () => createNewVersion(currentSchedule?.id ?? ""),
    onSuccess: async (schedule) => {
      toast.success("Черновик новой версии создан");
      setNewVersionOpen(false);
      setSelectedScheduleId(schedule.id);
      await queryClient.invalidateQueries({ queryKey: ["schedules"] });
      queryClient.setQueryData(["schedule", schedule.id], schedule);
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось создать новую версию")),
  });

  const deleteScheduleMutation = useMutation({
    mutationFn: () => deleteSchedule(currentSchedule?.id ?? ""),
    onSuccess: async () => {
      toast.success("Черновик удалён");
      setDeleteScheduleOpen(false);
      const removedId = currentSchedule?.id;
      if (removedId) {
        queryClient.removeQueries({ queryKey: ["schedule", removedId] });
      }
      setSelectedScheduleId(null);
      await queryClient.invalidateQueries({ queryKey: ["schedules"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось удалить черновик")),
  });

  const copyWeekMutation = useMutation({
    mutationFn: (toDate: string) =>
      copyWeek(currentSchedule?.id ?? "", {
        from_date: selectedWeekStart,
        to_date: toDate,
      }),
    onSuccess: async (result) => {
      toast.success(`Скопировано ${result.copied} смен`);
      setCopyDialog((current) => ({ ...current, open: false }));
      await invalidateCurrentSchedule();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось скопировать неделю")),
  });

  const warmForecastMutation = useMutation({
    mutationFn: (payload: RevenueForecastRecomputePayload) => recomputeForecast(payload),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["forecast"] });
      await queryClient.invalidateQueries({ queryKey: ["plan-fact"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось рассчитать прогноз")),
  });

  const recomputeForecastMutation = useMutation({
    mutationFn: (payload: RevenueForecastRecomputePayload) => recomputeForecast(payload),
    onSuccess: async (result) => {
      toast.success(`Пересчитано ${result.recomputed} дней`);
      setForceRefreshConfirmOpen(false);
      await queryClient.invalidateQueries({ queryKey: ["forecast"] });
      await queryClient.invalidateQueries({ queryKey: ["plan-fact"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось пересчитать прогноз")),
  });

  const runCostForecastMutation = useMutation({
    mutationFn: () => runCostForecast(currentSchedule?.id ?? ""),
    onSuccess: async (run) => {
      toast.success(`Расчёт готов: ФОТ ${formatPercent(run.fot_to_revenue_pct)}`);
      setSelectedCostRunId(run.id);
      queryClient.setQueryData(["cost-forecast", currentSchedule?.id, run.id], run);
      await invalidateCostForecast();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось рассчитать стоимость")),
  });

  const saveForecastOverrideMutation = useMutation({
    mutationFn: (variables: {
      businessDate: string;
      payload: { amount: number; reason?: string | null };
    }) => overrideForecast(variables.businessDate, variables.payload),
    onSuccess: async (forecast) => {
      toast.success("Ручной прогноз сохранён");
      setForecastDialog((current) =>
        current
          ? {
              ...current,
              forecast,
              amount: amountInputValue(forecast.manual_override_amount),
              reason: forecast.manual_override_reason ?? "",
            }
          : current,
      );
      await queryClient.invalidateQueries({ queryKey: ["forecast"] });
      await queryClient.invalidateQueries({ queryKey: ["plan-fact"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить прогноз")),
  });

  const removeForecastOverrideMutation = useMutation({
    mutationFn: (businessDate: string) => removeForecastOverride(businessDate),
    onSuccess: async (forecast) => {
      toast.success("Ручной прогноз снят");
      setForecastDialog((current) =>
        current
          ? {
              ...current,
              forecast,
              amount: "",
              reason: "",
              removeConfirmOpen: false,
            }
          : current,
      );
      await queryClient.invalidateQueries({ queryKey: ["forecast"] });
      await queryClient.invalidateQueries({ queryKey: ["plan-fact"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось снять прогноз")),
  });

  useEffect(() => {
    if (
      !canEditRevenue ||
      !currentSchedule ||
      warmedScheduleIds.current.has(currentSchedule.id)
    ) {
      return;
    }
    warmedScheduleIds.current.add(currentSchedule.id);
    // Единый график покрывает широкое окно дат — прогноз греем на ВИДИМЫЙ период
    // (иначе упрёмся в лимит 62 дня у прогноза выручки).
    warmForecastMutation.mutate({
      date_from: forecastRange.from,
      date_to: forecastRange.to,
      force_refresh_iiko: false,
    });
  }, [canEditRevenue, currentSchedule, forecastRange.from, forecastRange.to, warmForecastMutation]);

  useEffect(() => {
    setSelectedCostRunId(null);
    setCostHistoryOpen(false);
  }, [selectedScheduleId]);

  useEffect(() => {
    if (!shiftDialog || shiftDialog.allowanceDirty) {
      return;
    }
    const existingOverride = cashierOverridesByDay.get(shiftDialog.businessDate);
    const nextSelection = existingOverride?.recipient_role ?? "auto";
    const nextRecipient = existingOverride?.recipient_employee_id ?? null;
    const nextComment = existingOverride?.comment ?? "";
    if (
      shiftDialog.allowanceOverrideId === (existingOverride?.id ?? null) &&
      shiftDialog.allowanceSelection === nextSelection &&
      shiftDialog.allowanceRecipientEmployeeId === nextRecipient &&
      shiftDialog.allowanceComment === nextComment
    ) {
      return;
    }
    setShiftDialog((current) =>
      current && !current.allowanceDirty
        ? {
            ...current,
            allowanceOverrideId: existingOverride?.id ?? null,
            allowanceSelection: nextSelection,
            allowanceRecipientEmployeeId: nextRecipient,
            allowanceComment: nextComment,
          }
        : current,
    );
  }, [cashierOverridesByDay, shiftDialog]);

  async function invalidateCurrentSchedule() {
    await queryClient.invalidateQueries({ queryKey: ["schedule", selectedScheduleId] });
    await queryClient.invalidateQueries({ queryKey: ["schedules"] });
    await queryClient.invalidateQueries({ queryKey: ["plan-fact", selectedScheduleId] });
    await invalidateCashierAllowance();
  }

  async function invalidateCostForecast() {
    await queryClient.invalidateQueries({ queryKey: ["cost-forecast", currentSchedule?.id] });
    await queryClient.invalidateQueries({ queryKey: ["plan-fact", currentSchedule?.id] });
  }

  async function invalidateCashierAllowance() {
    await queryClient.invalidateQueries({
      queryKey: ["cashier-allowance-overrides", selectedScheduleId],
    });
    await queryClient.invalidateQueries({ queryKey: ["cashier-allowance-resolve"] });
    await queryClient.invalidateQueries({ queryKey: ["plan-fact", selectedScheduleId] });
    await queryClient.invalidateQueries({ queryKey: ["cost-forecast", selectedScheduleId] });
  }

  function openCreateDialog(initialRange?: PeriodRange, initialPreset: PeriodPreset = "month") {
    if (!canEditSchedule) {
      return;
    }
    const draftRange = initialRange ?? rangeForPreset("month", new Date())!;
    setCreatePeriodPreset(initialPreset);
    setCreateDraft(defaultScheduleDraft(draftRange));
    setCreateOpen(true);
  }

  function handlePeriodRangeApply(nextRange: PeriodRange) {
    setPeriodRange(nextRange);
  }

  function handleCreatePeriodPresetChange(nextPreset: PeriodPreset) {
    setCreatePeriodPreset(nextPreset);
    if (nextPreset !== "custom") {
      const nextRange = rangeForPreset(nextPreset, new Date())!;
      setCreateDraft((current) => ({
        ...current,
        date_start: nextRange.from,
        date_end: nextRange.to,
      }));
    }
  }

  function handleCreatePeriodRangeApply(nextRange: PeriodRange) {
    setCreateDraft((current) => ({
      ...current,
      date_start: nextRange.from,
      date_end: nextRange.to,
    }));
  }

  function openShiftDialog(options: {
    employeeId?: string;
    businessDate: string;
    stationCode?: string | null;
    shift?: ScheduledShiftRead;
  }) {
    if (!currentSchedule || isLocked || !canEditSchedule) {
      return;
    }
    const shift = options.shift ?? null;
    const employee = roster.find((item) => item.id === (shift?.employee_id ?? options.employeeId));
    const fallbackStation =
      options.stationCode !== undefined
        ? options.stationCode
        : shift
          ? shift.station_code
          : defaultStationForEmployee(employee);
    const hasPrimaryRole = employee?.available_roles.some((role) => role.is_primary) ?? false;
    const fallbackRole = shift
      ? roleForShiftOrPrimary(shift, employee)
      : hasPrimaryRole
        ? (defaultRoleForEmployeeAtStation(employee, fallbackStation) ??
          employee?.primary_payroll_role ??
          null)
        : null;
    const existingOverride = cashierOverridesByDay.get(
      shift?.business_date ?? options.businessDate,
    );
    const compact = !shift && Boolean(options.employeeId) && options.stationCode === undefined;
    setShiftDialog({
      mode: shift ? "edit" : "create",
      shift,
      employeeId: shift?.employee_id ?? options.employeeId ?? "",
      businessDate: shift?.business_date ?? options.businessDate,
      payrollRole: fallbackRole,
      stationCode: shift ? shift.station_code : fallbackStation,
      startTime: shift ? timeFromDateTime(shift.planned_start_at) : "10:00",
      endTime: shift ? timeFromDateTime(shift.planned_end_at) : "22:00",
      comment: shift?.comment_private ?? "",
      allowanceOverrideId: existingOverride?.id ?? null,
      allowanceSelection: existingOverride?.recipient_role ?? "auto",
      allowanceRecipientEmployeeId: existingOverride?.recipient_employee_id ?? null,
      allowanceComment: existingOverride?.comment ?? "",
      allowanceDirty: false,
      allowanceNoneConfirmOpen: false,
      compact,
    });
  }

  function handleEmployeeEmptyCellClick(employee: EmployeeRosterRow, businessDate: string) {
    if (
      !currentSchedule ||
      isLocked ||
      !canEditSchedule ||
      quickCreateShiftMutation.isPending ||
      deleteShiftMutation.isPending
    ) {
      return;
    }
    const hasPrimaryRole = employee.available_roles.some((role) => role.is_primary);
    if (!hasPrimaryRole) {
      openShiftDialog({ employeeId: employee.id, businessDate });
      return;
    }
    quickCreateShiftMutation.mutate({
      scheduleId: currentSchedule.id,
      employeeId: employee.id,
      businessDate,
    });
  }

  function handleFilledShiftClick(shift: ScheduledShiftRead) {
    if (
      !currentSchedule ||
      isLocked ||
      !canEditSchedule ||
      quickCreateShiftMutation.isPending ||
      deleteShiftMutation.isPending
    ) {
      return;
    }
    deleteShiftMutation.mutate(shift);
  }

  async function submitShiftDialog(allowNoneConfirmed = false) {
    if (!canEditSchedule || !currentSchedule || !shiftDialog || !shiftDialog.employeeId) {
      toast.error("Выберите сотрудника");
      return;
    }
    if (
      shiftDialog.allowanceDirty &&
      shiftDialog.allowanceSelection === "none" &&
      !allowNoneConfirmed
    ) {
      setShiftDialog({ ...shiftDialog, allowanceNoneConfirmOpen: true });
      return;
    }
    const employee = roster.find((item) => item.id === shiftDialog.employeeId);
    const hasPrimaryRole = employee?.available_roles.some((role) => role.is_primary) ?? false;
    const payrollRole =
      shiftDialog.payrollRole ??
      (hasPrimaryRole
        ? (defaultRoleForEmployeeAtStation(employee, shiftDialog.stationCode) ??
          employee?.primary_payroll_role ??
          null)
        : null);
    if (!payrollRole) {
      toast.error("Выберите роль");
      return;
    }
    // Закрываем диалог только после того, как ОБА запроса (смена + надбавка) реально
    // завершились — иначе один из них может остаться висеть в фоне после закрытия и
    // заблокировать кнопку «Сохранить» в следующем открытом диалоге (см. isSaving выше).
    try {
      await saveShiftMutation.mutateAsync({
        scheduleId: currentSchedule.id,
        payload: {
          business_date: shiftDialog.businessDate,
          employee_id: shiftDialog.employeeId,
          payroll_role: payrollRole,
          station_code: shiftDialog.stationCode,
          planned_start_at: composeDateTime(shiftDialog.businessDate, shiftDialog.startTime),
          planned_end_at: composeEndDateTime(
            shiftDialog.businessDate,
            shiftDialog.startTime,
            shiftDialog.endTime,
          ),
          comment_private: shiftDialog.comment.trim() || null,
        },
      });
      await submitCashierAllowanceOverride(shiftDialog);
      setShiftDialog(null);
    } catch {
      // Ошибка уже показана тостом в onError соответствующего мутейшена — оставляем
      // диалог открытым, чтобы пользователь мог поправить и повторить сохранение.
    }
  }

  async function submitCashierAllowanceOverride(state: ShiftDialogState) {
    if (!canEditSchedule || !currentSchedule || !state.allowanceDirty || state.mode !== "edit") {
      return;
    }
    if (state.allowanceSelection === "auto") {
      if (state.allowanceOverrideId) {
        await removeCashierAllowanceOverrideMutation.mutateAsync({
          scheduleId: currentSchedule.id,
          overrideId: state.allowanceOverrideId,
        });
      }
      return;
    }
    const payload: CashierAllowanceOverridePayload = {
      business_date: state.businessDate,
      recipient_role: state.allowanceSelection,
      recipient_employee_id:
        state.allowanceSelection === "none" ? null : state.allowanceRecipientEmployeeId,
      comment: state.allowanceComment.trim() || null,
    };
    await saveCashierAllowanceOverrideMutation.mutateAsync({
      scheduleId: currentSchedule.id,
      payload,
      overrideId: state.allowanceOverrideId,
    });
  }

  function submitCopyWeek() {
    if (!canEditSchedule) {
      return;
    }
    const toDate =
      copyDialog.targetMode === "next"
        ? toIsoDate(addDays(parseIsoDate(selectedWeekStart), 7))
        : copyDialog.customDate;
    copyWeekMutation.mutate(toDate);
  }

  function requestForecastRecompute() {
    if (!canEditRevenue) {
      return;
    }
    if (forceRefreshIiko) {
      setForceRefreshConfirmOpen(true);
      return;
    }
    runForecastRecompute(false);
  }

  function runForecastRecompute(forceRefresh: boolean) {
    if (!canEditRevenue) {
      return;
    }
    recomputeForecastMutation.mutate({
      date_from: forecastRange.from,
      date_to: forecastRange.to,
      force_refresh_iiko: forceRefresh,
    });
  }

  function openForecastDialog(forecast: RevenueForecastRead) {
    if (!canEditRevenue) {
      return;
    }
    setForecastDialog({
      forecast,
      amount: amountInputValue(forecast.manual_override_amount),
      reason: forecast.manual_override_reason ?? "",
      removeConfirmOpen: false,
    });
  }

  function submitForecastOverride() {
    if (!canEditRevenue || !forecastDialog) {
      return;
    }
    const amount = parseAmountInput(forecastDialog.amount);
    if (amount === null || amount <= 0) {
      toast.error("Введите сумму прогноза больше нуля");
      return;
    }
    saveForecastOverrideMutation.mutate({
      businessDate: forecastDialog.forecast.business_date,
      payload: {
        amount,
        reason: forecastDialog.reason.trim() || null,
      },
    });
  }

  function removeForecastOverrideFromDialog() {
    if (!forecastDialog) {
      return;
    }
    removeForecastOverrideMutation.mutate(forecastDialog.forecast.business_date);
  }

  const shiftByEmployeeDay = useMemo(
    () => indexShiftsByEmployeeDay(currentSchedule?.shifts ?? []),
    [currentSchedule?.shifts],
  );
  const todayIso = useMemo(() => toIsoDate(new Date()), []);
  const ledgerEntries = useMemo(() => ledgerQuery.data ?? [], [ledgerQuery.data]);
  const ledgerByEmployeeDay = useMemo(
    () => indexLedgerByEmployeeDay(ledgerEntries),
    [ledgerEntries],
  );
  const ledgerByStationDay = useMemo(() => indexLedgerByStationDay(ledgerEntries), [ledgerEntries]);
  const ledgerByDay = useMemo(() => indexLedgerByDay(ledgerEntries), [ledgerEntries]);
  const stationRows = useMemo(
    () => buildStationRows(currentSchedule?.shifts ?? [], ledgerEntries),
    [currentSchedule?.shifts, ledgerEntries],
  );
  const vacationByEmployeeDay = useMemo(
    () => indexVacationByEmployeeDay(vacationRosterQuery.data ?? []),
    [vacationRosterQuery.data],
  );
  const hasPublishedOverlap = useMemo(
    () =>
      Boolean(
        currentSchedule &&
        schedules.some(
          (schedule) =>
            schedule.id !== currentSchedule.id &&
            schedule.status === "published" &&
            rangesOverlap(
              schedule.date_start,
              schedule.date_end,
              currentSchedule.date_start,
              currentSchedule.date_end,
            ),
        ),
      ),
    [currentSchedule, schedules],
  );

  function handleScheduleTabChange(value: string) {
    if (!isScheduleTab(value)) {
      return;
    }
    setIsGridFullscreen(false);
    window.localStorage.setItem(SCHEDULE_ACTIVE_TAB_STORAGE_KEY, value);
    onNavigate(scheduleTabPath(value));
  }

  if (isResolvingStoredTab) {
    return null;
  }

  if (!firstAllowedTab) {
    return (
      <EmptyState
        icon={<Lock className="h-5 w-5" aria-hidden="true" />}
        title="Недостаточно прав"
        description="График, учёт смен и отпуска недоступны для текущего профиля."
      />
    );
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="График сотрудников"
        action={
          // Черновик/версия/публикация относятся только к расписанию смен (вкладка
          // «График»). На остальных вкладках (Учёт смен, Отпуска, График мойщиц) их нет.
          // Единый график: «Зафиксировать» (редактируемый → действующий план) и
          // «Редактировать график» (зафиксированный → снова редактируемый). Версий/создания нет.
          activeTab === "schedule" && canEditSchedule && currentSchedule ? (
            isDraft ? (
              <InlineTooltip content="Зафиксировать график: он становится действующим планом (виден в Учёте смен, план-факте, расчётах) и блокируется от правок. Чтобы снова изменить — нажмите «Редактировать график».">
                <Button disabled={publishMutation.isPending} onClick={() => setPublishOpen(true)}>
                  {publishMutation.isPending ? (
                    <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                  ) : (
                    <Lock size={16} aria-hidden="true" />
                  )}
                  Зафиксировать
                </Button>
              </InlineTooltip>
            ) : (
              <InlineTooltip content="Открыть зафиксированный график для редактирования. После правок снова нажмите «Зафиксировать».">
                <Button
                  disabled={reopenMutation.isPending}
                  onClick={() => reopenMutation.mutate()}
                  variant="outline"
                >
                  {reopenMutation.isPending ? (
                    <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                  ) : (
                    <Pencil size={16} aria-hidden="true" />
                  )}
                  Редактировать график
                </Button>
              </InlineTooltip>
            )
          ) : null
        }
      />

      <Tabs value={activeTab} onValueChange={handleScheduleTabChange} className="space-y-5">
        <TabsList>
          {canViewSchedule ? <TabsTrigger value="schedule">График</TabsTrigger> : null}
          {canViewShiftLedger ? <TabsTrigger value="shifts-ledger">Учёт смен</TabsTrigger> : null}
          {canViewVacations ? <TabsTrigger value="vacations">Отпуска</TabsTrigger> : null}
          {canViewDishwashers ? (
            <TabsTrigger value="dishwashers">График мойщиц</TabsTrigger>
          ) : null}
        </TabsList>

        {canViewSchedule ? (
        <TabsContent className="mt-0 space-y-5" value="schedule">
          {/* Минимальная панель: статус графика + иконка-календарь для выбора периода
              (диапазон кликами по датам + быстрые фильтры «7 дней»/«Этот месяц»). */}
          <div className="flex flex-wrap items-center gap-3">
            {currentSchedule ? (
              <>
                <Badge className="gap-1" variant={isDraft ? "outline" : "default"}>
                  {isDraft ? null : <Lock size={13} aria-hidden="true" />}
                  {isDraft ? "Редактируемый" : "Зафиксирован"}
                </Badge>
                <span className="text-sm text-muted-foreground">
                  {currentSchedule.shifts.length} смен
                </span>
              </>
            ) : null}
            <DateRangePopover
              from={periodRange.from}
              to={periodRange.to}
              onChange={(from, to) => {
                if (from && to) {
                  handlePeriodRangeApply({ from, to });
                } else {
                  // «Очистить»/«×»: период у графика обязателен — сбрасываем к текущему месяцу.
                  handlePeriodRangeApply(rangeForPreset("month", new Date())!);
                }
              }}
            />
          </div>

          {currentSchedule && canViewForecastBudget ? (
            <ScheduleForecastGroup
              actualRevenueByDay={actualRevenueByDay}
              collapsed={forecastBudgetCollapsed}
              costRun={displayedCostRun}
              days={visibleDays}
              forecasts={forecastQuery.data ?? []}
              forceRefreshIiko={forceRefreshIiko}
              isCostLoading={
                latestCostQuery.isLoading ||
                (selectedCostRunId ? selectedCostQuery.isLoading : false)
              }
              isCostRecomputing={runCostForecastMutation.isPending}
              isForecastLoading={forecastQuery.isLoading || warmForecastMutation.isPending}
              isForecastRecomputing={recomputeForecastMutation.isPending}
              canEditCost={canEditSchedule && canViewPayrollSourceData}
              canEditRevenue={canEditRevenue}
              onCollapsedChange={setForecastBudgetCollapsed}
              onCostHistoryOpen={() => setCostHistoryOpen(true)}
              onCostRecompute={() => {
                if (canEditSchedule && canViewPayrollSourceData) {
                  runCostForecastMutation.mutate();
                }
              }}
              onForecastCellClick={openForecastDialog}
              onForceRefreshChange={setForceRefreshIiko}
              onForecastRecompute={requestForecastRecompute}
              todayIso={todayIso}
            />
          ) : null}

          {currentSchedule && !isGridFullscreen ? (
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap items-center gap-1.5">
                <SegmentedButton
                  active={viewMode === "employees"}
                  icon={<Users size={16} aria-hidden="true" />}
                  label="По сотрудникам"
                  onClick={() => setViewMode("employees")}
                />
                <SegmentedButton
                  active={viewMode === "stations"}
                  icon={<CalendarDays size={16} aria-hidden="true" />}
                  label="По цехам"
                  onClick={() => setViewMode("stations")}
                />
                {viewMode === "employees" && rosterCategories.length > 0 ? (
                  <>
                    <span className="mx-1 h-6 w-px bg-border" aria-hidden="true" />
                    <SegmentedButton
                      active={positionFilter === "all"}
                      label="Все"
                      onClick={() => setPositionFilter("all")}
                    />
                    {rosterCategories.map((category) => (
                      <SegmentedButton
                        active={positionFilter === category}
                        key={category}
                        label={category}
                        onClick={() => setPositionFilter(category)}
                      />
                    ))}
                  </>
                ) : null}
              </div>
              <button
                type="button"
                onClick={() => setIsGridFullscreen(true)}
                title="Развернуть график"
                aria-label="Развернуть график"
                className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md border text-muted-foreground hover:bg-accent hover:text-foreground"
              >
                <Maximize2 size={16} aria-hidden="true" />
              </button>
            </div>
          ) : null}

          <div
            className={cn(
              isGridFullscreen &&
                "fixed inset-0 z-50 flex flex-col gap-3 overflow-auto bg-background p-4",
            )}
          >
            {isGridFullscreen ? (
              <div className="flex items-center justify-between gap-2">
                <div className="text-sm font-medium text-muted-foreground">
                  График — {viewMode === "stations" ? "по цехам" : "по сотрудникам"}
                  {currentSchedule
                    ? ` · ${formatDate(currentSchedule.date_start)} — ${formatDate(
                        currentSchedule.date_end,
                      )}`
                    : ""}
                </div>
                <Button onClick={() => setIsGridFullscreen(false)} size="sm" variant="outline">
                  <Minimize2 size={16} aria-hidden="true" />
                  Свернуть
                </Button>
              </div>
            ) : null}
            {!currentSchedule && !schedulesQuery.isLoading ? (
            <NoSchedulePeriodGrid
              days={visibleDays}
              isLoading={rosterQuery.isLoading}
              onCreate={() => openCreateDialog(periodRange, periodPreset)}
              canCreate={canEditSchedule}
              range={periodRange}
              roster={viewMode === "employees" ? visibleRoster : roster}
              viewMode={viewMode}
            />
          ) : viewMode === "planFact" ? (
            <PlanFactView
              isLoading={planFactQuery.isLoading}
              onTableModeChange={setPlanFactTableMode}
              summary={planFactQuery.data ?? null}
              tableMode={planFactTableMode}
            />
          ) : viewMode === "employees" ? (
            <EmployeeScheduleGrid
              days={visibleDays}
              isLoading={scheduleQuery.isLoading || rosterQuery.isLoading}
              isLocked={isLocked}
              ledgerByDay={ledgerByDay}
              ledgerByEmployeeDay={ledgerByEmployeeDay}
              onEditShift={(shift) =>
                openShiftDialog({
                  businessDate: shift.business_date,
                  shift,
                })
              }
              onEmptyCellClick={handleEmployeeEmptyCellClick}
              onFilledCellClick={handleFilledShiftClick}
              costByShiftId={costEstimatesByShiftId}
              cashierAllowanceByDay={cashierAllowanceByDay}
              roster={visibleRoster}
              scheduleRange={currentScheduleRange}
              shiftByEmployeeDay={shiftByEmployeeDay}
              today={todayIso}
              vacationByEmployeeDay={vacationByEmployeeDay}
              fullscreen={isGridFullscreen}
            />
          ) : (
            <StationScheduleGrid
              days={visibleDays}
              isLoading={scheduleQuery.isLoading}
              isLocked={isLocked}
              ledgerByDay={ledgerByDay}
              ledgerByStationDay={ledgerByStationDay}
              onCellClick={(station, day) =>
                openShiftDialog({
                  businessDate: day,
                  stationCode: station === "(без станции)" ? null : station,
                })
              }
              onShiftClick={(shift) =>
                openShiftDialog({
                  businessDate: shift.business_date,
                  shift,
                })
              }
              onShiftDelete={handleFilledShiftClick}
              costByShiftId={costEstimatesByShiftId}
              cashierAllowanceByDay={cashierAllowanceByDay}
              roster={roster}
              rows={stationRows}
              scheduleRange={currentScheduleRange}
              today={todayIso}
              fullscreen={isGridFullscreen}
            />
          )}
          </div>

          <CreateScheduleDialog
            draft={createDraft}
            isSaving={createMutation.isPending}
            onChange={setCreateDraft}
            onOpenChange={setCreateOpen}
            onPeriodRangeApply={handleCreatePeriodRangeApply}
            onPeriodPresetChange={handleCreatePeriodPresetChange}
            onSubmit={() => createMutation.mutate(createDraft)}
            open={createOpen}
            periodPreset={createPeriodPreset}
          />

          <ShiftDialog
            allowanceAssignment={shiftDialogAllowanceAssignment}
            employees={roster}
            isAllowanceLoading={shiftDialogAllowanceLoading}
            isRemovingAllowance={removeCashierAllowanceOverrideMutation.isPending}
            isSaving={
              saveShiftMutation.isPending ||
              saveCashierAllowanceOverrideMutation.isPending ||
              removeCashierAllowanceOverrideMutation.isPending
            }
            onDelete={(shift) => setDeleteTarget(shift)}
            onRemoveAllowance={() => {
              if (currentSchedule && shiftDialog?.allowanceOverrideId) {
                removeCashierAllowanceOverrideMutation.mutate({
                  scheduleId: currentSchedule.id,
                  overrideId: shiftDialog.allowanceOverrideId,
                });
              }
            }}
            onOpenChange={(open) => {
              if (!open) {
                setShiftDialog(null);
              }
            }}
            onSubmit={submitShiftDialog}
            setValue={setShiftDialog}
            state={shiftDialog}
          />

          <AlertDialog
            open={Boolean(shiftDialog?.allowanceNoneConfirmOpen)}
            onOpenChange={(open) =>
              setShiftDialog((current) =>
                current ? { ...current, allowanceNoneConfirmOpen: open } : current,
              )
            }
          >
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Оставить день без надбавки?</AlertDialogTitle>
                <AlertDialogDescription>
                  За этот день никто из администраторов не получит надбавку старшего или зама.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Отмена</AlertDialogCancel>
                <AlertDialogAction
                  onClick={(event) => {
                    event.preventDefault();
                    setShiftDialog((current) =>
                      current ? { ...current, allowanceNoneConfirmOpen: false } : current,
                    );
                    submitShiftDialog(true);
                  }}
                >
                  Сохранить
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>

          <ForecastOverrideDialog
            isRemoving={removeForecastOverrideMutation.isPending}
            isSaving={saveForecastOverrideMutation.isPending}
            onChange={setForecastDialog}
            onOpenChange={(open) => {
              if (!open) {
                setForecastDialog(null);
              }
            }}
            onRemove={removeForecastOverrideFromDialog}
            onSubmit={submitForecastOverride}
            state={forecastDialog}
          />

          <CopyWeekDialog
            copyDialog={copyDialog}
            isSaving={copyWeekMutation.isPending}
            onChange={setCopyDialog}
            onSubmit={submitCopyWeek}
            selectedWeekEnd={selectedWeekEnd}
            selectedWeekStart={selectedWeekStart}
          />

          <CostHistorySheet
            currentRunId={displayedCostRun?.id ?? null}
            isLoading={costRunsQuery.isLoading}
            onOpenChange={setCostHistoryOpen}
            onSelectRun={(runId) => setSelectedCostRunId(runId)}
            open={costHistoryOpen}
            runs={costRunsQuery.data ?? []}
          />

          <AlertDialog open={Boolean(deleteTarget)} onOpenChange={() => setDeleteTarget(null)}>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Удалить смену?</AlertDialogTitle>
                <AlertDialogDescription>
                  {deleteTarget
                    ? `${deleteTarget.employee_full_name}, ${formatDate(deleteTarget.business_date)}`
                    : ""}
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel disabled={deleteShiftMutation.isPending}>
                  Отмена
                </AlertDialogCancel>
                <AlertDialogAction
                  disabled={deleteShiftMutation.isPending || !deleteTarget}
                  onClick={(event) => {
                    event.preventDefault();
                    if (deleteTarget) {
                      deleteShiftMutation.mutate(deleteTarget);
                    }
                  }}
                >
                  Удалить
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>

          <AlertDialog open={publishOpen} onOpenChange={setPublishOpen}>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Опубликовать график?</AlertDialogTitle>
                <AlertDialogDescription>
                  {currentSchedule
                    ? `Период ${formatDate(currentSchedule.date_start)} — ${formatDate(
                        currentSchedule.date_end,
                      )}, ${currentSchedule.shifts.length} смен.`
                    : ""}
                  {hasPublishedOverlap
                    ? " Если в этот период уже есть опубликованный график, он будет помечен как замещённый."
                    : ""}
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel disabled={publishMutation.isPending}>Отмена</AlertDialogCancel>
                <AlertDialogAction
                  disabled={publishMutation.isPending}
                  onClick={(event) => {
                    event.preventDefault();
                    publishMutation.mutate();
                  }}
                >
                  Опубликовать
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>

          <AlertDialog open={newVersionOpen} onOpenChange={setNewVersionOpen}>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Создать редактируемую копию?</AlertDialogTitle>
                <AlertDialogDescription>
                  Текущий график остаётся опубликованным. Новая версия откроется как черновик.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel disabled={newVersionMutation.isPending}>
                  Отмена
                </AlertDialogCancel>
                <AlertDialogAction
                  disabled={newVersionMutation.isPending}
                  onClick={(event) => {
                    event.preventDefault();
                    newVersionMutation.mutate();
                  }}
                >
                  Создать
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>

          <AlertDialog open={deleteScheduleOpen} onOpenChange={setDeleteScheduleOpen}>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Удалить черновик графика?</AlertDialogTitle>
                <AlertDialogDescription>
                  Черновик и все его смены будут удалены безвозвратно. Действие нельзя отменить.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel disabled={deleteScheduleMutation.isPending}>
                  Отмена
                </AlertDialogCancel>
                <AlertDialogAction
                  disabled={deleteScheduleMutation.isPending}
                  onClick={(event) => {
                    event.preventDefault();
                    deleteScheduleMutation.mutate();
                  }}
                >
                  Удалить
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>

          <AlertDialog open={forceRefreshConfirmOpen} onOpenChange={setForceRefreshConfirmOpen}>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Принудительно перечитать выручку из iiko?</AlertDialogTitle>
                <AlertDialogDescription>Может занять до минуты.</AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel disabled={recomputeForecastMutation.isPending}>
                  Отмена
                </AlertDialogCancel>
                <AlertDialogAction
                  disabled={recomputeForecastMutation.isPending}
                  onClick={(event) => {
                    event.preventDefault();
                    runForecastRecompute(true);
                  }}
                >
                  Перечитать
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </TabsContent>
        ) : null}

        {canViewShiftLedger ? (
          <TabsContent className="mt-0" value="shifts-ledger">
            <PayrollDailyLedgerRoute embedded />
          </TabsContent>
        ) : null}

        {canViewVacations ? (
          <TabsContent className="mt-0" value="vacations">
            <VacationsRoute embedded />
          </TabsContent>
        ) : null}

        {canViewDishwashers ? (
          <TabsContent className="mt-0" value="dishwashers">
            <DishwasherScheduleSection canEdit={canEditDishwashers} />
          </TabsContent>
        ) : null}
      </Tabs>
    </div>
  );
}

function scheduleTabPath(tab: ScheduleActiveTab) {
  if (tab === "shifts-ledger") {
    return "/schedule/shifts-ledger";
  }
  if (tab === "vacations") {
    return "/schedule/vacations";
  }
  if (tab === "dishwashers") {
    return "/schedule/dishwashers";
  }
  return "/schedule";
}

function readStoredScheduleTab() {
  const value = window.localStorage.getItem(SCHEDULE_ACTIVE_TAB_STORAGE_KEY);
  return isScheduleTab(value) ? value : null;
}

function isScheduleTab(value: unknown): value is ScheduleActiveTab {
  return (
    value === "schedule" ||
    value === "shifts-ledger" ||
    value === "vacations" ||
    value === "dishwashers"
  );
}

const periodPresetLabels: Record<PeriodPreset, string> = {
  week: "Неделя",
  "2weeks": "2 недели",
  month: "Месяц",
  custom: "Кастом",
};

function PeriodToolbar({
  className,
  compact = false,
  onCustomRangeApply,
  onNavigate,
  onPresetChange,
  preset,
  range,
  title,
}: {
  className?: string;
  compact?: boolean;
  onCustomRangeApply: (range: PeriodRange) => void;
  onNavigate?: (direction: -1 | 1) => void;
  onPresetChange: (preset: PeriodPreset) => void;
  preset: PeriodPreset;
  range: PeriodRange;
  title?: string;
}) {
  const [customDialogOpen, setCustomDialogOpen] = useState(false);
  const [customFrom, setCustomFrom] = useState(range.from);
  const [customTo, setCustomTo] = useState(range.to);
  const showNavigation = Boolean(onNavigate);
  const customRangeInvalid = !customFrom || !customTo || customFrom > customTo;

  useEffect(() => {
    if (!customDialogOpen) {
      setCustomFrom(range.from);
      setCustomTo(range.to);
    }
  }, [customDialogOpen, range.from, range.to]);

  function openCustomDialog() {
    setCustomFrom(range.from);
    setCustomTo(range.to);
    setCustomDialogOpen(true);
  }

  function handlePresetClick(nextPreset: PeriodPreset) {
    if (nextPreset === "custom") {
      openCustomDialog();
      return;
    }
    onPresetChange(nextPreset);
  }

  function applyCustomRange() {
    if (customRangeInvalid) {
      return;
    }
    onCustomRangeApply({ from: customFrom, to: customTo });
    onPresetChange("custom");
    setCustomDialogOpen(false);
  }

  return (
    <>
      <section className={cn("grid gap-3 rounded-lg border bg-card p-4", className)}>
        <div className="grid gap-3 lg:grid-cols-[1fr_auto] lg:items-center">
          <div className="grid gap-1">
            {title ? (
              <div className="text-sm font-medium text-muted-foreground">{title}</div>
            ) : null}
            <div className={cn("font-medium tabular-nums", compact ? "text-sm" : "text-base")}>
              {formatRange(range.from, range.to)}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {showNavigation ? (
              <Button
                aria-label="Предыдущий период"
                disabled={preset === "custom"}
                onClick={() => onNavigate?.(-1)}
                size="icon"
                type="button"
                variant="outline"
              >
                <ChevronLeft size={16} aria-hidden="true" />
              </Button>
            ) : null}
            <div className="flex flex-wrap items-center gap-1">
              {(Object.keys(periodPresetLabels) as PeriodPreset[]).map((item) => (
                <Button
                  className={cn(
                    "min-w-[86px]",
                    preset === item && "border-primary bg-primary/10 text-primary",
                  )}
                  key={item}
                  onClick={() => handlePresetClick(item)}
                  size={compact ? "sm" : "default"}
                  type="button"
                  variant="outline"
                >
                  {periodPresetLabels[item]}
                </Button>
              ))}
            </div>
            {showNavigation ? (
              <Button
                aria-label="Следующий период"
                disabled={preset === "custom"}
                onClick={() => onNavigate?.(1)}
                size="icon"
                type="button"
                variant="outline"
              >
                <ChevronRight size={16} aria-hidden="true" />
              </Button>
            ) : null}
          </div>
        </div>
      </section>

      <Dialog open={customDialogOpen} onOpenChange={setCustomDialogOpen}>
        <DialogContent className="sm:max-w-[460px] sm:rounded-xl">
          <DialogHeader>
            <DialogTitle>Произвольный период</DialogTitle>
            <DialogDescription>
              Выберите даты начала и окончания периода просмотра.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-1">
            <DatePopoverInput
              id={compact ? "create-custom-period-from" : "schedule-custom-period-from"}
              label="С"
              onChange={setCustomFrom}
              value={customFrom}
            />
            <DatePopoverInput
              id={compact ? "create-custom-period-to" : "schedule-custom-period-to"}
              label="По"
              onChange={setCustomTo}
              value={customTo}
            />
          </div>
          <DialogFooter>
            <Button onClick={() => setCustomDialogOpen(false)} type="button" variant="outline">
              Отмена
            </Button>
            <Button disabled={customRangeInvalid} onClick={applyCustomRange} type="button">
              Применить
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

function DatePopoverInput({
  id,
  label,
  onChange,
  value,
}: {
  id: string;
  label: string;
  onChange: (value: string) => void;
  value: string;
}) {
  const [open, setOpen] = useState(false);

  return (
    <div className="grid gap-2 sm:grid-cols-[44px_1fr] sm:items-center">
      <Label htmlFor={id}>{label}</Label>
      <div className="relative">
        <Button
          aria-expanded={open}
          className="w-full justify-start text-left font-normal tabular-nums"
          onClick={() => setOpen((current) => !current)}
          type="button"
          variant="outline"
        >
          <CalendarDays size={16} aria-hidden="true" />
          {value ? formatDate(value) : "Выберите дату"}
        </Button>
        {open ? (
          <div className="absolute left-0 top-full z-50 mt-2 rounded-md border bg-popover p-3 shadow-lg">
            <Input
              autoFocus
              id={id}
              onBlur={() => setOpen(false)}
              onChange={(event) => onChange(event.target.value)}
              type="date"
              value={value}
            />
          </div>
        ) : null}
      </div>
    </div>
  );
}

function NoSchedulePeriodGrid({
  canCreate,
  days,
  isLoading,
  onCreate,
  range,
  roster,
  viewMode,
}: {
  canCreate: boolean;
  days: string[];
  isLoading: boolean;
  onCreate: () => void;
  range: PeriodRange;
  roster: EmployeeRosterRow[];
  viewMode: ViewMode;
}) {
  const isStations = viewMode === "stations";
  const leadingWidth = isStations ? STATION_COLUMN_WIDTH : EMPLOYEE_COLUMN_WIDTH;
  const rows = isStations
    ? stationOptions.map((station) => ({ id: station, title: station, subtitle: "" }))
    : roster.map((employee) => ({
        id: employee.id,
        title: employee.full_name,
        subtitle: employeeRoleLine(employee, primaryRoleLabelSource(employee)),
      }));
  const minWidth = leadingWidth + days.length * DAY_CELL_WIDTH;

  return (
    <section className="overflow-hidden rounded-lg border bg-card">
      <div className="flex flex-col items-center gap-3 border-b px-4 py-5 text-center">
        <div>
          <div className="font-medium">Графика на этот период нет</div>
          <div className="mt-1 text-sm text-muted-foreground">
            {formatRange(range.from, range.to)}
          </div>
        </div>
        {canCreate ? (
          <Button onClick={onCreate} type="button">
            <Plus size={16} aria-hidden="true" />
            Создать график на {formatShortRange(range.from, range.to)}
          </Button>
        ) : null}
      </div>
      <div className="overflow-x-auto">
        <table className="border-separate border-spacing-0 text-sm" style={{ minWidth }}>
          <thead>
            <tr>
              <th
                className="sticky left-0 z-20 border-b border-r bg-muted px-3 py-3 text-left font-medium text-muted-foreground"
                style={{ width: leadingWidth, minWidth: leadingWidth }}
              >
                {isStations ? "Станция" : "Сотрудник"}
              </th>
              {days.map((day) => (
                <GridDayHeader day={day} key={day} />
              ))}
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <LoadingGridRows columns={days.length} stickyWidth={leadingWidth} />
            ) : (
              rows.map((row) => (
                <tr className="hover:bg-muted/20" key={row.id}>
                  <td
                    className="sticky left-0 z-10 border-b border-r bg-card px-3 py-3 align-top"
                    style={{ width: leadingWidth, minWidth: leadingWidth }}
                  >
                    <div className="font-medium leading-5">{row.title}</div>
                    {row.subtitle ? (
                      <div className="mt-1 text-xs text-muted-foreground">{row.subtitle}</div>
                    ) : null}
                  </td>
                  {days.map((day) => (
                    <DisabledScheduleCell key={day} label="Графика на этот период нет" />
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function PlanFactView({
  isLoading,
  onTableModeChange,
  summary,
  tableMode,
}: {
  isLoading: boolean;
  onTableModeChange: (mode: PlanFactTableMode) => void;
  summary: PlanFactSummaryRead | null;
  tableMode: PlanFactTableMode;
}) {
  const [daySort, setDaySort] = useState<{ key: DaySortKey; direction: SortDirection }>({
    key: "date",
    direction: "asc",
  });
  const [employeeSort, setEmployeeSort] = useState<{
    key: EmployeeSortKey;
    direction: SortDirection;
  }>({
    key: "name",
    direction: "asc",
  });

  const dayRows = useMemo(
    () => sortPlanFactDays(summary?.by_date ?? [], daySort),
    [daySort, summary?.by_date],
  );
  const employeeRows = useMemo(
    () => sortPlanFactEmployees(summary?.by_employee ?? [], employeeSort),
    [employeeSort, summary?.by_employee],
  );

  if (isLoading) {
    return <PlanFactLoading />;
  }

  if (!summary) {
    return (
      <EmptyState
        icon={<BarChart3 className="h-5 w-5" aria-hidden="true" />}
        title="Сверка пока недоступна"
      />
    );
  }

  return (
    <div className="space-y-4">
      <PlanFactSummaryPanel summary={summary} />

      {summary.fact_availability === "none" ? (
        <EmptyState
          action={
            <Button asChild>
              <a href="/payroll">
                <ExternalLink size={16} aria-hidden="true" />К расчётам payroll
              </a>
            </Button>
          }
          icon={<CircleSlash className="h-5 w-5" aria-hidden="true" />}
          title="Факт за этот период ещё не зафиксирован"
          description="Запустите payroll-расчёт за пересекающиеся недели."
        />
      ) : (
        <section className="rounded-lg border bg-card">
          <div className="flex flex-col gap-3 border-b px-4 py-3 md:flex-row md:items-center md:justify-between">
            <div className="flex flex-wrap gap-2">
              <SegmentedButton
                active={tableMode === "days"}
                label="По дням"
                onClick={() => onTableModeChange("days")}
              />
              <SegmentedButton
                active={tableMode === "employees"}
                label="По сотрудникам"
                onClick={() => onTableModeChange("employees")}
              />
            </div>
            <Badge className={factAvailabilityClass(summary.fact_availability)} variant="outline">
              {factAvailabilityText(summary.fact_availability)}
            </Badge>
          </div>

          {summary.fact_availability === "partial" ? (
            <div className="border-b border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-800">
              Факт доступен за {summary.covered_dates.length} из {planFactTotalDays(summary)} дней.
              Остальные дни ожидают payroll-расчёт.
            </div>
          ) : null}

          {tableMode === "days" ? (
            <PlanFactDaysTable
              rows={dayRows}
              sort={daySort}
              summary={summary}
              onSort={(key) => setDaySort((current) => nextSort(current, key))}
            />
          ) : (
            <PlanFactEmployeesTable
              rows={employeeRows}
              sort={employeeSort}
              summary={summary}
              onSort={(key) => setEmployeeSort((current) => nextSort(current, key))}
            />
          )}
        </section>
      )}
    </div>
  );
}

function PlanFactLoading() {
  return (
    <div className="space-y-4">
      <section className="rounded-lg border bg-card p-4">
        <Skeleton className="h-6 w-64" />
        <div className="mt-4 grid gap-3 md:grid-cols-5">
          {Array.from({ length: 5 }).map((_, index) => (
            <Skeleton className="h-20 w-full" key={index} />
          ))}
        </div>
      </section>
      <section className="rounded-lg border bg-card p-4">
        <Skeleton className="h-8 w-52" />
        <div className="mt-4 space-y-2">
          {Array.from({ length: 6 }).map((_, index) => (
            <Skeleton className="h-12 w-full" key={index} />
          ))}
        </div>
      </section>
    </div>
  );
}

function PlanFactSummaryPanel({ summary }: { summary: PlanFactSummaryRead }) {
  const metrics = planFactSummaryMetrics(summary);

  return (
    <section className="rounded-lg border bg-card p-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div className="font-medium">
            Итоги периода {formatRange(summary.schedule.date_start, summary.schedule.date_end)}
          </div>
          <div className="mt-3">
            <PlanFactAvailability summary={summary} />
          </div>
        </div>
        <div className="text-xs text-muted-foreground" title={planFactSourceTitle(summary)}>
          Источники: график, payroll, iiko
        </div>
      </div>

      <div className="mt-4 overflow-x-auto">
        <table className="w-full min-w-[720px] text-sm">
          <thead>
            <tr className="border-b text-left text-muted-foreground">
              <th className="px-3 py-2 font-medium">Метрика</th>
              <th className="px-3 py-2 text-right font-medium">План</th>
              <th className="px-3 py-2 text-right font-medium">Факт</th>
              <th className="px-3 py-2 text-right font-medium">Отклонение</th>
              <th className="px-3 py-2 text-left font-medium">Статус</th>
            </tr>
          </thead>
          <tbody>
            {metrics.map((metric) => (
              <tr className="border-b last:border-b-0" key={metric.label}>
                <td className="px-3 py-2 font-medium">{metric.label}</td>
                <td className="px-3 py-2 text-right tabular-nums">{metric.planned}</td>
                <td className="px-3 py-2 text-right tabular-nums">{metric.actual}</td>
                <td className={cn("px-3 py-2 text-right tabular-nums", metric.className)}>
                  {metric.deviation}
                </td>
                <td className="px-3 py-2">
                  <PlanFactMetricStatus status={metric.status} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function PlanFactAvailability({ summary }: { summary: PlanFactSummaryRead }) {
  const totalDays = planFactTotalDays(summary);
  const covered = summary.covered_dates.length;
  const filled = totalDays === 0 ? 0 : Math.round((covered / totalDays) * 12);

  return (
    <div className="flex flex-wrap items-center gap-3 text-sm">
      <span className="text-muted-foreground">Доступность факта:</span>
      <div className="flex gap-1" aria-hidden="true">
        {Array.from({ length: 12 }).map((_, index) => (
          <span
            className={cn(
              "h-3 w-3 rounded-sm border",
              index < filled ? "border-emerald-600 bg-emerald-600" : "border-muted-foreground/30",
            )}
            key={index}
          />
        ))}
      </div>
      <Badge className={factAvailabilityClass(summary.fact_availability)} variant="outline">
        {factAvailabilityText(summary.fact_availability)} ({covered} из {totalDays} дней)
      </Badge>
    </div>
  );
}

function PlanFactMetricStatus({ status }: { status: "none" | "within" | "over" }) {
  if (status === "within") {
    return (
      <span className="inline-flex items-center gap-1 text-emerald-700">
        <CheckCircle2 size={15} aria-hidden="true" />в норме
      </span>
    );
  }
  if (status === "over") {
    return (
      <span className="inline-flex items-center gap-1 text-amber-700">
        <AlertTriangle size={15} aria-hidden="true" />
        выше порога
      </span>
    );
  }
  return <span className="text-muted-foreground">—</span>;
}

function PlanFactDaysTable({
  onSort,
  rows,
  sort,
  summary,
}: {
  onSort: (key: DaySortKey) => void;
  rows: PlanFactDayRowRead[];
  sort: { key: DaySortKey; direction: SortDirection };
  summary: PlanFactSummaryRead;
}) {
  const threshold = decimalToNumber(summary.warning_threshold_pct) ?? 3;

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[980px] text-sm">
        <thead>
          <tr className="border-b bg-muted/50 text-left text-muted-foreground">
            <SortableTh
              active={sort.key === "date"}
              direction={sort.direction}
              onClick={() => onSort("date")}
            >
              Дата
            </SortableTh>
            <th className="px-3 py-3 font-medium">План: смен/час/руб</th>
            <th className="px-3 py-3 font-medium">Факт: смен/час/руб</th>
            <SortableTh
              active={sort.key === "hours"}
              direction={sort.direction}
              onClick={() => onSort("hours")}
            >
              Δ часы
            </SortableTh>
            <SortableTh
              active={sort.key === "cost"}
              direction={sort.direction}
              onClick={() => onSort("cost")}
            >
              Δ стоим.
            </SortableTh>
            <th className="px-3 py-3 font-medium">Надбавка админа</th>
            <SortableTh
              active={sort.key === "status"}
              direction={sort.direction}
              onClick={() => onSort("status")}
            >
              Статус
            </SortableTh>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              className="border-b last:border-b-0 hover:bg-muted/30"
              key={row.business_date}
              title={planFactDayTitle(row, summary)}
            >
              <td className="px-3 py-3 font-medium">{formatDayWithWeekday(row.business_date)}</td>
              <td className="px-3 py-3 tabular-nums">
                {formatPlanFactTriplet(row.planned_shifts, row.planned_hours, row.planned_cost)}
              </td>
              <td className="px-3 py-3 tabular-nums">
                {formatPlanFactTriplet(row.actual_shifts, row.actual_hours, row.actual_cost, true)}
              </td>
              <td
                className={cn(
                  "px-3 py-3 tabular-nums",
                  deviationTextClass(row.hours_deviation_pct, threshold),
                )}
              >
                {formatPercent(row.hours_deviation_pct)}
              </td>
              <td
                className={cn(
                  "px-3 py-3 tabular-nums",
                  deviationTextClass(row.cost_deviation_pct, threshold),
                )}
              >
                {formatPercent(row.cost_deviation_pct)}
              </td>
              <td className="px-3 py-3">
                <CashierAllowancePlanFactCell row={row} />
              </td>
              <td className="px-3 py-3">
                <PlanFactStatusBadge status={row.deviation_status} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PlanFactEmployeesTable({
  onSort,
  rows,
  sort,
  summary,
}: {
  onSort: (key: EmployeeSortKey) => void;
  rows: PlanFactEmployeeRowRead[];
  sort: { key: EmployeeSortKey; direction: SortDirection };
  summary: PlanFactSummaryRead;
}) {
  const threshold = decimalToNumber(summary.warning_threshold_pct) ?? 3;

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[900px] text-sm">
        <thead>
          <tr className="border-b bg-muted/50 text-left text-muted-foreground">
            <SortableTh
              active={sort.key === "name"}
              direction={sort.direction}
              onClick={() => onSort("name")}
            >
              ФИО
            </SortableTh>
            <th className="px-3 py-3 font-medium">Должн.</th>
            <th className="px-3 py-3 font-medium">План: смен/час/руб</th>
            <th className="px-3 py-3 font-medium">Факт: смен/час/руб</th>
            <SortableTh
              active={sort.key === "hours"}
              direction={sort.direction}
              onClick={() => onSort("hours")}
            >
              Δ часы
            </SortableTh>
            <SortableTh
              active={sort.key === "cost"}
              direction={sort.direction}
              onClick={() => onSort("cost")}
            >
              Δ стоим.
            </SortableTh>
            <SortableTh
              active={sort.key === "status"}
              direction={sort.direction}
              onClick={() => onSort("status")}
            >
              Статус
            </SortableTh>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              className="border-b last:border-b-0 hover:bg-muted/30"
              key={row.employee_id}
              title={planFactEmployeeTitle(row, summary)}
            >
              <td className="px-3 py-3 font-medium">{row.full_name}</td>
              <td className="px-3 py-3 text-muted-foreground">{row.position || "—"}</td>
              <td className="px-3 py-3 tabular-nums">
                {formatPlanFactTriplet(row.planned_shifts, row.planned_hours, row.planned_cost)}
              </td>
              <td className="px-3 py-3 tabular-nums">
                {formatPlanFactTriplet(row.actual_shifts, row.actual_hours, row.actual_cost, true)}
              </td>
              <td
                className={cn(
                  "px-3 py-3 tabular-nums",
                  deviationTextClass(row.hours_deviation_pct, threshold),
                )}
              >
                {formatPercent(row.hours_deviation_pct)}
              </td>
              <td
                className={cn(
                  "px-3 py-3 tabular-nums",
                  deviationTextClass(row.cost_deviation_pct, threshold),
                )}
              >
                {formatPercent(row.cost_deviation_pct)}
              </td>
              <td className="px-3 py-3">
                <PlanFactStatusBadge status={row.deviation_status} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SortableTh({
  active,
  children,
  direction,
  onClick,
}: {
  active: boolean;
  children: ReactNode;
  direction: SortDirection;
  onClick: () => void;
}) {
  return (
    <th className="px-3 py-3 font-medium">
      <button
        className={cn(
          "inline-flex items-center gap-1 rounded-sm text-left hover:text-foreground focus:outline-none focus:ring-2 focus:ring-ring",
          active && "text-foreground",
        )}
        onClick={onClick}
        type="button"
      >
        {children}
        <ArrowUpDown
          className={cn("h-3.5 w-3.5", active ? "opacity-100" : "opacity-50")}
          aria-hidden="true"
        />
        {active ? (
          <span className="sr-only">{direction === "asc" ? "по возрастанию" : "по убыванию"}</span>
        ) : null}
      </button>
    </th>
  );
}

function PlanFactStatusBadge({ status }: { status: PlanFactDeviationStatus }) {
  const labels: Record<PlanFactDeviationStatus, string> = {
    no_data: "нет данных",
    within_threshold: "в норме",
    over_threshold: "выше порога",
    plan_no_fact: "план без факта",
    fact_no_plan: "факт без плана",
  };
  return (
    <Badge className={planFactStatusClass(status)} variant="outline">
      {labels[status] ?? status}
    </Badge>
  );
}

function CashierAllowancePlanFactCell({ row }: { row: PlanFactDayRowRead }) {
  const changed = row.deviation_flags.includes("cashier_allowance_recipient_changed");
  const title = cashierAllowancePlanFactTitle(row);
  if (!row.planned_cashier_allowance && !row.actual_cashier_allowance) {
    return <span className="text-muted-foreground">—</span>;
  }
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-sm border px-2 py-1 text-xs",
        changed
          ? "border-amber-200 bg-amber-50 text-amber-700"
          : "border-emerald-200 bg-emerald-50 text-emerald-700",
      )}
      title={title}
    >
      {changed ? <AlertTriangle size={14} aria-hidden="true" /> : null}
      {changed ? "изменилось" : "без изменений"}
    </span>
  );
}

function ScheduleForecastGroup({
  actualRevenueByDay,
  collapsed,
  costRun,
  days,
  forecasts,
  forceRefreshIiko,
  isCostLoading,
  isCostRecomputing,
  isForecastLoading,
  isForecastRecomputing,
  canEditCost,
  canEditRevenue,
  onCollapsedChange,
  onCostHistoryOpen,
  onCostRecompute,
  onForecastCellClick,
  onForceRefreshChange,
  onForecastRecompute,
  todayIso,
}: {
  actualRevenueByDay: Map<string, number>;
  collapsed: boolean;
  costRun: PayrollForecastRunRead | null;
  days: string[];
  forecasts: RevenueForecastRead[];
  forceRefreshIiko: boolean;
  isCostLoading: boolean;
  isCostRecomputing: boolean;
  isForecastLoading: boolean;
  isForecastRecomputing: boolean;
  canEditCost: boolean;
  canEditRevenue: boolean;
  onCollapsedChange: (collapsed: boolean) => void;
  onCostHistoryOpen: () => void;
  onCostRecompute: () => void;
  onForecastCellClick: (forecast: RevenueForecastRead) => void;
  onForceRefreshChange: (checked: boolean) => void;
  onForecastRecompute: () => void;
  todayIso: string;
}) {
  const forecastByDay = useMemo(
    () => new Map(forecasts.map((forecast) => [forecast.business_date, forecast])),
    [forecasts],
  );
  const costByDay = useMemo(
    () => buildCostSummariesByDay(costRun?.estimates ?? []),
    [costRun?.estimates],
  );
  const warningThresholdPct = decimalToNumber(costRun?.fot_warning_threshold_pct ?? null) ?? 28;
  const totals = useMemo(
    () =>
      calculateForecastBudgetTotals({
        actualRevenueByDay,
        costByDay,
        days,
        forecastByDay,
        hasCostRun: Boolean(costRun),
        todayIso,
      }),
    [actualRevenueByDay, costByDay, costRun, days, forecastByDay, todayIso],
  );

  if (days.length === 0) {
    return null;
  }

  return (
    <section className="overflow-hidden rounded-lg border bg-card">
      <ForecastBudgetHeader
        actions={
          <ForecastBudgetActions
            canEditCost={canEditCost}
            canEditRevenue={canEditRevenue}
            forceRefreshIiko={forceRefreshIiko}
            hasCostRun={Boolean(costRun)}
            hasRevenueForecasts={forecasts.length > 0}
            isCostRecomputing={isCostRecomputing}
            isForecastRecomputing={isForecastRecomputing}
            onCostHistoryOpen={onCostHistoryOpen}
            onCostRecompute={onCostRecompute}
            onForceRefreshChange={onForceRefreshChange}
            onForecastRecompute={onForecastRecompute}
          />
        }
        collapsed={collapsed}
        onToggle={() => onCollapsedChange(!collapsed)}
        summary={
          <ForecastBudgetSummary
            isCostLoading={isCostLoading}
            isForecastLoading={isForecastLoading}
            threshold={warningThresholdPct}
            totals={totals}
          />
        }
        title="Прогнозы и бюджет"
      />
      {collapsed ? null : (
        <div className="p-3">
          <ForecastBudgetTable
            actualRevenueByDay={actualRevenueByDay}
            costByDay={costByDay}
            costRun={costRun}
            days={days}
            forecastByDay={forecastByDay}
            isCostLoading={isCostLoading}
            isForecastLoading={isForecastLoading}
            canEditRevenue={canEditRevenue}
            onForecastCellClick={onForecastCellClick}
            threshold={warningThresholdPct}
            todayIso={todayIso}
            totals={totals}
          />
        </div>
      )}
    </section>
  );
}

type ForecastBudgetTotals = {
  revenueSum: number | null;
  costSum: number | null;
  fotPct: number | null;
};

function ForecastBudgetHeader({
  actions,
  collapsed,
  onToggle,
  summary,
  title,
}: {
  actions: ReactNode;
  collapsed: boolean;
  onToggle: () => void;
  summary: ReactNode;
  title: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col gap-3 px-4 py-3 md:flex-row md:items-center md:justify-between",
        !collapsed && "border-b",
      )}
    >
      <button
        aria-expanded={!collapsed}
        className="flex min-w-0 items-center gap-2 rounded-sm text-left hover:text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
        onClick={onToggle}
        type="button"
      >
        {collapsed ? (
          <ChevronDown size={16} aria-hidden="true" />
        ) : (
          <ChevronUp size={16} aria-hidden="true" />
        )}
        <span className="font-medium">{title}</span>
      </button>
      <div className="flex min-w-0 flex-wrap items-center gap-2 md:justify-end">
        {summary}
        {actions}
      </div>
    </div>
  );
}

function ForecastBudgetActions({
  canEditCost,
  canEditRevenue,
  forceRefreshIiko,
  hasCostRun,
  hasRevenueForecasts,
  isCostRecomputing,
  isForecastRecomputing,
  onCostHistoryOpen,
  onCostRecompute,
  onForceRefreshChange,
  onForecastRecompute,
}: {
  canEditCost: boolean;
  canEditRevenue: boolean;
  forceRefreshIiko: boolean;
  hasCostRun: boolean;
  hasRevenueForecasts: boolean;
  isCostRecomputing: boolean;
  isForecastRecomputing: boolean;
  onCostHistoryOpen: () => void;
  onCostRecompute: () => void;
  onForceRefreshChange: (checked: boolean) => void;
  onForecastRecompute: () => void;
}) {
  const hasActions = canEditRevenue || canEditCost || hasCostRun;
  if (!hasActions) {
    return null;
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button size="sm" type="button" variant="outline">
          <MoreHorizontal size={16} aria-hidden="true" />
          Действия
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        {canEditRevenue ? (
          <>
            <DropdownMenuItem disabled={isForecastRecomputing} onSelect={onForecastRecompute}>
              {isForecastRecomputing ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <RefreshCw size={16} aria-hidden="true" />
              )}
              {hasRevenueForecasts ? "Пересчитать прогноз" : "Рассчитать прогноз"}
            </DropdownMenuItem>
            <DropdownMenuCheckboxItem
              checked={forceRefreshIiko}
              onCheckedChange={(checked) => onForceRefreshChange(checked === true)}
            >
              Force-refresh iiko
            </DropdownMenuCheckboxItem>
          </>
        ) : null}
        {canEditRevenue && canEditCost ? <DropdownMenuSeparator /> : null}
        {canEditCost ? (
          <DropdownMenuItem disabled={isCostRecomputing} onSelect={onCostRecompute}>
            {isCostRecomputing ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : (
              <Calculator size={16} aria-hidden="true" />
            )}
            {hasCostRun ? "Пересчитать стоимость" : "Рассчитать стоимость"}
          </DropdownMenuItem>
        ) : null}
        <DropdownMenuItem disabled={!hasCostRun} onSelect={onCostHistoryOpen}>
          <History size={16} aria-hidden="true" />
          История версий
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function ForecastBudgetSummary({
  isCostLoading,
  isForecastLoading,
  threshold,
  totals,
}: {
  isCostLoading: boolean;
  isForecastLoading: boolean;
  threshold: number;
  totals: ForecastBudgetTotals;
}) {
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-1.5 text-sm text-muted-foreground">
      <span className="whitespace-nowrap">
        Выручка{" "}
        <span className="font-medium tabular-nums text-foreground">
          {isForecastLoading ? "загрузка" : formatMoneyWithCurrency(totals.revenueSum)}
        </span>
      </span>
      <span>·</span>
      <span className="whitespace-nowrap">
        Стоимость{" "}
        <span className="font-medium tabular-nums text-foreground">
          {isCostLoading ? "загрузка" : formatMoneyWithCurrency(totals.costSum)}
        </span>
      </span>
      <span>·</span>
      <span className="whitespace-nowrap">ФОТ</span>
      {isForecastLoading || isCostLoading ? (
        <span className="font-medium tabular-nums text-foreground">загрузка</span>
      ) : (
        <FotBadge compact threshold={threshold} value={totals.fotPct} />
      )}
    </div>
  );
}

function ForecastBudgetTable({
  actualRevenueByDay,
  canEditRevenue,
  costByDay,
  costRun,
  days,
  forecastByDay,
  isCostLoading,
  isForecastLoading,
  onForecastCellClick,
  threshold,
  todayIso,
  totals,
}: {
  actualRevenueByDay: Map<string, number>;
  canEditRevenue: boolean;
  costByDay: Map<string, CostDaySummary>;
  costRun: PayrollForecastRunRead | null;
  days: string[];
  forecastByDay: Map<string, RevenueForecastRead>;
  isCostLoading: boolean;
  isForecastLoading: boolean;
  onForecastCellClick: (forecast: RevenueForecastRead) => void;
  threshold: number;
  todayIso: string;
  totals: ForecastBudgetTotals;
}) {
  const minWidth =
    FORECAST_BUDGET_LEFT_COLUMN_WIDTH +
    days.length * FORECAST_BUDGET_DAY_COLUMN_WIDTH +
    FORECAST_BUDGET_TOTAL_COLUMN_WIDTH;

  return (
    <div className="overflow-x-auto">
      <table className="w-full border-separate border-spacing-0 text-sm" style={{ minWidth }}>
        <thead>
          <tr>
            <th
              className="sticky left-0 z-30 border-b border-r bg-muted px-3 py-2 text-left font-medium text-muted-foreground"
              style={{
                minWidth: FORECAST_BUDGET_LEFT_COLUMN_WIDTH,
                width: FORECAST_BUDGET_LEFT_COLUMN_WIDTH,
              }}
            >
              Показатель
            </th>
            {days.map((day) => (
              <ForecastBudgetDayHeader day={day} key={day} />
            ))}
            <th
              className="sticky right-0 z-30 border-b border-l bg-muted px-3 py-2 text-center font-medium text-muted-foreground shadow-[-8px_0_12px_-12px_rgba(15,23,42,0.35)]"
              style={{
                minWidth: FORECAST_BUDGET_TOTAL_COLUMN_WIDTH,
                width: FORECAST_BUDGET_TOTAL_COLUMN_WIDTH,
              }}
            >
              ИТОГО
            </th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <ForecastBudgetMetricHeader label="Выручка" />
            {days.map((day) => (
              <RevenueBudgetCell
                actualRevenueByDay={actualRevenueByDay}
                day={day}
                forecast={forecastByDay.get(day)}
                isLoading={isForecastLoading}
                key={day}
                canEdit={canEditRevenue}
                onClick={onForecastCellClick}
                todayIso={todayIso}
              />
            ))}
            <ForecastBudgetTotalCell isLoading={isForecastLoading}>
              {formatMoney(totals.revenueSum)}
            </ForecastBudgetTotalCell>
          </tr>
          <tr>
            <ForecastBudgetMetricHeader label="Стоимость смен" />
            {days.map((day) => (
              <CostBudgetCell
                day={day}
                isLoading={isCostLoading}
                key={day}
                run={costRun}
                summary={costByDay.get(day)}
              />
            ))}
            <ForecastBudgetTotalCell isLoading={isCostLoading}>
              {formatMoney(totals.costSum)}
            </ForecastBudgetTotalCell>
          </tr>
          <tr>
            <ForecastBudgetMetricHeader label="ФОТ %" />
            {days.map((day) => {
              const revenue = getForecastBudgetRevenueAmount({
                actualRevenueByDay,
                day,
                forecastByDay,
                todayIso,
              });
              const cost = costRun ? (costByDay.get(day)?.total ?? 0) : null;
              const value =
                revenue !== null && revenue > 0 && cost !== null ? (cost / revenue) * 100 : null;
              return (
                <FotBudgetCell
                  cost={cost}
                  isLoading={isForecastLoading || isCostLoading}
                  key={day}
                  revenue={revenue}
                  threshold={threshold}
                  value={value}
                />
              );
            })}
            <ForecastBudgetTotalCell isLoading={isForecastLoading || isCostLoading}>
              <FotBadge threshold={threshold} value={totals.fotPct} />
            </ForecastBudgetTotalCell>
          </tr>
        </tbody>
      </table>
    </div>
  );
}

function ForecastBudgetDayHeader({ day }: { day: string }) {
  return (
    <th
      className="border-b border-r bg-muted/70 px-2 py-2 text-center font-medium text-muted-foreground"
      style={{
        minWidth: FORECAST_BUDGET_DAY_COLUMN_WIDTH,
        width: FORECAST_BUDGET_DAY_COLUMN_WIDTH,
      }}
    >
      <div className="text-xs">{weekdayLabels[parseIsoDate(day).getDay()]}</div>
      <div className="text-base leading-5 text-foreground">{day.slice(8, 10)}</div>
    </th>
  );
}

function ForecastBudgetMetricHeader({ label }: { label: string }) {
  return (
    <th
      className="sticky left-0 z-20 border-b border-r bg-card px-3 py-3 text-left font-medium"
      style={{
        minWidth: FORECAST_BUDGET_LEFT_COLUMN_WIDTH,
        width: FORECAST_BUDGET_LEFT_COLUMN_WIDTH,
      }}
    >
      {label}
    </th>
  );
}

function RevenueBudgetCell({
  actualRevenueByDay,
  canEdit,
  day,
  forecast,
  isLoading,
  onClick,
  todayIso,
}: {
  actualRevenueByDay: Map<string, number>;
  canEdit: boolean;
  day: string;
  forecast: RevenueForecastRead | undefined;
  isLoading: boolean;
  onClick: (forecast: RevenueForecastRead) => void;
  todayIso: string;
}) {
  const display = getForecastBudgetRevenueDisplay({
    actualRevenueByDay,
    day,
    forecast,
    todayIso,
  });
  const title = revenueBudgetCellTitle(day, forecast, display);
  const content = (
    <div className="flex h-full w-full flex-col items-center justify-center gap-1 rounded-sm px-1.5">
      <div
        className={cn("font-semibold tabular-nums", revenueBudgetAmountClass(display, forecast))}
      >
        {display.amount === null ? "—" : formatMoney(display.amount)}
      </div>
      <div className="min-h-4 max-w-full truncate text-[11px] leading-4 text-muted-foreground">
        {display.label}
      </div>
    </div>
  );

  return (
    <td
      className="h-[64px] border-b border-r p-1.5 text-center align-middle"
      style={{
        minWidth: FORECAST_BUDGET_DAY_COLUMN_WIDTH,
        width: FORECAST_BUDGET_DAY_COLUMN_WIDTH,
      }}
      title={title}
    >
      {isLoading ? (
        <Skeleton className="mx-auto h-10 w-full" />
      ) : forecast && canEdit ? (
        <button
          className="h-full w-full rounded-sm hover:bg-primary/5 focus:outline-none focus:ring-2 focus:ring-ring"
          onClick={() => onClick(forecast)}
          type="button"
        >
          {content}
        </button>
      ) : (
        content
      )}
    </td>
  );
}

function CostBudgetCell({
  day,
  isLoading,
  run,
  summary,
}: {
  day: string;
  isLoading: boolean;
  run: PayrollForecastRunRead | null;
  summary: CostDaySummary | undefined;
}) {
  const hasWarnings = (summary?.warningCount ?? 0) > 0;

  return (
    <td
      className="h-[64px] border-b border-r p-1.5 text-center align-middle"
      style={{
        minWidth: FORECAST_BUDGET_DAY_COLUMN_WIDTH,
        width: FORECAST_BUDGET_DAY_COLUMN_WIDTH,
      }}
      title={costBudgetCellTitle(day, summary, run)}
    >
      {isLoading ? (
        <Skeleton className="mx-auto h-10 w-full" />
      ) : summary ? (
        <div className="flex h-full w-full flex-col items-center justify-center gap-1 rounded-sm px-1.5">
          <div
            className={cn(
              "font-semibold tabular-nums",
              hasWarnings ? "text-orange-600" : "text-foreground",
            )}
          >
            {formatMoney(summary.total)}
          </div>
          <div
            className={cn(
              "min-h-4 max-w-full truncate text-[11px] leading-4 text-muted-foreground",
              hasWarnings && "text-orange-600",
            )}
          >
            {hasWarnings ? "проверить" : `смен: ${summary.estimateCount}`}
          </div>
        </div>
      ) : (
        <div className="flex h-full w-full flex-col items-center justify-center gap-1 rounded-sm px-1.5">
          <div className="font-semibold tabular-nums text-muted-foreground">—</div>
          <div className="min-h-4 text-[11px] leading-4 text-muted-foreground">
            {run ? "нет смен" : "нет расчёта"}
          </div>
        </div>
      )}
    </td>
  );
}

function FotBudgetCell({
  cost,
  isLoading,
  revenue,
  threshold,
  value,
}: {
  cost: number | null;
  isLoading: boolean;
  revenue: number | null;
  threshold: number;
  value: number | null;
}) {
  return (
    <td
      className={cn(
        "h-[64px] border-b border-r p-1.5 text-center align-middle",
        fotBudgetCellClass(value, threshold),
      )}
      style={{
        minWidth: FORECAST_BUDGET_DAY_COLUMN_WIDTH,
        width: FORECAST_BUDGET_DAY_COLUMN_WIDTH,
      }}
      title={fotBudgetTitle(revenue, cost, value, threshold)}
    >
      {isLoading ? (
        <Skeleton className="mx-auto h-8 w-full" />
      ) : (
        <FotBadge threshold={threshold} value={value} />
      )}
    </td>
  );
}

function ForecastBudgetTotalCell({
  children,
  isLoading,
}: {
  children: ReactNode;
  isLoading: boolean;
}) {
  return (
    <td
      className="sticky right-0 z-20 h-[64px] border-b border-l bg-card px-2 py-3 text-center align-middle font-semibold tabular-nums shadow-[-8px_0_12px_-12px_rgba(15,23,42,0.35)]"
      style={{
        minWidth: FORECAST_BUDGET_TOTAL_COLUMN_WIDTH,
        width: FORECAST_BUDGET_TOTAL_COLUMN_WIDTH,
      }}
    >
      {isLoading ? <Skeleton className="mx-auto h-8 w-full" /> : children}
    </td>
  );
}

function FotBadge({
  compact = false,
  threshold,
  value,
}: {
  compact?: boolean;
  threshold: number;
  value: number | null;
}) {
  const level = fotLevelForValue(value, threshold);
  if (level === "none") {
    return <span className="tabular-nums text-muted-foreground">—</span>;
  }
  const marker = level === "ok" ? "✓" : level === "warning" ? "⚠" : "⚠⚠";
  return (
    <Badge
      className={cn(
        "rounded-sm px-2 py-0.5 font-mono tabular-nums",
        compact ? "text-[11px]" : "text-xs",
        level === "ok" && "border-emerald-200 bg-emerald-50 text-emerald-700",
        level === "warning" && "border-amber-200 bg-amber-50 text-amber-700",
        level === "danger" && "border-red-200 bg-red-50 text-red-700",
      )}
      variant="outline"
    >
      {formatPercent(value)} {marker}
    </Badge>
  );
}

function InlineTooltip({ children, content }: { children: ReactNode; content: string }) {
  return (
    <span className="group relative inline-flex">
      {children}
      <span
        className="pointer-events-none absolute right-0 top-full z-50 mt-2 hidden w-80 rounded-md border bg-popover px-3 py-2 text-left text-xs leading-5 text-popover-foreground shadow-lg group-focus-within:block group-hover:block"
        role="tooltip"
      >
        {content}
      </span>
    </span>
  );
}

function CostHistorySheet({
  currentRunId,
  isLoading,
  onOpenChange,
  onSelectRun,
  open,
  runs,
}: {
  currentRunId: string | null;
  isLoading: boolean;
  onOpenChange: (open: boolean) => void;
  onSelectRun: (runId: string) => void;
  open: boolean;
  runs: PayrollForecastRunRead[];
}) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="w-full overflow-y-auto sm:max-w-xl">
        <SheetHeader>
          <SheetTitle>История версий</SheetTitle>
          <SheetDescription>Выберите расчёт, чтобы отобразить его в графике.</SheetDescription>
        </SheetHeader>
        <div className="mt-5 grid gap-2">
          {isLoading ? (
            Array.from({ length: 5 }).map((_, index) => (
              <Skeleton className="h-20 w-full" key={index} />
            ))
          ) : runs.length === 0 ? (
            <div className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
              Истории расчётов пока нет.
            </div>
          ) : (
            runs.map((run) => (
              <button
                className={cn(
                  "rounded-md border px-3 py-3 text-left text-sm hover:border-primary/50",
                  run.id === currentRunId && "border-primary bg-primary/5",
                )}
                key={run.id}
                onClick={() => onSelectRun(run.id)}
                type="button"
              >
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="font-medium">{formatDateTime(run.run_at)}</div>
                    <div className="mt-1 text-xs text-muted-foreground">
                      {run.run_by_label ?? "Автор не указан"} · {runStatusLabel(run.status)}
                    </div>
                  </div>
                  <div className="text-right tabular-nums">
                    <div className={fotStatusClass(fotStatusLevel(run))}>
                      {formatPercent(run.fot_to_revenue_pct)}
                    </div>
                    <div className="mt-1 text-xs text-muted-foreground">
                      {formatMoneyWithCurrency(run.total_shift_cost_estimate)}
                    </div>
                  </div>
                </div>
                <div className="mt-2 text-xs text-muted-foreground">
                  Смен: {run.shifts_total}, предупреждений: {run.shifts_with_warnings}
                </div>
              </button>
            ))
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}

function ForecastOverrideDialog({
  isRemoving,
  isSaving,
  onChange,
  onOpenChange,
  onRemove,
  onSubmit,
  state,
}: {
  isRemoving: boolean;
  isSaving: boolean;
  onChange: (state: ForecastDialogState | null) => void;
  onOpenChange: (open: boolean) => void;
  onRemove: () => void;
  onSubmit: () => void;
  state: ForecastDialogState | null;
}) {
  const forecast = state?.forecast ?? null;
  const hasOverride =
    forecast?.manual_override_amount !== null && forecast?.manual_override_amount !== undefined;
  const validHistoryCount = forecast?.history_points.filter((point) => point.included).length ?? 0;

  function patchState(patch: Partial<ForecastDialogState>) {
    if (!state) {
      return;
    }
    onChange({ ...state, ...patch });
  }

  return (
    <>
      <Dialog open={Boolean(state)} onOpenChange={onOpenChange}>
        <DialogContent className="sm:max-w-[640px]">
          <DialogHeader>
            <DialogTitle>
              {forecast ? `Прогноз выручки на ${formatDate(forecast.business_date)}` : ""}
            </DialogTitle>
            <DialogDescription>Метод: среднее за 6 одинаковых дней недели</DialogDescription>
          </DialogHeader>
          {forecast && state ? (
            <div className="grid gap-4">
              <div className="grid gap-1 text-sm">
                <div>
                  Статус:{" "}
                  <span className={forecastAmountClass(forecast)}>
                    {forecastStatusText(forecast)}
                  </span>
                </div>
                <div>
                  История: {validHistoryCount} из {forecast.history_window_weeks} точек
                </div>
              </div>

              <div className="overflow-hidden rounded-md border">
                <table className="w-full text-sm">
                  <tbody>
                    {forecast.history_points.map((point) => (
                      <tr className="border-b last:border-b-0" key={point.date}>
                        <td className="px-3 py-2">{formatDate(point.date)}</td>
                        <td className="px-3 py-2 text-right tabular-nums">
                          {point.amount === null ? "—" : formatMoneyWithCurrency(point.amount)}
                        </td>
                        <td className="px-3 py-2 text-right text-muted-foreground">
                          {point.included ? "учтён" : "исключён"}
                        </td>
                      </tr>
                    ))}
                    <tr>
                      <td className="px-3 py-2 font-medium">Среднее</td>
                      <td className="px-3 py-2 text-right font-medium tabular-nums">
                        {forecast.base_average_amount === null
                          ? "—"
                          : formatMoneyWithCurrency(forecast.base_average_amount)}
                      </td>
                      <td />
                    </tr>
                  </tbody>
                </table>
              </div>

              <div className="grid gap-1 text-sm">
                <div>
                  Текущий прогноз:{" "}
                  <span className="font-medium tabular-nums">
                    {forecast.forecast_amount === null
                      ? "—"
                      : formatMoneyWithCurrency(forecast.forecast_amount)}
                  </span>
                  {forecast.quality_status === "manual_override" ? " (override)" : ""}
                </div>
                {forecast.manual_override_set_by_label || forecast.manual_override_set_at ? (
                  <div className="text-muted-foreground">
                    Установил: {forecast.manual_override_set_by_label ?? "—"}
                    {forecast.manual_override_set_at
                      ? ` в ${formatDateTime(forecast.manual_override_set_at)}`
                      : ""}
                  </div>
                ) : null}
                {forecast.manual_override_reason ? (
                  <div className="text-muted-foreground">
                    Причина: {forecast.manual_override_reason}
                  </div>
                ) : null}
              </div>

              <div className="grid gap-3 rounded-md border p-3">
                <div className="grid gap-2">
                  <Label htmlFor="forecast-override-amount">Ручной прогноз</Label>
                  <div className="flex items-center gap-2">
                    <Input
                      id="forecast-override-amount"
                      inputMode="decimal"
                      onChange={(event) => patchState({ amount: event.target.value })}
                      value={state.amount}
                    />
                    <span className="text-sm text-muted-foreground">₽</span>
                  </div>
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="forecast-override-reason">Причина</Label>
                  <Textarea
                    id="forecast-override-reason"
                    maxLength={2000}
                    onChange={(event) => patchState({ reason: event.target.value })}
                    value={state.reason}
                  />
                </div>
              </div>
            </div>
          ) : null}
          <DialogFooter className="gap-2 sm:justify-between sm:space-x-0">
            <Button
              disabled={!hasOverride || isRemoving || isSaving}
              onClick={() => patchState({ removeConfirmOpen: true })}
              type="button"
              variant="outline"
            >
              Снять override
            </Button>
            <div className="flex flex-col-reverse gap-2 sm:flex-row">
              <Button
                disabled={isSaving || isRemoving}
                onClick={() => onOpenChange(false)}
                variant="outline"
              >
                Отмена
              </Button>
              <Button disabled={isSaving || isRemoving} onClick={onSubmit}>
                {isSaving ? <LoaderCircle className="animate-spin" size={16} /> : null}
                Сохранить
              </Button>
            </div>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog
        open={Boolean(state?.removeConfirmOpen)}
        onOpenChange={(open) => patchState({ removeConfirmOpen: open })}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Снять ручной прогноз?</AlertDialogTitle>
            <AlertDialogDescription>Будет применён расчётный.</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isRemoving}>Отмена</AlertDialogCancel>
            <AlertDialogAction
              disabled={isRemoving}
              onClick={(event) => {
                event.preventDefault();
                onRemove();
              }}
            >
              Снять
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

function EmployeeScheduleGrid({
  cashierAllowanceByDay,
  costByShiftId,
  days,
  isLoading,
  isLocked,
  ledgerByDay,
  ledgerByEmployeeDay,
  onEditShift,
  onEmptyCellClick,
  onFilledCellClick,
  roster,
  scheduleRange,
  shiftByEmployeeDay,
  today,
  vacationByEmployeeDay,
  fullscreen = false,
}: {
  cashierAllowanceByDay: Map<string, AllowanceAssignmentRead>;
  costByShiftId: Map<string, ShiftCostEstimateRead>;
  days: string[];
  isLoading: boolean;
  isLocked: boolean;
  ledgerByDay: Map<string, ScheduleLedgerEntryRead[]>;
  ledgerByEmployeeDay: Map<string, ScheduleLedgerEntryRead[]>;
  onEditShift: (shift: ScheduledShiftRead) => void;
  onEmptyCellClick: (employee: EmployeeRosterRow, day: string) => void;
  onFilledCellClick: (shift: ScheduledShiftRead) => void;
  roster: EmployeeRosterRow[];
  scheduleRange: PeriodRange;
  shiftByEmployeeDay: Map<string, ScheduledShiftRead>;
  today: string;
  vacationByEmployeeDay: Map<string, VacationPeriodRead>;
  fullscreen?: boolean;
}) {
  const minWidth = EMPLOYEE_COLUMN_WIDTH + days.length * DAY_CELL_WIDTH;

  return (
    <div className="overflow-hidden rounded-lg border bg-card">
      <div
        className={cn("overflow-auto", fullscreen ? "max-h-[calc(100vh-6rem)]" : "max-h-[70vh]")}
      >
        <table className="border-separate border-spacing-0 text-sm" style={{ minWidth }}>
          <thead>
            <tr>
              <th
                className="sticky left-0 top-0 z-30 border-b border-r bg-muted px-3 py-3 text-left font-medium text-muted-foreground"
                style={{ width: EMPLOYEE_COLUMN_WIDTH, minWidth: EMPLOYEE_COLUMN_WIDTH }}
              >
                Сотрудник
              </th>
              {days.map((day) => (
                <GridDayHeader
                  day={day}
                  ledgerEmpty={day < today && !ledgerByDay.has(day)}
                  key={day}
                />
              ))}
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <LoadingGridRows columns={days.length} stickyWidth={EMPLOYEE_COLUMN_WIDTH} />
            ) : (
              roster.map((employee) => (
                <tr className="hover:bg-muted/30" key={employee.id}>
                  <td
                    className="sticky left-0 z-10 border-b border-r bg-card px-3 py-1.5 align-top text-xs"
                    style={{ width: EMPLOYEE_COLUMN_WIDTH, minWidth: EMPLOYEE_COLUMN_WIDTH }}
                  >
                    <div className="text-sm font-bold leading-tight text-foreground">
                      {employee.full_name}
                    </div>
                    <EmployeeRoleSubtitle
                      employee={employee}
                      payrollRole={primaryRoleLabelSource(employee)}
                    />
                  </td>
                  {days.map((day) => {
                    const shift = shiftByEmployeeDay.get(`${employee.id}:${day}`);
                    const visibleShift = day < today ? null : shift;
                    const ledgerEntries = ledgerByEmployeeDay.get(`${employee.id}:${day}`) ?? [];
                    const vacation = vacationByEmployeeDay.get(`${employee.id}:${day}`);
                    const outsideSchedule = !isDateInRange(day, scheduleRange);
                    const showFact = shouldShowFact(day, ledgerEntries, today);
                    const canEditPlan = !isLocked && day >= today && !outsideSchedule;
                    if (showFact) {
                      return (
                        <td
                          className="min-h-[2rem] border-b border-r bg-muted/10 p-1 align-top"
                          key={day}
                          style={{ width: DAY_CELL_WIDTH, minWidth: DAY_CELL_WIDTH }}
                        >
                          <div className="flex h-full flex-col gap-1">
                            {ledgerEntries.map((entry) => (
                              <LedgerFactPill entry={entry} key={entry.id} />
                            ))}
                          </div>
                        </td>
                      );
                    }
                    if (day > today && outsideSchedule) {
                      return <DisabledScheduleCell key={day} label="вне периода графика" />;
                    }
                    if (vacation && !visibleShift) {
                      return <VacationScheduleCell key={day} period={vacation} />;
                    }
                    return (
                      <td
                        className={cn(
                          "group relative min-h-[2rem] border-b border-r p-1 align-top",
                          canEditPlan && "cursor-pointer hover:bg-primary/5",
                          isLocked && "bg-muted/10",
                        )}
                        key={day}
                        onClick={() => {
                          if (!canEditPlan) {
                            return;
                          }
                          if (!visibleShift) {
                            onEmptyCellClick(employee, day);
                          } else {
                            onFilledCellClick(visibleShift);
                          }
                        }}
                        style={{ width: DAY_CELL_WIDTH, minWidth: DAY_CELL_WIDTH }}
                      >
                        {visibleShift ? (
                          <>
                            <div className="flex h-full flex-col gap-1">
                              <ShiftPill
                                allowanceAssignment={cashierAllowanceByDay.get(day)}
                                employee={employee}
                                estimate={costByShiftId.get(visibleShift.id)}
                                shift={visibleShift}
                              />
                            </div>
                            {canEditPlan ? (
                              <EditShiftButton
                                businessDate={day}
                                employeeId={employee.id}
                                onClick={() => onEditShift(visibleShift)}
                              />
                            ) : null}
                          </>
                        ) : null}
                      </td>
                    );
                  })}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function StationScheduleGrid({
  cashierAllowanceByDay,
  costByShiftId,
  days,
  isLoading,
  isLocked,
  ledgerByDay,
  ledgerByStationDay,
  onCellClick,
  onShiftDelete,
  onShiftClick,
  roster,
  rows,
  scheduleRange,
  today,
  fullscreen = false,
}: {
  cashierAllowanceByDay: Map<string, AllowanceAssignmentRead>;
  costByShiftId: Map<string, ShiftCostEstimateRead>;
  days: string[];
  isLoading: boolean;
  isLocked: boolean;
  ledgerByDay: Map<string, ScheduleLedgerEntryRead[]>;
  ledgerByStationDay: Map<string, ScheduleLedgerEntryRead[]>;
  onCellClick: (station: string, day: string) => void;
  onShiftDelete: (shift: ScheduledShiftRead) => void;
  onShiftClick: (shift: ScheduledShiftRead) => void;
  roster: EmployeeRosterRow[];
  rows: Array<{ station: string; byDay: Map<string, ScheduledShiftRead[]> }>;
  scheduleRange: PeriodRange;
  today: string;
  fullscreen?: boolean;
}) {
  const minWidth = STATION_COLUMN_WIDTH + days.length * DAY_CELL_WIDTH;

  return (
    <div className="overflow-hidden rounded-lg border bg-card">
      <div
        className={cn("overflow-auto", fullscreen ? "max-h-[calc(100vh-6rem)]" : "max-h-[70vh]")}
      >
        <table className="border-separate border-spacing-0 text-sm" style={{ minWidth }}>
          <thead>
            <tr>
              <th
                className="sticky left-0 top-0 z-30 border-b border-r bg-muted px-3 py-3 text-left font-medium text-muted-foreground"
                style={{ width: STATION_COLUMN_WIDTH, minWidth: STATION_COLUMN_WIDTH }}
              >
                Станция
              </th>
              {days.map((day) => (
                <GridDayHeader
                  day={day}
                  ledgerEmpty={day < today && !ledgerByDay.has(day)}
                  key={day}
                />
              ))}
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <LoadingGridRows columns={days.length} stickyWidth={STATION_COLUMN_WIDTH} />
            ) : (
              rows.map((row) => (
                <tr className="hover:bg-muted/30" key={row.station}>
                  <td
                    className="sticky left-0 z-10 border-b border-r bg-card px-3 py-1.5 align-top text-xs font-medium"
                    style={{ width: STATION_COLUMN_WIDTH, minWidth: STATION_COLUMN_WIDTH }}
                  >
                    {row.station}
                  </td>
                  {days.map((day) => {
                    const dayShifts = row.byDay.get(day) ?? [];
                    const visibleShifts = day < today ? [] : dayShifts;
                    const ledgerEntries = ledgerByStationDay.get(`${row.station}:${day}`) ?? [];
                    const outsideSchedule = !isDateInRange(day, scheduleRange);
                    const showFact = shouldShowFact(day, ledgerEntries, today);
                    const canEditPlan =
                      !isLocked && day >= today && !outsideSchedule && visibleShifts.length === 0;
                    if (showFact) {
                      return (
                        <td
                          className="min-h-[2rem] border-b border-r bg-muted/10 p-1 align-top"
                          key={day}
                          style={{ width: DAY_CELL_WIDTH, minWidth: DAY_CELL_WIDTH }}
                        >
                          <div className="space-y-0.5">
                            {ledgerEntries.map((entry) => (
                              <LedgerFactPill entry={entry} key={entry.id} />
                            ))}
                          </div>
                        </td>
                      );
                    }
                    if (day > today && outsideSchedule) {
                      return <DisabledScheduleCell key={day} label="вне периода графика" />;
                    }
                    return (
                      <td
                        className={cn(
                          "min-h-[2rem] border-b border-r p-1 align-top",
                          canEditPlan && "cursor-pointer hover:bg-primary/5",
                          isLocked && "bg-muted/10",
                        )}
                        key={day}
                        onClick={() => {
                          if (canEditPlan) {
                            onCellClick(row.station, day);
                          }
                        }}
                        style={{ width: DAY_CELL_WIDTH, minWidth: DAY_CELL_WIDTH }}
                      >
                        <div className="space-y-0.5">
                          {visibleShifts.map((shift) => (
                            <StationShiftCard
                              allowanceAssignment={cashierAllowanceByDay.get(day)}
                              costByShiftId={costByShiftId}
                              isLocked={isLocked}
                              key={shift.id}
                              onShiftClick={onShiftClick}
                              onShiftDelete={onShiftDelete}
                              roster={roster}
                              shift={shift}
                            />
                          ))}
                        </div>
                      </td>
                    );
                  })}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function StationShiftCard({
  allowanceAssignment,
  costByShiftId,
  isLocked,
  onShiftClick,
  onShiftDelete,
  roster,
  shift,
}: {
  allowanceAssignment: AllowanceAssignmentRead | undefined;
  costByShiftId: Map<string, ShiftCostEstimateRead>;
  isLocked: boolean;
  onShiftClick: (shift: ScheduledShiftRead) => void;
  onShiftDelete: (shift: ScheduledShiftRead) => void;
  roster: EmployeeRosterRow[];
  shift: ScheduledShiftRead;
}) {
  const employee = roster.find((item) => item.id === shift.employee_id);
  const allowanceBadge = employee ? cashierAllowanceBadge(employee, allowanceAssignment) : null;

  return (
    <div
      className={cn(
        "group relative w-full rounded border px-1.5 py-0.5 pr-7 text-left text-[11px] leading-tight",
        !isLocked && "cursor-pointer",
        roleColorClasses(shift.payroll_role).container,
      )}
      onClick={(event) => {
        event.stopPropagation();
        if (!isLocked) {
          onShiftDelete(shift);
        }
      }}
      title={shiftTitle(shift, costByShiftId.get(shift.id), employee, allowanceAssignment)}
    >
      <div className={cn("truncate font-medium", roleColorClasses(shift.payroll_role).primaryText)}>
        {shift.employee_full_name}
      </div>
      {isFullDayShift(shift) ? null : (
        <div className={cn("tabular-nums", roleColorClasses(shift.payroll_role).secondaryText)}>
          {formatShiftTime(shift)}
        </div>
      )}
      <AllowanceBadge badge={allowanceBadge} />
      {!isLocked ? (
        <EditShiftButton
          businessDate={shift.business_date}
          employeeId={shift.employee_id}
          onClick={() => onShiftClick(shift)}
        />
      ) : null}
    </div>
  );
}

function EditShiftButton({
  businessDate,
  employeeId,
  onClick,
}: {
  businessDate: string;
  employeeId: string;
  onClick: () => void;
}) {
  return (
    <button
      aria-label="Редактировать смену"
      className="absolute right-1 top-1 inline-flex h-6 w-6 items-center justify-center rounded-sm border bg-background/95 text-muted-foreground opacity-0 shadow-sm transition hover:text-foreground focus:opacity-100 focus:outline-none focus:ring-2 focus:ring-ring group-focus-within:opacity-100 group-hover:opacity-100"
      data-business-date={businessDate}
      data-employee-id={employeeId}
      onClick={(event) => {
        event.stopPropagation();
        onClick();
      }}
      title="Редактировать смену"
      type="button"
    >
      <Pencil size={14} aria-hidden="true" />
    </button>
  );
}

function EmployeeRoleSubtitle({
  employee,
  payrollRole,
}: {
  employee: EmployeeRosterRow;
  payrollRole?: string | null;
}) {
  const badge = employeeAllowanceFlag(employee);
  const role = rosterRoleForPayrollRole(employee, payrollRole);

  return (
    <div className="mt-1 flex min-w-0 items-center gap-1 text-xs text-muted-foreground">
      <span className="truncate">{employeeRoleLine(employee, payrollRole)}</span>
      {role?.is_substitute ? (
        <Badge className="h-4 shrink-0 rounded-sm border-sky-200 bg-sky-50 px-1 text-[10px] leading-none text-sky-700 shadow-none">
          подмена
        </Badge>
      ) : null}
      {badge ? <InlineAllowanceBadge role={badge} /> : null}
    </div>
  );
}

function InlineAllowanceBadge({ role }: { role: "senior" | "deputy_senior" }) {
  return (
    <span className="inline-flex h-4 min-w-5 shrink-0 items-center justify-center rounded-sm border border-muted-foreground/30 px-1 text-[10px] leading-none text-muted-foreground">
      {allowanceRoleShortLabel(role)}
    </span>
  );
}

function RosterRoleSelectLabel({ role }: { role: EmployeeRosterRow["available_roles"][number] }) {
  return (
    <span className="flex min-w-0 items-center gap-2">
      <span className="truncate">{payrollRoleLabel(role.payroll_role)}</span>
      {role.is_substitute ? (
        <Badge className="h-5 shrink-0 rounded-sm border-sky-200 bg-sky-50 px-1.5 text-[10px] leading-none text-sky-700 shadow-none">
          подмена
        </Badge>
      ) : null}
    </span>
  );
}

function LedgerFactPill({ entry }: { entry: ScheduleLedgerEntryRead }) {
  const colors = roleColorClasses(entry.payroll_role ?? "");

  return (
    <div
      className={cn(
        "flex h-full min-h-[1.75rem] flex-1 items-center justify-between gap-1 rounded border px-1.5 py-0.5 text-[11px] leading-tight",
        colors.container,
      )}
      title={ledgerFactTitle(entry)}
    >
      <span className={cn("truncate font-semibold tabular-nums", colors.primaryText)}>
        {formatLedgerTime(entry)}
      </span>
      {!entry.is_closed ? (
        <span className="shrink-0 rounded-sm bg-amber-100 px-1 text-[9px] leading-tight text-amber-700">
          не закрыта
        </span>
      ) : null}
    </div>
  );
}

function ShiftPill({
  allowanceAssignment,
  employee,
  estimate,
  shift,
}: {
  allowanceAssignment: AllowanceAssignmentRead | undefined;
  employee: EmployeeRosterRow;
  estimate: ShiftCostEstimateRead | undefined;
  shift: ScheduledShiftRead;
}) {
  const colors = roleColorClasses(shift.payroll_role);
  const allowanceBadge = cashierAllowanceBadge(employee, allowanceAssignment);

  return (
    <div
      className={cn(
        "flex h-full min-h-[1.75rem] flex-1 flex-col justify-center rounded border px-1.5 py-0.5 text-[11px] leading-tight",
        colors.container,
      )}
      title={shiftTitle(shift, estimate, employee, allowanceAssignment)}
    >
      {isFullDayShift(shift) ? null : (
        <div className={cn("font-semibold tabular-nums", colors.primaryText)}>
          {formatShiftTime(shift)}
        </div>
      )}
      <div className="flex items-center gap-1">
        <span className={cn("min-w-0 truncate", colors.secondaryText)}>
          {payrollRoleLabel(shift.payroll_role)}
        </span>
        <AllowanceBadge badge={allowanceBadge} inline />
      </div>
    </div>
  );
}

type AllowanceBadgeInfo = {
  label: string;
  className: string;
  title: string;
};

function AllowanceBadge({
  badge,
  inline = false,
}: {
  badge: AllowanceBadgeInfo | null;
  inline?: boolean;
}) {
  if (!badge) {
    return null;
  }
  return (
    <span
      className={cn(
        "inline-flex h-4 min-w-6 items-center justify-center rounded-sm border px-1 text-[10px] font-medium leading-none",
        inline ? "shrink-0" : "mt-0.5",
        badge.className,
      )}
      title={badge.title}
    >
      {badge.label}
    </span>
  );
}

function cashierAllowanceBadge(
  employee: EmployeeRosterRow,
  assignment: AllowanceAssignmentRead | undefined,
): AllowanceBadgeInfo | null {
  const employeeFlag = employeeAllowanceFlag(employee);
  if (!employeeFlag) {
    return null;
  }
  if (employee.position === "Повар") {
    return coloredAllowanceBadge(employeeFlag, "Надбавка повара начисляется независимо");
  }
  if (employee.position !== "Кассир") {
    return null;
  }
  if (!assignment) {
    return coloredAllowanceBadge(employeeFlag, "Надбавка администратора");
  }
  if (assignment.recipient_employee_id === employee.id) {
    return coloredAllowanceBadge(
      assignment.recipient_role === "deputy_senior" ? "deputy_senior" : "senior",
      `Надбавка администратора: ${allowanceReasonLabel(assignment.reason)}`,
    );
  }
  const recipient = assignment.recipient_full_name ?? "другому сотруднику";
  return {
    label: allowanceRoleShortLabel(employeeFlag),
    className: "border-slate-300 bg-slate-50 text-slate-500 line-through",
    title: `Надбавка отошла ${recipient} (${allowanceReasonLabel(assignment.reason)})`,
  };
}

function coloredAllowanceBadge(role: "senior" | "deputy_senior", title: string) {
  return {
    label: allowanceRoleShortLabel(role),
    className:
      role === "senior"
        ? "border-orange-300 bg-orange-50 text-orange-700"
        : "border-blue-300 bg-blue-50 text-blue-700",
    title,
  };
}

function employeeAllowanceFlag(employee: EmployeeRosterRow): "senior" | "deputy_senior" | null {
  if (employee.allowances.senior) {
    return "senior";
  }
  if (employee.allowances.deputy) {
    return "deputy_senior";
  }
  return null;
}

function allowanceRoleShortLabel(role: "senior" | "deputy_senior") {
  return role === "senior" ? "ст" : "зам";
}

function GridDayHeader({ day, ledgerEmpty = false }: { day: string; ledgerEmpty?: boolean }) {
  return (
    <th
      className="sticky top-0 z-20 border-b border-r bg-muted px-2 py-1 text-center text-xs font-medium text-muted-foreground"
      style={{ width: DAY_CELL_WIDTH, minWidth: DAY_CELL_WIDTH }}
    >
      <div>{weekdayLabels[parseIsoDate(day).getDay()]}</div>
      <div className="text-sm text-foreground">{day.slice(8, 10)}</div>
      {ledgerEmpty ? (
        <div className="mt-1 text-[10px] font-normal leading-3 text-muted-foreground">
          Учёт смен пуст
        </div>
      ) : null}
    </th>
  );
}

function DisabledScheduleCell({ label }: { label: string }) {
  return (
    <td
      className="min-h-[2rem] border-b border-r bg-muted/30 p-1 align-middle text-center text-[11px] leading-tight text-muted-foreground"
      style={{ width: DAY_CELL_WIDTH, minWidth: DAY_CELL_WIDTH }}
      title={label}
    >
      {label}
    </td>
  );
}

function VacationScheduleCell({ period }: { period: VacationPeriodRead }) {
  return (
    <td
      className="min-h-[2rem] border-b border-r bg-emerald-50 p-1 align-middle text-center text-[11px] leading-tight text-emerald-800"
      style={{ width: DAY_CELL_WIDTH, minWidth: DAY_CELL_WIDTH }}
      title={`Отпуск: ${formatShortRange(period.date_start, period.date_end)}`}
    >
      <div className="font-medium">отпуск</div>
      <div className="mt-1 tabular-nums">{period.days_count} дн.</div>
    </td>
  );
}

function LoadingGridRows({ columns, stickyWidth }: { columns: number; stickyWidth: number }) {
  return (
    <>
      {Array.from({ length: 6 }).map((_, rowIndex) => (
        <tr key={rowIndex}>
          <td
            className="sticky left-0 z-10 border-b border-r bg-card px-3 py-3"
            style={{ width: stickyWidth, minWidth: stickyWidth }}
          >
            <Skeleton className="h-8 w-36" />
          </td>
          {Array.from({ length: columns }).map((__, columnIndex) => (
            <td className="border-b border-r p-2" key={columnIndex}>
              <Skeleton className="h-10 w-full" />
            </td>
          ))}
        </tr>
      ))}
    </>
  );
}

function CreateScheduleDialog({
  draft,
  isSaving,
  onChange,
  onOpenChange,
  onPeriodRangeApply,
  onPeriodPresetChange,
  onSubmit,
  open,
  periodPreset,
}: {
  draft: ScheduleCreatePayload;
  isSaving: boolean;
  onChange: (draft: ScheduleCreatePayload) => void;
  onOpenChange: (open: boolean) => void;
  onPeriodRangeApply: (range: PeriodRange) => void;
  onPeriodPresetChange: (preset: PeriodPreset) => void;
  onSubmit: () => void;
  open: boolean;
  periodPreset: PeriodPreset;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Новый график</DialogTitle>
          <DialogDescription>Период черновика можно выбрать только при создании.</DialogDescription>
        </DialogHeader>
        <div className="grid gap-4">
          <PeriodToolbar
            className="rounded-md"
            compact
            onCustomRangeApply={onPeriodRangeApply}
            onPresetChange={onPeriodPresetChange}
            preset={periodPreset}
            range={{ from: draft.date_start, to: draft.date_end }}
          />
          <div className="grid gap-2">
            <Label htmlFor="schedule-notes">Заметки</Label>
            <Textarea
              id="schedule-notes"
              onChange={(event) => onChange({ ...draft, notes: event.target.value })}
              value={draft.notes ?? ""}
            />
          </div>
        </div>
        <DialogFooter>
          <Button disabled={isSaving} onClick={() => onOpenChange(false)} variant="outline">
            Отмена
          </Button>
          <Button disabled={isSaving} onClick={onSubmit}>
            {isSaving ? <LoaderCircle className="animate-spin" size={16} /> : null}
            Создать draft
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ShiftDialog({
  allowanceAssignment,
  employees,
  isAllowanceLoading,
  isRemovingAllowance,
  isSaving,
  onDelete,
  onRemoveAllowance,
  onOpenChange,
  onSubmit,
  setValue,
  state,
}: {
  allowanceAssignment: AllowanceAssignmentRead | undefined;
  employees: EmployeeRosterRow[];
  isAllowanceLoading: boolean;
  isRemovingAllowance: boolean;
  isSaving: boolean;
  onDelete: (shift: ScheduledShiftRead) => void;
  onRemoveAllowance: () => void;
  onOpenChange: (open: boolean) => void;
  onSubmit: () => void;
  setValue: (state: ShiftDialogState | null) => void;
  state: ShiftDialogState | null;
}) {
  const selectedEmployee = employees.find((employee) => employee.id === state?.employeeId);
  const employeeOptions =
    state?.mode === "create" && state.stationCode
      ? employees.filter((employee) => defaultRoleForEmployeeAtStation(employee, state.stationCode))
      : employees;
  const selectedRole = selectedEmployee?.available_roles.find(
    (role) => role.payroll_role === state?.payrollRole,
  );
  const plannedHours = state ? hoursBetween(state.businessDate, state.startTime, state.endTime) : 0;
  const allowanceCandidates = allowanceAssignment?.candidates ?? [];
  const showAllowanceSection =
    state?.mode === "edit" &&
    selectedEmployee?.position === "Кассир" &&
    allowanceCandidates.filter((candidate) => candidate.is_senior || candidate.is_deputy_senior)
      .length >= 2;
  const autoAllowanceText = allowanceAssignment
    ? `${allowanceAssignment.recipient_full_name ?? "никто"}, ${allowanceRoleLabel(
        allowanceAssignment.recipient_role,
      )}`
    : "";

  function patchState(patch: Partial<ShiftDialogState>) {
    if (!state) {
      return;
    }
    setValue({ ...state, ...patch });
  }

  return (
    <Dialog open={Boolean(state)} onOpenChange={onOpenChange}>
      <DialogContent preventClose={isSaving}>
        <DialogHeader>
          <DialogTitle>
            {state?.mode === "edit"
              ? `Смена ${formatDate(state.businessDate)}, ${
                  selectedEmployee?.full_name ?? state.shift?.employee_full_name ?? ""
                }`
              : "Новая смена"}
          </DialogTitle>
          <DialogDescription>
            {state?.mode === "create"
              ? formatDate(state.businessDate)
              : selectedRole
                ? payrollRoleLabel(selectedRole.payroll_role)
                : ""}
          </DialogDescription>
        </DialogHeader>
        {state ? (
          <div className="grid gap-4">
            {!state.compact ? (
            <div className="grid gap-2">
              <Label>Сотрудник</Label>
              <Select
                disabled={state.mode === "edit"}
                onValueChange={(value) => {
                  const employee = employees.find((item) => item.id === value);
                  const stationCode = state.stationCode;
                  patchState({
                    employeeId: value === NO_VALUE ? "" : value,
                    payrollRole:
                      value === NO_VALUE
                        ? null
                        : (defaultRoleForEmployeeAtStation(employee, stationCode) ??
                          employee?.primary_payroll_role ??
                          null),
                    stationCode,
                  });
                }}
                value={state.employeeId || NO_VALUE}
              >
                <SelectTrigger>
                  <SelectValue placeholder="Выберите сотрудника" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem disabled value={NO_VALUE}>
                    Выберите сотрудника
                  </SelectItem>
                  {employeeOptions.map((employee) => (
                    <SelectItem key={employee.id} value={employee.id}>
                      {employee.full_name} ·{" "}
                      {employeeRoleLine(
                        employee,
                        defaultRoleForEmployeeAtStation(employee, state.stationCode) ??
                          primaryRoleLabelSource(employee),
                      )}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            ) : null}
            <div className="grid gap-2">
              <Label>Роль</Label>
              <Select
                disabled={!selectedEmployee || selectedEmployee.available_roles.length === 0}
                onValueChange={(value) => {
                  const role = selectedEmployee?.available_roles.find(
                    (item) => item.payroll_role === value,
                  );
                  patchState({
                    payrollRole: value,
                    stationCode: role?.default_station_code ?? null,
                  });
                }}
                value={state.payrollRole ?? undefined}
              >
                <SelectTrigger>
                  <SelectValue placeholder="Выберите роль" />
                </SelectTrigger>
                <SelectContent>
                  {(selectedEmployee?.available_roles ?? []).map((role) => (
                    <SelectItem key={role.payroll_role} value={role.payroll_role}>
                      <RosterRoleSelectLabel role={role} />
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {!state.compact ? (
            <div className="grid gap-2">
              <Label>Станция</Label>
              <Select
                onValueChange={(value) => {
                  const stationCode = value === NO_VALUE ? null : value;
                  const shouldResetEmployee =
                    state.mode === "create" &&
                    selectedEmployee &&
                    stationCode &&
                    !defaultRoleForEmployeeAtStation(selectedEmployee, stationCode);
                  const payrollRole =
                    state.mode === "create"
                      ? defaultRoleForEmployeeAtStation(selectedEmployee, stationCode)
                      : state.payrollRole;
                  patchState({
                    stationCode,
                    payrollRole: shouldResetEmployee ? null : (payrollRole ?? state.payrollRole),
                    employeeId: shouldResetEmployee ? "" : state.employeeId,
                  });
                }}
                value={state.stationCode ?? NO_VALUE}
              >
                <SelectTrigger>
                  <SelectValue placeholder="Без станции" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={NO_VALUE}>Без станции</SelectItem>
                  {stationOptions.map((station) => (
                    <SelectItem key={station} value={station}>
                      {station}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            ) : null}
            <div className="grid grid-cols-2 gap-3">
              <div className="grid gap-2">
                <Label htmlFor="shift-start">Начало</Label>
                <Input
                  id="shift-start"
                  onChange={(event) => patchState({ startTime: event.target.value })}
                  type="time"
                  value={state.startTime}
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="shift-end">Конец</Label>
                <Input
                  id="shift-end"
                  onChange={(event) => patchState({ endTime: event.target.value })}
                  type="time"
                  value={state.endTime}
                />
              </div>
            </div>
            {!state.compact ? (
            <div className="grid gap-2">
              <Label htmlFor="shift-comment">Комментарий</Label>
              <Textarea
                id="shift-comment"
                onChange={(event) => patchState({ comment: event.target.value })}
                value={state.comment}
              />
            </div>
            ) : null}
            {showAllowanceSection ? (
              <div className="grid gap-3 rounded-md border p-3">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="text-sm font-medium">Кому идёт надбавка администратора</div>
                    <div className="mt-1 text-xs text-muted-foreground">
                      Авто-правило: {autoAllowanceText}
                    </div>
                  </div>
                  {isAllowanceLoading ? (
                    <LoaderCircle className="h-4 w-4 animate-spin text-muted-foreground" />
                  ) : null}
                </div>
                <div className="grid gap-1.5 text-sm">
                  <label className="flex items-center gap-2">
                    <input
                      checked={state.allowanceSelection === "auto"}
                      onChange={() =>
                        patchState({
                          allowanceSelection: "auto",
                          allowanceRecipientEmployeeId: null,
                          allowanceDirty: true,
                        })
                      }
                      type="radio"
                    />
                    Авто ({autoAllowanceText})
                  </label>
                  {allowanceCandidates.map((candidate) => {
                    const role = candidate.is_senior ? "senior" : "deputy_senior";
                    return (
                      <label className="flex items-center gap-2" key={candidate.employee_id}>
                        <input
                          checked={
                            state.allowanceSelection === role &&
                            state.allowanceRecipientEmployeeId === candidate.employee_id
                          }
                          onChange={() =>
                            patchState({
                              allowanceSelection: role,
                              allowanceRecipientEmployeeId: candidate.employee_id,
                              allowanceDirty: true,
                            })
                          }
                          type="radio"
                        />
                        {candidate.full_name} ({allowanceRoleLabel(role)})
                      </label>
                    );
                  })}
                  <label className="flex items-center gap-2">
                    <input
                      checked={state.allowanceSelection === "none"}
                      onChange={() =>
                        patchState({
                          allowanceSelection: "none",
                          allowanceRecipientEmployeeId: null,
                          allowanceDirty: true,
                        })
                      }
                      type="radio"
                    />
                    Никто
                  </label>
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="allowance-comment">Комментарий</Label>
                  <Textarea
                    id="allowance-comment"
                    maxLength={2000}
                    onChange={(event) =>
                      patchState({
                        allowanceComment: event.target.value,
                        allowanceDirty: true,
                      })
                    }
                    value={state.allowanceComment}
                  />
                </div>
                <div>
                  <Button
                    disabled={!state.allowanceOverrideId || isRemovingAllowance}
                    onClick={onRemoveAllowance}
                    type="button"
                    variant="outline"
                  >
                    Снять выбор
                  </Button>
                </div>
              </div>
            ) : null}
            <div className="text-sm text-muted-foreground">
              Часов:{" "}
              <span className="font-medium text-foreground">
                {Number.isFinite(plannedHours) ? formatHours(plannedHours) : "—"}
              </span>
              {selectedEmployee?.allowances.senior ? " · Старший" : ""}
            </div>
          </div>
        ) : null}
        <DialogFooter className="gap-2 sm:justify-between sm:space-x-0">
          <div>
            {state?.shift ? (
              <Button
                disabled={isSaving}
                onClick={() => state.shift && onDelete(state.shift)}
                type="button"
                variant="outline"
              >
                Удалить
              </Button>
            ) : null}
          </div>
          <div className="flex flex-col-reverse gap-2 sm:flex-row">
            <Button disabled={isSaving} onClick={() => onOpenChange(false)} variant="outline">
              Отмена
            </Button>
            <Button disabled={isSaving} onClick={onSubmit}>
              {isSaving ? <LoaderCircle className="animate-spin" size={16} /> : null}
              Сохранить
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function CopyWeekDialog({
  copyDialog,
  isSaving,
  onChange,
  onSubmit,
  selectedWeekEnd,
  selectedWeekStart,
}: {
  copyDialog: CopyWeekState;
  isSaving: boolean;
  onChange: (state: CopyWeekState) => void;
  onSubmit: () => void;
  selectedWeekEnd: string;
  selectedWeekStart: string;
}) {
  const nextStart = toIsoDate(addDays(parseIsoDate(selectedWeekStart), 7));
  const nextEnd = toIsoDate(addDays(parseIsoDate(selectedWeekEnd), 7));

  return (
    <Dialog open={copyDialog.open} onOpenChange={(open) => onChange({ ...copyDialog, open })}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Копировать неделю</DialogTitle>
          <DialogDescription>
            Копировать смены недели {formatDate(selectedWeekStart)} — {formatDate(selectedWeekEnd)}
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-3">
          <button
            className={cn(
              "rounded-md border px-3 py-2 text-left text-sm",
              copyDialog.targetMode === "next" && "border-primary bg-primary/5",
            )}
            onClick={() => onChange({ ...copyDialog, targetMode: "next" })}
            type="button"
          >
            Следующая: {formatDate(nextStart)} — {formatDate(nextEnd)}
          </button>
          <button
            className={cn(
              "rounded-md border px-3 py-2 text-left text-sm",
              copyDialog.targetMode === "custom" && "border-primary bg-primary/5",
            )}
            onClick={() => onChange({ ...copyDialog, targetMode: "custom" })}
            type="button"
          >
            Произвольная дата начала
          </button>
          {copyDialog.targetMode === "custom" ? (
            <Input
              onChange={(event) => onChange({ ...copyDialog, customDate: event.target.value })}
              type="date"
              value={copyDialog.customDate}
            />
          ) : null}
        </div>
        <DialogFooter>
          <Button
            disabled={isSaving}
            onClick={() => onChange({ ...copyDialog, open: false })}
            variant="outline"
          >
            Отмена
          </Button>
          <Button disabled={isSaving} onClick={onSubmit}>
            {isSaving ? <LoaderCircle className="animate-spin" size={16} /> : null}
            Копировать
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function SegmentedButton({
  active,
  icon,
  label,
  onClick,
}: {
  active: boolean;
  icon?: ReactNode;
  label: string;
  onClick: () => void;
}) {
  return (
    <Button
      className={cn(active && "border-primary bg-primary/10 text-primary")}
      onClick={onClick}
      type="button"
      variant="outline"
    >
      {icon}
      {label}
    </Button>
  );
}

function planFactSummaryMetrics(summary: PlanFactSummaryRead) {
  const threshold = decimalToNumber(summary.warning_threshold_pct) ?? 3;
  const actual = summary.actual;
  const deviation = summary.deviation;
  const rows = [
    {
      label: "Смен",
      planned: String(summary.planned.total_shifts),
      actual: actual ? String(actual.total_shifts) : "—",
      deviation: formatCountDeviation(
        actual?.total_shifts ?? null,
        summary.planned.total_shifts,
        deviation?.shifts_pct ?? null,
      ),
      status: metricDeviationStatus(deviation?.shifts_pct ?? null, threshold),
      className: deviationTextClass(deviation?.shifts_pct ?? null, threshold),
    },
    {
      label: "Часов",
      planned: formatHoursValue(summary.planned.total_hours),
      actual: formatHoursValue(actual?.total_hours ?? null),
      deviation: formatNumberDeviation(
        actual?.total_hours ?? null,
        summary.planned.total_hours,
        deviation?.hours_pct ?? null,
      ),
      status: metricDeviationStatus(deviation?.hours_pct ?? null, threshold),
      className: deviationTextClass(deviation?.hours_pct ?? null, threshold),
    },
    {
      label: "Стоимость",
      planned: formatMoneyWithCurrency(summary.planned.total_cost),
      actual: formatMoneyWithCurrency(actual?.total_cost ?? null),
      deviation: formatMoneyDeviation(
        actual?.total_cost ?? null,
        summary.planned.total_cost,
        deviation?.cost_pct ?? null,
      ),
      status: metricDeviationStatus(deviation?.cost_pct ?? null, threshold),
      className: deviationTextClass(deviation?.cost_pct ?? null, threshold),
    },
    {
      label: "Выручка",
      planned: formatMoneyWithCurrency(summary.planned.total_revenue),
      actual: formatMoneyWithCurrency(actual?.total_revenue ?? null),
      deviation: formatMoneyDeviation(
        actual?.total_revenue ?? null,
        summary.planned.total_revenue,
        deviation?.revenue_pct ?? null,
      ),
      status: metricDeviationStatus(deviation?.revenue_pct ?? null, threshold),
      className: deviationTextClass(deviation?.revenue_pct ?? null, threshold),
    },
    {
      label: "ФОТ %",
      planned: formatPercent(summary.planned.fot_pct),
      actual: formatPercent(actual?.fot_pct ?? null),
      deviation: formatPpDiff(deviation?.fot_pct_diff ?? null),
      status: metricDeviationStatus(deviation?.fot_pct_diff ?? null, threshold),
      className: deviationTextClass(deviation?.fot_pct_diff ?? null, threshold),
    },
  ];
  return rows;
}

function planFactTotalDays(summary: PlanFactSummaryRead) {
  return (
    Math.floor(
      (parseIsoDate(summary.schedule.date_end).getTime() -
        parseIsoDate(summary.schedule.date_start).getTime()) /
        86_400_000,
    ) + 1
  );
}

function factAvailabilityText(value: PlanFactSummaryRead["fact_availability"]) {
  if (value === "full") {
    return "Полностью";
  }
  if (value === "partial") {
    return "Частично";
  }
  return "Нет факта";
}

function factAvailabilityClass(value: PlanFactSummaryRead["fact_availability"]) {
  if (value === "full") {
    return "border-emerald-200 bg-emerald-50 text-emerald-700";
  }
  if (value === "partial") {
    return "border-blue-200 bg-blue-50 text-blue-700";
  }
  return "border-muted bg-muted/40 text-muted-foreground";
}

function planFactStatusClass(status: PlanFactDeviationStatus) {
  const classes: Record<PlanFactDeviationStatus, string> = {
    no_data: "border-muted bg-muted/40 text-muted-foreground",
    within_threshold: "border-emerald-200 bg-emerald-50 text-emerald-700",
    over_threshold: "border-amber-200 bg-amber-50 text-amber-700",
    plan_no_fact: "border-blue-200 bg-blue-50 text-blue-700",
    fact_no_plan: "border-red-200 bg-red-50 text-red-700",
  };
  return classes[status] ?? classes.no_data;
}

function nextSort<T extends string>(
  current: { key: T; direction: SortDirection },
  key: T,
): { key: T; direction: SortDirection } {
  if (current.key === key) {
    return {
      key,
      direction: current.direction === "asc" ? "desc" : "asc",
    };
  }
  return {
    key,
    direction: key === "date" || key === "name" ? "asc" : "desc",
  };
}

function sortPlanFactDays(
  rows: PlanFactDayRowRead[],
  sort: { key: DaySortKey; direction: SortDirection },
) {
  return [...rows].sort((left, right) => {
    if (sort.key === "date") {
      return sort.direction === "asc"
        ? left.business_date.localeCompare(right.business_date)
        : right.business_date.localeCompare(left.business_date);
    }
    if (sort.key === "hours") {
      return compareNullable(
        absDecimal(left.hours_deviation_pct),
        absDecimal(right.hours_deviation_pct),
        sort.direction,
      );
    }
    if (sort.key === "cost") {
      return compareNullable(
        absDecimal(left.cost_deviation_pct),
        absDecimal(right.cost_deviation_pct),
        sort.direction,
      );
    }
    return compareStatus(left.deviation_status, right.deviation_status, sort.direction);
  });
}

function sortPlanFactEmployees(
  rows: PlanFactEmployeeRowRead[],
  sort: { key: EmployeeSortKey; direction: SortDirection },
) {
  return [...rows].sort((left, right) => {
    if (sort.key === "name") {
      return sort.direction === "asc"
        ? left.full_name.localeCompare(right.full_name, "ru")
        : right.full_name.localeCompare(left.full_name, "ru");
    }
    if (sort.key === "hours") {
      return compareNullable(
        absDecimal(left.hours_deviation_pct),
        absDecimal(right.hours_deviation_pct),
        sort.direction,
      );
    }
    if (sort.key === "cost") {
      return compareNullable(
        absDecimal(left.cost_deviation_pct),
        absDecimal(right.cost_deviation_pct),
        sort.direction,
      );
    }
    return compareStatus(left.deviation_status, right.deviation_status, sort.direction);
  });
}

function compareNullable(left: number | null, right: number | null, direction: SortDirection) {
  if (left === null && right === null) {
    return 0;
  }
  if (left === null) {
    return 1;
  }
  if (right === null) {
    return -1;
  }
  return direction === "asc" ? left - right : right - left;
}

function compareStatus(
  left: PlanFactDeviationStatus,
  right: PlanFactDeviationStatus,
  direction: SortDirection,
) {
  const rank: Record<PlanFactDeviationStatus, number> = {
    over_threshold: 0,
    fact_no_plan: 1,
    plan_no_fact: 2,
    no_data: 3,
    within_threshold: 4,
  };
  const delta = rank[left] - rank[right];
  return direction === "asc" ? delta : -delta;
}

function absDecimal(value: string | number | null) {
  const amount = decimalToNumber(value);
  return amount === null ? null : Math.abs(amount);
}

function metricDeviationStatus(
  value: string | number | null,
  threshold: number,
): "none" | "within" | "over" {
  const amount = decimalToNumber(value);
  if (amount === null) {
    return "none";
  }
  return Math.abs(amount) > threshold ? "over" : "within";
}

function deviationTextClass(value: string | number | null, threshold: number) {
  const amount = decimalToNumber(value);
  if (amount === null) {
    return "text-muted-foreground";
  }
  return Math.abs(amount) > threshold ? "text-amber-700" : "text-emerald-700";
}

function formatPlanFactTriplet(
  shifts: number,
  hours: string | number | null,
  cost: string | number | null,
  isActual = false,
) {
  if (isActual && shifts === 0 && hours === null && cost === null) {
    return "— / — / —";
  }
  return `${shifts} / ${formatHoursValue(hours)} / ${formatMoney(cost)}`;
}

function formatHoursValue(value: string | number | null) {
  const amount = decimalToNumber(value);
  return amount === null ? "—" : formatHours(amount);
}

function formatCountDeviation(actual: number | null, planned: number, pct: string | number | null) {
  if (actual === null) {
    return "—";
  }
  return `${formatSignedInteger(actual - planned)} (${formatSignedPercent(pct)})`;
}

function formatNumberDeviation(
  actual: string | number | null,
  planned: string | number | null,
  pct: string | number | null,
) {
  const actualValue = decimalToNumber(actual);
  const plannedValue = decimalToNumber(planned);
  if (actualValue === null || plannedValue === null) {
    return "—";
  }
  return `${formatSignedNumber(actualValue - plannedValue)} (${formatSignedPercent(pct)})`;
}

function formatMoneyDeviation(
  actual: string | number | null,
  planned: string | number | null,
  pct: string | number | null,
) {
  const actualValue = decimalToNumber(actual);
  const plannedValue = decimalToNumber(planned);
  if (actualValue === null || plannedValue === null) {
    return "—";
  }
  return `${formatSignedMoney(actualValue - plannedValue)} (${formatSignedPercent(pct)})`;
}

function formatSignedInteger(value: number) {
  if (value === 0) {
    return "0";
  }
  return `${value > 0 ? "+" : ""}${value.toLocaleString("ru-RU", {
    maximumFractionDigits: 0,
  })}`;
}

function formatSignedNumber(value: number) {
  if (value === 0) {
    return "0";
  }
  return `${value > 0 ? "+" : ""}${value.toLocaleString("ru-RU", {
    maximumFractionDigits: 1,
  })}`;
}

function formatSignedMoney(value: number) {
  if (value === 0) {
    return "0 ₽";
  }
  return `${value > 0 ? "+" : ""}${value.toLocaleString("ru-RU", {
    maximumFractionDigits: 0,
  })} ₽`;
}

function formatSignedPercent(value: string | number | null) {
  const amount = decimalToNumber(value);
  if (amount === null) {
    return "—";
  }
  return `${amount > 0 ? "+" : ""}${amount.toLocaleString("ru-RU", {
    maximumFractionDigits: 1,
  })}%`;
}

function formatPpDiff(value: string | number | null) {
  const amount = decimalToNumber(value);
  if (amount === null) {
    return "—";
  }
  return `${amount > 0 ? "+" : ""}${amount.toLocaleString("ru-RU", {
    maximumFractionDigits: 1,
  })} п.п.`;
}

function formatDayWithWeekday(value: string) {
  const date = parseIsoDate(value);
  return `${value.slice(8, 10)}.${value.slice(5, 7)} ${weekdayLabels[date.getDay()]}`;
}

function planFactDayTitle(row: PlanFactDayRowRead, summary: PlanFactSummaryRead) {
  return [
    formatDate(row.business_date),
    `План: ${formatPlanFactTriplet(row.planned_shifts, row.planned_hours, row.planned_cost)}`,
    `Факт: ${formatPlanFactTriplet(row.actual_shifts, row.actual_hours, row.actual_cost, true)}`,
    `Плановая выручка: ${formatMoneyWithCurrency(row.planned_revenue)}`,
    `Фактическая выручка: ${formatMoneyWithCurrency(row.actual_revenue)}`,
    planFactSourceTitle(summary),
  ].join("\n");
}

function planFactEmployeeTitle(row: PlanFactEmployeeRowRead, summary: PlanFactSummaryRead) {
  return [
    `${row.full_name}${row.position ? `, ${row.position}` : ""}`,
    `План: ${formatPlanFactTriplet(row.planned_shifts, row.planned_hours, row.planned_cost)}`,
    `Факт: ${formatPlanFactTriplet(row.actual_shifts, row.actual_hours, row.actual_cost, true)}`,
    planFactSourceTitle(summary),
  ].join("\n");
}

function planFactSourceTitle(summary: PlanFactSummaryRead) {
  const plannedCost = sourceRecord(summary.sources["planned_cost"]);
  const actualCost = Array.isArray(summary.sources["actual_cost"])
    ? summary.sources["actual_cost"]
    : [];
  const lines = [
    plannedCost
      ? `Плановая стоимость: payroll_forecast_run #${shortId(
          plannedCost.forecast_run_id,
        )} от ${formatSourceDateTime(plannedCost.run_at)}${
          plannedCost.run_by_label ? `, автор ${plannedCost.run_by_label}` : ""
        }`
      : "Плановая стоимость: расчёт стоимости не найден",
  ];

  if (actualCost.length > 0) {
    const actualLines = actualCost.slice(0, 5).reduce<string[]>((accumulator, source) => {
      const item = sourceRecord(source);
      if (item) {
        accumulator.push(
          `Фактическая стоимость: payroll_run #${shortId(
            item.run_id,
          )} за ${formatSourceDate(item.period_start)}–${formatSourceDate(item.period_end)}`,
        );
      }
      return accumulator;
    }, []);
    lines.push(...actualLines);
    if (actualCost.length > 5) {
      lines.push(`Ещё payroll_run: ${actualCost.length - 5}`);
    }
  } else {
    lines.push("Фактическая стоимость: payroll_run не найден");
  }

  if (typeof summary.sources["actual_cost_note"] === "string") {
    lines.push(summary.sources["actual_cost_note"]);
  }
  return lines.join("\n");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function sourceRecord(value: unknown): Record<string, unknown> | null {
  return isRecord(value) ? value : null;
}

function shortId(value: unknown) {
  const text = String(value ?? "");
  return text ? text.slice(0, 8) : "—";
}

function formatSourceDate(value: unknown) {
  return typeof value === "string" ? formatDate(value.slice(0, 10)) : "—";
}

function formatSourceDateTime(value: unknown) {
  return typeof value === "string" ? formatDateTime(value) : "—";
}

function indexShiftsByEmployeeDay(shifts: ScheduledShiftRead[]) {
  const index = new Map<string, ScheduledShiftRead>();
  shifts.forEach((shift) => {
    index.set(`${shift.employee_id}:${shift.business_date}`, shift);
  });
  return index;
}

function indexVacationByEmployeeDay(rows: VacationRosterRow[]) {
  const index = new Map<string, VacationPeriodRead>();
  rows.forEach((row) => {
    row.periods
      .filter((period) => period.status !== "cancelled")
      .forEach((period) => {
        eachIsoDate(period.date_start, period.date_end).forEach((day) => {
          index.set(`${row.employee_id}:${day}`, period);
        });
      });
  });
  return index;
}

function indexLedgerByEmployeeDay(entries: ScheduleLedgerEntryRead[]) {
  const index = new Map<string, ScheduleLedgerEntryRead[]>();
  entries.forEach((entry) => {
    const key = `${entry.employee_id}:${entry.business_date}`;
    const current = index.get(key) ?? [];
    current.push(entry);
    index.set(key, current);
  });
  sortLedgerIndex(index);
  return index;
}

function indexLedgerByStationDay(entries: ScheduleLedgerEntryRead[]) {
  const index = new Map<string, ScheduleLedgerEntryRead[]>();
  entries.forEach((entry) => {
    const key = `${stationForLedgerEntry(entry)}:${entry.business_date}`;
    const current = index.get(key) ?? [];
    current.push(entry);
    index.set(key, current);
  });
  sortLedgerIndex(index);
  return index;
}

function indexLedgerByDay(entries: ScheduleLedgerEntryRead[]) {
  const index = new Map<string, ScheduleLedgerEntryRead[]>();
  entries.forEach((entry) => {
    const current = index.get(entry.business_date) ?? [];
    current.push(entry);
    index.set(entry.business_date, current);
  });
  sortLedgerIndex(index);
  return index;
}

function sortLedgerIndex(index: Map<string, ScheduleLedgerEntryRead[]>) {
  index.forEach((entries) =>
    entries.sort(
      (left, right) =>
        left.opened_at.localeCompare(right.opened_at) ||
        left.employee_full_name.localeCompare(right.employee_full_name, "ru"),
    ),
  );
}

function indexCostEstimatesByShift(estimates: ShiftCostEstimateRead[]) {
  const index = new Map<string, ShiftCostEstimateRead>();
  estimates.forEach((estimate) => {
    index.set(estimate.scheduled_shift_id, estimate);
  });
  return index;
}

function indexCashierOverridesByDay(overrides: ShiftAllowanceOverrideRead[]) {
  const index = new Map<string, ShiftAllowanceOverrideRead>();
  overrides.forEach((override) => {
    index.set(override.business_date, override);
  });
  return index;
}

function findCashierAllowanceResolveDays(
  shifts: ScheduledShiftRead[],
  roster: EmployeeRosterRow[],
) {
  const employees = new Map(roster.map((employee) => [employee.id, employee]));
  const days = new Set<string>();
  shifts.forEach((shift) => {
    const employee = employees.get(shift.employee_id);
    if (
      employee?.position === "Кассир" &&
      (employee.allowances.senior || employee.allowances.deputy)
    ) {
      days.add(shift.business_date);
    }
  });
  return [...days].sort();
}

function buildCostSummariesByDay(estimates: ShiftCostEstimateRead[]) {
  const index = new Map<string, CostDaySummary>();
  estimates.forEach((estimate) => {
    const current = index.get(estimate.business_date) ?? {
      total: 0,
      estimateCount: 0,
      warningCount: 0,
      reasons: [],
    };
    current.total += decimalToNumber(estimate.total_cost_estimate) ?? 0;
    current.estimateCount += 1;
    if (estimate.quality_status === "requires_review") {
      current.warningCount += 1;
      estimate.quality_reasons.forEach((reason) => {
        if (!current.reasons.includes(reason)) {
          current.reasons.push(reason);
        }
      });
    }
    index.set(estimate.business_date, current);
  });
  return index;
}

type RevenueBudgetDisplay = {
  amount: number | null;
  label: string;
  source: "fact" | "forecast" | "missing";
};

function indexActualRevenueByDay(rows: PlanFactDayRowRead[]) {
  const index = new Map<string, number>();
  rows.forEach((row) => {
    const amount = decimalToNumber(row.actual_revenue);
    if (amount !== null) {
      index.set(row.business_date, amount);
    }
  });
  return index;
}

function calculateForecastBudgetTotals({
  actualRevenueByDay,
  costByDay,
  days,
  forecastByDay,
  hasCostRun,
  todayIso,
}: {
  actualRevenueByDay: Map<string, number>;
  costByDay: Map<string, CostDaySummary>;
  days: string[];
  forecastByDay: Map<string, RevenueForecastRead>;
  hasCostRun: boolean;
  todayIso: string;
}): ForecastBudgetTotals {
  let revenueSum = 0;
  let revenueCount = 0;
  let costSum = 0;
  days.forEach((day) => {
    const revenue = getForecastBudgetRevenueAmount({
      actualRevenueByDay,
      day,
      forecastByDay,
      todayIso,
    });
    if (revenue !== null) {
      revenueSum += revenue;
      revenueCount += 1;
    }
    if (hasCostRun) {
      costSum += costByDay.get(day)?.total ?? 0;
    }
  });
  const revenueTotal = revenueCount > 0 ? revenueSum : null;
  const costTotal = hasCostRun ? costSum : null;
  const fotPct =
    revenueTotal !== null && revenueTotal > 0 && costTotal !== null
      ? (costTotal / revenueTotal) * 100
      : null;
  return { revenueSum: revenueTotal, costSum: costTotal, fotPct };
}

function getForecastBudgetRevenueAmount({
  actualRevenueByDay,
  day,
  forecastByDay,
  todayIso,
}: {
  actualRevenueByDay: Map<string, number>;
  day: string;
  forecastByDay: Map<string, RevenueForecastRead>;
  todayIso: string;
}) {
  return getForecastBudgetRevenueDisplay({
    actualRevenueByDay,
    day,
    forecast: forecastByDay.get(day),
    todayIso,
  }).amount;
}

function getForecastBudgetRevenueDisplay({
  actualRevenueByDay,
  day,
  forecast,
  todayIso,
}: {
  actualRevenueByDay: Map<string, number>;
  day: string;
  forecast: RevenueForecastRead | undefined;
  todayIso: string;
}): RevenueBudgetDisplay {
  const factAmount = day <= todayIso ? actualRevenueByDay.get(day) : undefined;
  if (factAmount !== undefined) {
    return { amount: factAmount, label: "факт", source: "fact" };
  }
  if (!forecast) {
    return { amount: null, label: "нет прогноза", source: "missing" };
  }
  if (forecast.quality_status === "requires_review" || forecast.forecast_amount === null) {
    return { amount: null, label: "проверить", source: "missing" };
  }
  return {
    amount: decimalToNumber(forecast.forecast_amount),
    label: forecast.quality_status === "manual_override" ? "override" : "план",
    source: "forecast",
  };
}

function revenueBudgetAmountClass(
  display: RevenueBudgetDisplay,
  forecast: RevenueForecastRead | undefined,
) {
  if (display.source === "fact") {
    return "text-emerald-700";
  }
  if (display.source === "missing") {
    return "text-orange-600";
  }
  return forecast ? forecastAmountClass(forecast) : "text-foreground";
}

function revenueBudgetCellTitle(
  day: string,
  forecast: RevenueForecastRead | undefined,
  display: RevenueBudgetDisplay,
) {
  const lines = [formatDate(day)];
  if (display.source === "fact") {
    lines.push(`Факт выручки: ${formatMoneyWithCurrency(display.amount)}`);
    lines.push("Источник: plan-fact / iiko");
  }
  if (!forecast) {
    lines.push("Прогноз выручки не рассчитан");
    return lines.join("\n");
  }
  lines.push(`Прогноз: ${formatMoneyWithCurrency(forecast.forecast_amount)}`);
  lines.push(`Метод: ${forecast.method_code}`);
  lines.push(`Статус: ${forecastStatusText(forecast)}`);
  if (forecast.event_review_recommended) {
    lines.push("Праздничный день, рекомендуем проверить прогноз вручную");
  }
  const history = forecast.history_points.slice(0, 6).map((point) => {
    const suffix = point.included ? "" : " (исключено)";
    return `${formatDate(point.date)}: ${formatMoneyWithCurrency(point.amount)}${suffix}`;
  });
  if (history.length > 0) {
    lines.push("История:");
    lines.push(...history);
  }
  if (forecast.computed_at) {
    lines.push(`Обновлено: ${formatDateTime(forecast.computed_at)}`);
  }
  return lines.join("\n");
}

function costBudgetCellTitle(
  day: string,
  summary: CostDaySummary | undefined,
  run: PayrollForecastRunRead | null,
) {
  const lines = [formatDate(day)];
  if (!run) {
    lines.push("Стоимость ещё не рассчитана");
    return lines.join("\n");
  }
  lines.push(`Расчёт: payroll_forecast_run #${shortId(run.id)} от ${formatDateTime(run.run_at)}`);
  if (run.run_by_label) {
    lines.push(`Автор: ${run.run_by_label}`);
  }
  if (!summary) {
    lines.push("Нет смен или расчёт не содержит данных за день");
    return lines.join("\n");
  }
  lines.push(`Стоимость: ${formatMoneyWithCurrency(summary.total)}`);
  lines.push(`Смен: ${summary.estimateCount}`);
  if (summary.warningCount > 0) {
    lines.push(`Предупреждения: ${summary.warningCount}`);
  }
  if (summary.reasons.length > 0) {
    lines.push(`Причины: ${summary.reasons.map(costReasonLabel).join(", ")}`);
  }
  return lines.join("\n");
}

function fotBudgetCellClass(value: number | null, threshold: number) {
  const level = fotLevelForValue(value, threshold);
  if (level === "warning") {
    return "bg-amber-50";
  }
  if (level === "danger") {
    return "bg-red-50";
  }
  return "";
}

function fotBudgetTitle(
  revenue: number | null,
  cost: number | null,
  value: number | null,
  threshold: number,
) {
  return [
    "ФОТ % = Стоимость / Выручка x 100",
    `Выручка: ${formatMoneyWithCurrency(revenue)}`,
    `Стоимость: ${formatMoneyWithCurrency(cost)}`,
    `ФОТ: ${formatPercent(value)}`,
    `Порог: ${formatPercent(threshold)}`,
  ].join("\n");
}

function fotLevelForValue(value: number | null, threshold: number): FotStatusLevel {
  if (value === null) {
    return "none";
  }
  if (value <= threshold) {
    return "ok";
  }
  if (value <= threshold + 4) {
    return "warning";
  }
  return "danger";
}

function buildStationRows(
  shifts: ScheduledShiftRead[],
  ledgerEntries: ScheduleLedgerEntryRead[] = [],
) {
  const stations = new Map<string, Map<string, ScheduledShiftRead[]>>();
  shifts.forEach((shift) => {
    const station = stationForShift(shift);
    const byDay = stations.get(station) ?? new Map<string, ScheduledShiftRead[]>();
    const dayShifts = byDay.get(shift.business_date) ?? [];
    dayShifts.push(shift);
    byDay.set(shift.business_date, dayShifts);
    stations.set(station, byDay);
  });
  const ledgerStations = new Set(ledgerEntries.map(stationForLedgerEntry));
  const orderedStations = [
    ...stationOrder,
    ...[...new Set([...stations.keys(), ...ledgerStations])]
      .filter((station) => !stationOrder.includes(station))
      .sort(),
  ];
  return orderedStations
    .filter(
      (station) =>
        station !== "(без станции)" || stations.has(station) || ledgerStations.has(station),
    )
    .map((station) => ({
      station,
      byDay: stations.get(station) ?? new Map<string, ScheduledShiftRead[]>(),
    }));
}

function useLocalStorageState<T>(
  key: string,
  defaultValue: T | (() => T),
  isValid: (value: unknown) => value is T,
  options: { hydrateFromStorage?: boolean } = {},
): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => {
    const fallback =
      typeof defaultValue === "function" ? (defaultValue as () => T)() : defaultValue;
    if (options.hydrateFromStorage === false) {
      return fallback;
    }
    return readLocalStorageValue(key, isValid) ?? fallback;
  });

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    window.localStorage.setItem(key, JSON.stringify(value));
  }, [key, value]);

  return [value, setValue];
}

function initialScheduleRange(storedPreset: PeriodPreset | null) {
  if (storedPreset === "custom") {
    return (
      readLocalStorageValue("schedule.range", isPeriodRange) ?? rangeForPreset("month", new Date())!
    );
  }
  return rangeForPreset(storedPreset ?? "month", new Date())!;
}

function readForecastBudgetCollapsedDefault() {
  const legacyValue =
    FORECAST_BUDGET_LEGACY_COLLAPSED_KEYS.map((key) => readLocalStorageValue(key, isBoolean)).find(
      (value) => value !== null,
    ) ?? false;
  if (typeof window !== "undefined") {
    FORECAST_BUDGET_LEGACY_COLLAPSED_KEYS.forEach((key) => {
      window.localStorage.removeItem(key);
    });
  }
  return legacyValue;
}

function readStoredSchedulePreset() {
  return readLocalStorageValue("schedule.preset", isPeriodPreset);
}

function readLocalStorageValue<T>(key: string, isValid: (value: unknown) => value is T): T | null {
  if (typeof window === "undefined") {
    return null;
  }
  const rawValue = window.localStorage.getItem(key);
  if (!rawValue) {
    return null;
  }
  const parsedValue = parseStoredValue(rawValue);
  return isValid(parsedValue) ? parsedValue : null;
}

function parseStoredValue(value: string): unknown {
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

function isPeriodPreset(value: unknown): value is PeriodPreset {
  return value === "week" || value === "2weeks" || value === "month" || value === "custom";
}

function isPeriodRange(value: unknown): value is PeriodRange {
  if (!value || typeof value !== "object") {
    return false;
  }
  const range = value as Partial<PeriodRange>;
  return isIsoDateString(range.from) && isIsoDateString(range.to) && range.from <= range.to;
}

function isBoolean(value: unknown): value is boolean {
  return typeof value === "boolean";
}

function isIsoDateString(value: unknown): value is string {
  return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value);
}

function defaultScheduleDraft(range = rangeForPreset("month", new Date())!): ScheduleCreatePayload {
  return {
    date_start: range.from,
    date_end: range.to,
    notes: "",
  };
}

function compareSchedulesForSelect(left: ScheduleRead, right: ScheduleRead) {
  const statusRank: Record<ScheduleRead["status"], number> = {
    draft: 0,
    published: 1,
    superseded: 2,
  };
  return (
    statusRank[left.status] - statusRank[right.status] ||
    right.date_start.localeCompare(left.date_start)
  );
}

function compareRosterRows(left: EmployeeRosterRow, right: EmployeeRosterRow) {
  return left.full_name.localeCompare(right.full_name, "ru");
}

function stationForShift(shift: ScheduledShiftRead) {
  return shift.station_code || stationForPayrollRole(shift.payroll_role);
}

function stationForLedgerEntry(entry: ScheduleLedgerEntryRead) {
  return normalizeStationDisplay(
    entry.station_code || stationForPayrollRole(entry.payroll_role ?? ""),
  );
}

function stationForPayrollRole(role: string) {
  const map: Record<string, string | null> = {
    administrator: "Касса",
    pizza: "Пицца",
    sushi: "Роллы",
    shawarma: "Горячий цех",
    prep: null,
    Кассир: "Касса",
    Касса: "Касса",
    Сушист: "Роллы",
    Пиццерист: "Пицца",
    Шаурмист: "Горячий цех",
  };
  return map[role] ?? "(без станции)";
}

function normalizeStationDisplay(station: string | null) {
  const value = station || "(без станции)";
  return normalizeStation(value) === "горячий цех" ? "Горячий цех" : value;
}

function defaultStationForEmployee(employee: EmployeeRosterRow | undefined) {
  if (!employee) {
    return null;
  }
  const primary = primaryAvailableRole(employee);
  if (primary) {
    return primary.default_station_code;
  }
  if (employee.position === "Кассир") {
    return "Касса";
  }
  const map: Record<string, string> = {
    pizza: "Пицца",
    sushi: "Роллы",
    shawarma: "Горячий цех",
  };
  return employee.default_cooking_station
    ? (map[employee.default_cooking_station] ?? employee.default_cooking_station)
    : null;
}

function primaryAvailableRole(employee: EmployeeRosterRow | undefined) {
  return employee?.available_roles.find((role) => role.is_primary) ?? employee?.available_roles[0];
}

function roleForShiftOrPrimary(shift: ScheduledShiftRead, employee: EmployeeRosterRow | undefined) {
  if (employee?.available_roles.some((role) => role.payroll_role === shift.payroll_role)) {
    return shift.payroll_role;
  }
  return primaryAvailableRole(employee)?.payroll_role ?? shift.payroll_role ?? null;
}

function defaultRoleForEmployeeAtStation(
  employee: EmployeeRosterRow | undefined,
  stationCode: string | null,
) {
  if (!employee) {
    return null;
  }
  if (!stationCode) {
    return primaryAvailableRole(employee)?.payroll_role ?? null;
  }
  return (
    employee.available_roles.find((role) => stationsMatch(role.default_station_code, stationCode))
      ?.payroll_role ?? null
  );
}

function stationsMatch(left: string | null, right: string | null) {
  return normalizeStation(left) === normalizeStation(right);
}

function normalizeStation(station: string | null) {
  const normalized = (station ?? "").trim().toLocaleLowerCase("ru-RU");
  return normalized === "шаурма" ? "горячий цех" : normalized;
}

function payrollRoleLabel(role: string | null | undefined) {
  if (!role) {
    return "—";
  }
  const labels: Record<string, string> = {
    ...PAYROLL_ROLE_LABELS,
    Касса: "Администратор",
    Сушист: "Сушист",
    Пиццерист: "Пиццерист",
    Шаурмист: "Шаурмист",
  };
  return labels[role] ?? role;
}

function positionRoleLabel(
  position: string | null | undefined,
  payrollRole: string | null | undefined,
) {
  const role = payrollRole
    ? payrollRole
        .split(",")
        .map((item) => payrollRoleLabel(item.trim()))
        .join(", ")
    : "—";
  return role === "—" ? position || "—" : `${position || "—"} · ${role}`;
}

function employeeRoleLine(
  employee: EmployeeRosterRow,
  payrollRole: string | null | undefined,
) {
  const role = rosterRoleForPayrollRole(employee, payrollRole);
  const roleValue = payrollRole ?? role?.payroll_role ?? null;
  const roleLabel = roleValue
    ? roleValue
        .split(",")
        .map((item) => payrollRoleLabel(item.trim()))
        .join(", ")
    : "—";
  const substituteSuffix = role?.is_substitute ? " (подмена)" : "";
  return roleLabel === "—"
    ? employee.position || "—"
    : `${employee.position || "—"} · ${roleLabel}${substituteSuffix}`;
}

function rosterRoleForPayrollRole(
  employee: EmployeeRosterRow,
  payrollRole: string | null | undefined,
) {
  if (!payrollRole) {
    return primaryAvailableRole(employee) ?? null;
  }
  return employee.available_roles.find((role) => role.payroll_role === payrollRole) ?? null;
}

function primaryRoleLabelSource(employee: EmployeeRosterRow) {
  const primary = employee.available_roles.find((role) => role.is_primary);
  if (primary) {
    return primary.payroll_role;
  }
  if (employee.primary_payroll_role) {
    return employee.primary_payroll_role;
  }
  return employee.available_roles
    .slice(0, 2)
    .map((role) => role.payroll_role)
    .join(", ");
}

function shouldShowFact(day: string, ledgerEntries: ScheduleLedgerEntryRead[], today: string) {
  if (ledgerEntries.length === 0) {
    return false;
  }
  return day < today || day === today;
}

function allowanceRoleLabel(role: string | null | undefined) {
  if (role === "senior") {
    return "старший";
  }
  if (role === "deputy_senior") {
    return "зам";
  }
  if (role === "none") {
    return "никто";
  }
  return "—";
}

function allowanceReasonLabel(reason: string | null | undefined) {
  const labels: Record<string, string> = {
    manual_override: "ручной выбор",
    plan_priority: "по плану",
    default_senior: "старший по умолчанию",
    sole_senior: "единственный старший",
    sole_deputy: "единственный зам",
    no_candidate: "нет кандидата",
    manual_override_fallback: "fallback после override",
  };
  return reason ? (labels[reason] ?? reason) : "авто";
}

function buildRangeDays(range: PeriodRange) {
  const start = parseIsoDate(range.from);
  const end = parseIsoDate(range.to);
  const days: string[] = [];
  for (let cursor = start; cursor <= end; cursor = addDays(cursor, 1)) {
    days.push(toIsoDate(cursor));
  }
  return days;
}

function eachIsoDate(dateStart: string, dateEnd: string) {
  const start = parseIsoDate(dateStart);
  const end = parseIsoDate(dateEnd);
  const days: string[] = [];
  for (let cursor = start; cursor <= end; cursor = addDays(cursor, 1)) {
    days.push(toIsoDate(cursor));
  }
  return days;
}

function startOfTuesdayWeek(value: Date) {
  const date = new Date(value);
  date.setHours(0, 0, 0, 0);
  const offset = (date.getDay() + 5) % 7;
  return addDays(date, -offset);
}

function addDays(value: Date, days: number) {
  const date = new Date(value);
  date.setDate(date.getDate() + days);
  return date;
}

function parseIsoDate(value: string) {
  const [year, month, day] = value.split("-").map(Number);
  return new Date(year, month - 1, day);
}

function toIsoDate(value: Date) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function isDateInRange(value: string, range: PeriodRange) {
  return value >= range.from && value <= range.to;
}

function formatDate(value: string) {
  const [year, month, day] = value.split("-");
  return `${day}.${month}.${year}`;
}

function formatRange(start: string, end: string) {
  return `${formatDate(start)} — ${formatDate(end)}`;
}

function formatShortRange(start: string, end: string) {
  return `${formatShortDate(start)} — ${formatShortDate(end)}`;
}

function formatShortDate(value: string) {
  const [, month, day] = value.split("-");
  return `${day}.${month}`;
}

function formatShiftTime(shift: ScheduledShiftRead) {
  return `${timeFromDateTime(shift.planned_start_at)}–${timeFromDateTime(shift.planned_end_at)}`;
}

// Полная смена = рабочий день целиком (10:00–22:00). Для неё время в графике не
// показываем — это значение по умолчанию.
const FULL_SHIFT_START = "10:00";
const FULL_SHIFT_END = "22:00";

function isFullDayShift(shift: ScheduledShiftRead) {
  return (
    timeFromDateTime(shift.planned_start_at) === FULL_SHIFT_START &&
    timeFromDateTime(shift.planned_end_at) === FULL_SHIFT_END
  );
}

function formatLedgerTime(entry: ScheduleLedgerEntryRead) {
  const end = entry.closed_at ? timeFromDateTime(entry.closed_at) : "—";
  return `${timeFromDateTime(entry.opened_at)}–${end}`;
}

function ledgerFactTitle(entry: ScheduleLedgerEntryRead) {
  return [
    `${entry.employee_full_name}: ${formatLedgerTime(entry)}`,
    positionRoleLabel(entry.position, entry.payroll_role),
    entry.station_code,
    `Минут: ${entry.minutes_worked}`,
    entry.is_closed ? null : "Смена не закрыта",
  ]
    .filter(Boolean)
    .join("\n");
}

function timeFromDateTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value.slice(11, 16);
  }
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(
    2,
    "0",
  )}`;
}

function composeDateTime(dateIso: string, time: string) {
  return `${dateIso}T${time}:00${MOSCOW_OFFSET}`;
}

function composeEndDateTime(dateIso: string, startTime: string, endTime: string) {
  const endDate = endTime <= startTime ? toIsoDate(addDays(parseIsoDate(dateIso), 1)) : dateIso;
  return `${endDate}T${endTime}:00${MOSCOW_OFFSET}`;
}

function hoursBetween(dateIso: string, startTime: string, endTime: string) {
  const start = new Date(composeDateTime(dateIso, startTime));
  const end = new Date(composeEndDateTime(dateIso, startTime, endTime));
  return (end.getTime() - start.getTime()) / 3_600_000;
}

function formatHours(value: number) {
  return value.toLocaleString("ru-RU", { maximumFractionDigits: 2 });
}

function forecastAmountClass(forecast: RevenueForecastRead) {
  if (forecast.quality_status === "manual_override") {
    return "text-blue-600";
  }
  if (forecast.quality_status === "requires_review" || forecast.forecast_amount === null) {
    return "text-orange-600";
  }
  return "text-foreground";
}

function forecastStatusText(forecast: RevenueForecastRead) {
  if (forecast.quality_status === "manual_override") {
    return "Ручной override";
  }
  if (forecast.quality_status === "requires_review" || forecast.forecast_amount === null) {
    return "Требует проверки";
  }
  return "Расчётный";
}

function costReasonLabel(reason: string) {
  const labels: Record<string, string> = {
    forecast_missing: "нет прогноза",
    forecast_requires_review: "прогноз проверить",
    no_category: "нет категории",
    no_rate: "нет ставки",
    no_role: "нет роли",
    overnight_shift: "ночная смена",
  };
  return labels[reason] ?? reason;
}

function cashierAllowancePlanFactTitle(row: PlanFactDayRowRead) {
  return [
    `План: ${cashierAllowanceInfoText(row.planned_cashier_allowance)}`,
    `Факт: ${cashierAllowanceInfoText(row.actual_cashier_allowance)}`,
  ].join("\n");
}

function cashierAllowanceInfoText(value: PlanFactDayRowRead["planned_cashier_allowance"]) {
  if (!value) {
    return "нет данных";
  }
  const recipient = value.recipient_role === "none" ? "никто" : (value.recipient_full_name ?? "—");
  return `${recipient} (${allowanceRoleLabel(value.recipient_role)}, ${allowanceReasonLabel(
    value.reason,
  )})`;
}

function allowanceTitleText(
  employee: EmployeeRosterRow,
  assignment: AllowanceAssignmentRead | undefined,
) {
  if (employee.position === "Повар") {
    return "начисляется независимо";
  }
  if (!assignment) {
    return "ожидает расчёта";
  }
  if (assignment.recipient_employee_id === employee.id) {
    return `получает ${allowanceRoleLabel(assignment.recipient_role)}`;
  }
  if (assignment.recipient_role === "none") {
    return "не начисляется";
  }
  return `получает ${assignment.recipient_full_name ?? "другой сотрудник"}`;
}

function fotStatusLevel(run: PayrollForecastRunRead): FotStatusLevel {
  const value = decimalToNumber(run.fot_to_revenue_pct);
  const threshold = decimalToNumber(run.fot_warning_threshold_pct) ?? 28;
  return fotLevelForValue(value, threshold);
}

function fotStatusClass(level: FotStatusLevel) {
  if (level === "ok") {
    return "text-emerald-700";
  }
  if (level === "warning") {
    return "text-amber-700";
  }
  if (level === "danger") {
    return "text-red-700";
  }
  return "text-muted-foreground";
}

function runStatusLabel(status: PayrollForecastRunRead["status"]) {
  const labels: Record<PayrollForecastRunRead["status"], string> = {
    draft: "Черновик",
    completed: "Активный",
    superseded: "Замещён",
  };
  return labels[status];
}

function formatMoney(value: string | number | null) {
  const amount = decimalToNumber(value);
  if (amount === null) {
    return "—";
  }
  return amount.toLocaleString("ru-RU", { maximumFractionDigits: 0 });
}

function formatMoneyWithCurrency(value: string | number | null) {
  const amount = formatMoney(value);
  return amount === "—" ? amount : `${amount} ₽`;
}

function formatPercent(value: string | number | null) {
  const amount = decimalToNumber(value);
  if (amount === null) {
    return "—";
  }
  return `${amount.toLocaleString("ru-RU", {
    maximumFractionDigits: 1,
    minimumFractionDigits: 0,
  })}%`;
}

function decimalToNumber(value: string | number | null) {
  if (value === null) {
    return null;
  }
  const amount =
    typeof value === "number"
      ? value
      : Number(
          String(value)
            .replace(/[\s\u00a0]/g, "")
            .replace(",", "."),
        );
  return Number.isFinite(amount) ? amount : null;
}

function amountInputValue(value: string | number | null) {
  return value === null ? "" : String(value);
}

function parseAmountInput(value: string) {
  const amount = Number(value.replace(/[\s\u00a0]/g, "").replace(",", "."));
  return Number.isFinite(amount) ? amount : null;
}

function formatDateTime(value: string) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function shiftTitle(
  shift: ScheduledShiftRead,
  estimate?: ShiftCostEstimateRead,
  employee?: EmployeeRosterRow,
  allowanceAssignment?: AllowanceAssignmentRead,
) {
  const station = shift.station_code || stationForPayrollRole(shift.payroll_role);
  const allowanceLines =
    employee && (employee.allowances.senior || employee.allowances.deputy)
      ? [
          `Надбавка: ${allowanceTitleText(employee, allowanceAssignment)}`,
          allowanceAssignment
            ? `Причина: ${allowanceReasonLabel(allowanceAssignment.reason)}`
            : null,
        ]
      : [];
  const costLines = estimate
    ? [
        "",
        `Оклад: ${formatMoneyWithCurrency(estimate.base_salary_estimate)}`,
        `Надбавка: ${formatMoneyWithCurrency(estimate.allowance_estimate)}`,
        `Пт/сб: ${formatMoneyWithCurrency(estimate.weekday_premium_estimate)}`,
        `Процент: ${formatMoneyWithCurrency(estimate.revenue_percent_estimate)}`,
        `Итого: ${formatMoneyWithCurrency(estimate.total_cost_estimate)}`,
        `Накопфонд: ${formatMoneyWithCurrency(estimate.fund_accrual_estimate)}`,
        estimate.quality_reasons.length > 0
          ? `Проверить: ${estimate.quality_reasons.map(costReasonLabel).join(", ")}`
          : null,
        breakdownLine(estimate.breakdown, "category", "Категория"),
        breakdownLine(estimate.breakdown, "minutes", "Минут"),
      ]
    : [];
  return [
    `${shift.employee_full_name}: ${formatShiftTime(shift)}`,
    station,
    shift.comment_private,
    ...allowanceLines,
    ...costLines,
  ]
    .filter(Boolean)
    .join("\n");
}

function breakdownLine(breakdown: Record<string, unknown>, key: string, label: string) {
  const value = breakdown[key];
  if (value === null || value === undefined || value === "") {
    return null;
  }
  return `${label}: ${String(value)}`;
}

function rangesOverlap(leftStart: string, leftEnd: string, rightStart: string, rightEnd: string) {
  return leftStart <= rightEnd && leftEnd >= rightStart;
}
