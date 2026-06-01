import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowUpDown,
  BarChart3,
  Calculator,
  CalendarDays,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleSlash,
  Copy,
  ExternalLink,
  History,
  LoaderCircle,
  Lock,
  Percent,
  Pencil,
  Plus,
  RefreshCw,
  Send,
  Users,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
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
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
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
import { Textarea } from "@/components/ui/textarea";
import { EmptyState } from "@/components/ui-app/EmptyState";
import { PageHeader } from "@/components/ui-app/PageHeader";
import {
  apiErrorMessage,
  copyWeek,
  createNewVersion,
  createSchedule,
  deleteCashierAllowanceOverride,
  deleteShift,
  getEmployeesRoster,
  getForecastRange,
  getLatestRun,
  getPlanFact,
  getRun,
  getSchedule,
  listRuns,
  listSchedules,
  overrideForecast,
  publishSchedule,
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
  type PayrollRole,
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
  type ScheduledShiftUpsertPayload,
} from "@/lib/api";
import { PAYROLL_ROLE_LABELS } from "@/lib/i18n/employee";
import { cn } from "@/lib/utils";

type ViewMode = "employees" | "stations" | "planFact";
type ScaleMode = "week" | "month";
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
};

type FocusEditButtonTarget = {
  employeeId: string;
  businessDate: string;
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
  warningCount: number;
  reasons: string[];
};

type FotStatusLevel = "none" | "ok" | "warning" | "danger";

const NO_VALUE = "__none";
const DAY_CELL_WIDTH = 128;
const EMPLOYEE_COLUMN_WIDTH = 230;
const STATION_COLUMN_WIDTH = 170;
const MOSCOW_OFFSET = "+03:00";
const stationOptions = ["Пицца", "Роллы", "Горячий цех", "Касса", "Шаурма"];
const stationOrder = [...stationOptions, "(без станции)"];
const weekdayLabels = ["Вс", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"];

const statusLabels: Record<ScheduleRead["status"], string> = {
  draft: "Черновик",
  published: "Опубликован",
  superseded: "Замещён",
};

export function ScheduleRoute() {
  const queryClient = useQueryClient();
  const initialWeekStart = useMemo(() => startOfTuesdayWeek(new Date()), []);
  const [anchorDate, setAnchorDate] = useState(() => toIsoDate(initialWeekStart));
  const [viewMode, setViewMode] = useState<ViewMode>("employees");
  const [scaleMode, setScaleMode] = useState<ScaleMode>("week");
  const [planFactTableMode, setPlanFactTableMode] = useState<PlanFactTableMode>("days");
  const [selectedScheduleId, setSelectedScheduleId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [createDraft, setCreateDraft] = useState<ScheduleCreatePayload>(() =>
    defaultScheduleDraft(),
  );
  const [shiftDialog, setShiftDialog] = useState<ShiftDialogState | null>(null);
  const [focusEditButton, setFocusEditButton] = useState<FocusEditButtonTarget | null>(null);
  const [forecastDialog, setForecastDialog] = useState<ForecastDialogState | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ScheduledShiftRead | null>(null);
  const [publishOpen, setPublishOpen] = useState(false);
  const [newVersionOpen, setNewVersionOpen] = useState(false);
  const [forceRefreshIiko, setForceRefreshIiko] = useState(false);
  const [forceRefreshConfirmOpen, setForceRefreshConfirmOpen] = useState(false);
  const [selectedCostRunId, setSelectedCostRunId] = useState<string | null>(null);
  const [costHistoryOpen, setCostHistoryOpen] = useState(false);
  const warmedScheduleIds = useRef(new Set<string>());
  const [copyDialog, setCopyDialog] = useState<CopyWeekState>(() => ({
    open: false,
    targetMode: "next",
    customDate: toIsoDate(addDays(initialWeekStart, 7)),
  }));

  const visibleDays = useMemo(
    () => (scaleMode === "week" ? buildWeekDays(anchorDate) : buildMonthDays(anchorDate)),
    [anchorDate, scaleMode],
  );
  const forecastRange = useMemo(
    () => ({
      from: visibleDays[0],
      to: visibleDays[visibleDays.length - 1],
    }),
    [visibleDays],
  );
  const scheduleWindow = useMemo(() => {
    const anchor = parseIsoDate(anchorDate);
    return {
      from: toIsoDate(addDays(anchor, -84)),
      to: toIsoDate(addDays(anchor, 84)),
    };
  }, [anchorDate]);

  const schedulesQuery = useQuery({
    queryKey: ["schedules", scheduleWindow.from, scheduleWindow.to],
    queryFn: () =>
      listSchedules({
        date_from: scheduleWindow.from,
        date_to: scheduleWindow.to,
      }),
  });
  const rosterQuery = useQuery({
    queryKey: ["schedule-employees-roster"],
    queryFn: getEmployeesRoster,
  });
  const scheduleQuery = useQuery({
    queryKey: ["schedule", selectedScheduleId],
    queryFn: () => getSchedule(selectedScheduleId ?? ""),
    enabled: Boolean(selectedScheduleId),
  });
  const forecastQuery = useQuery({
    queryKey: ["forecast", forecastRange.from, forecastRange.to],
    queryFn: () =>
      getForecastRange({
        date_from: forecastRange.from,
        date_to: forecastRange.to,
      }),
    enabled: Boolean(selectedScheduleId),
  });
  const latestCostQuery = useQuery({
    queryKey: ["cost-forecast", selectedScheduleId, "latest"],
    queryFn: () => getLatestRun(selectedScheduleId ?? ""),
    enabled: Boolean(selectedScheduleId),
  });
  const selectedCostQuery = useQuery({
    queryKey: ["cost-forecast", selectedScheduleId, selectedCostRunId],
    queryFn: () => getRun(selectedScheduleId ?? "", selectedCostRunId ?? ""),
    enabled: Boolean(selectedScheduleId && selectedCostRunId),
  });
  const costRunsQuery = useQuery({
    queryKey: ["cost-forecast", selectedScheduleId, "runs"],
    queryFn: () => listRuns(selectedScheduleId ?? ""),
    enabled: Boolean(selectedScheduleId && costHistoryOpen),
  });
  const planFactQuery = useQuery({
    queryKey: ["plan-fact", selectedScheduleId],
    queryFn: () => getPlanFact(selectedScheduleId ?? ""),
    enabled: Boolean(selectedScheduleId && viewMode === "planFact"),
  });

  const schedules = useMemo(
    () => [...(schedulesQuery.data ?? [])].sort(compareSchedulesForSelect),
    [schedulesQuery.data],
  );
  const roster = useMemo(
    () => [...(rosterQuery.data ?? [])].sort(compareRosterRows),
    [rosterQuery.data],
  );
  const currentSchedule = scheduleQuery.data ?? null;
  const cashierOverridesQuery = useQuery({
    queryKey: ["cashier-allowance-overrides", selectedScheduleId],
    queryFn: () => listCashierAllowanceOverrides(selectedScheduleId ?? ""),
    enabled: Boolean(selectedScheduleId),
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
      enabled: Boolean(selectedScheduleId),
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
    ? selectedCostQuery.data ?? null
    : latestCostQuery.data ?? null;
  const costEstimatesByShiftId = useMemo(
    () => indexCostEstimatesByShift(displayedCostRun?.estimates ?? []),
    [displayedCostRun?.estimates],
  );
  const isDraft = currentSchedule?.status === "draft";
  const isLocked = currentSchedule != null && !isDraft;
  const selectedWeekStart = toIsoDate(startOfTuesdayWeek(parseIsoDate(anchorDate)));
  const selectedWeekEnd = toIsoDate(addDays(parseIsoDate(selectedWeekStart), 6));
  const leadingWidth = viewMode === "stations" ? STATION_COLUMN_WIDTH : EMPLOYEE_COLUMN_WIDTH;
  const shiftDialogAllowanceAssignment = shiftDialog
    ? cashierAllowanceByDay.get(shiftDialog.businessDate)
    : undefined;
  const shiftDialogAllowanceQuery = shiftDialog
    ? cashierAllowanceResolveQueries[
        cashierAllowanceResolveDays.indexOf(shiftDialog.businessDate)
      ]
    : undefined;
  const shiftDialogAllowanceLoading =
    Boolean(shiftDialogAllowanceQuery?.isLoading) || cashierOverridesQuery.isLoading;

  useEffect(() => {
    if (!schedulesQuery.data) {
      return;
    }
    if (selectedScheduleId && schedules.some((schedule) => schedule.id === selectedScheduleId)) {
      return;
    }
    setSelectedScheduleId(pickDefaultSchedule(schedules, visibleDays)?.id ?? null);
  }, [schedules, schedulesQuery.data, selectedScheduleId, visibleDays]);

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
    onSuccess: async () => {
      toast.success("Смена сохранена");
      setShiftDialog(null);
      await invalidateCurrentSchedule();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить смену")),
  });

  const saveCashierAllowanceOverrideMutation = useMutation({
    mutationFn: (variables: {
      scheduleId: string;
      payload: CashierAllowanceOverridePayload;
      overrideId?: string | null;
    }) =>
      upsertCashierAllowanceOverride(
        variables.scheduleId,
        variables.payload,
        variables.overrideId,
      ),
    onSuccess: async () => {
      toast.success("Выбор надбавки сохранён");
      await invalidateCashierAllowance();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить выбор надбавки")),
  });

  const removeCashierAllowanceOverrideMutation = useMutation({
    mutationFn: (variables: { scheduleId: string; overrideId: string }) =>
      deleteCashierAllowanceOverride(variables.scheduleId, variables.overrideId),
    onSuccess: async () => {
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
      await invalidateCashierAllowance();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось снять выбор надбавки")),
  });

  const quickCreateShiftMutation = useMutation({
    mutationFn: (variables: { scheduleId: string; employeeId: string; businessDate: string }) =>
      upsertShift(variables.scheduleId, {
        employee_id: variables.employeeId,
        business_date: variables.businessDate,
      }),
    onSuccess: async (_shift, variables) => {
      toast.success("Смена добавлена");
      setFocusEditButton({
        employeeId: variables.employeeId,
        businessDate: variables.businessDate,
      });
      await invalidateCurrentSchedule();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось добавить смену")),
  });

  const deleteShiftMutation = useMutation({
    mutationFn: (shift: ScheduledShiftRead) =>
      deleteShift(currentSchedule?.id ?? "", shift.id),
    onSuccess: async () => {
      toast.success("Смена удалена");
      setDeleteTarget(null);
      setShiftDialog(null);
      await invalidateCurrentSchedule();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось удалить смену")),
  });

  const publishMutation = useMutation({
    mutationFn: () => publishSchedule(currentSchedule?.id ?? ""),
    onSuccess: async (schedule) => {
      toast.success("График опубликован");
      setPublishOpen(false);
      setSelectedScheduleId(schedule.id);
      await queryClient.invalidateQueries({ queryKey: ["schedules"] });
      await queryClient.invalidateQueries({ queryKey: ["schedule"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось опубликовать график")),
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
    if (!currentSchedule || warmedScheduleIds.current.has(currentSchedule.id)) {
      return;
    }
    warmedScheduleIds.current.add(currentSchedule.id);
    warmForecastMutation.mutate({
      date_from: currentSchedule.date_start,
      date_to: currentSchedule.date_end,
      force_refresh_iiko: false,
    });
  }, [currentSchedule, warmForecastMutation]);

  useEffect(() => {
    setSelectedCostRunId(null);
    setCostHistoryOpen(false);
  }, [selectedScheduleId]);

  useEffect(() => {
    if (!focusEditButton) {
      return;
    }
    const button = document.querySelector<HTMLButtonElement>(
      `button[data-employee-id="${focusEditButton.employeeId}"][data-business-date="${focusEditButton.businessDate}"]`,
    );
    if (!button) {
      return;
    }
    button.focus();
    setFocusEditButton(null);
  }, [currentSchedule?.shifts, focusEditButton]);

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

  function openCreateDialog() {
    setCreateDraft(defaultScheduleDraft());
    setCreateOpen(true);
  }

  function openShiftDialog(options: {
    employeeId?: string;
    businessDate: string;
    stationCode?: string | null;
    shift?: ScheduledShiftRead;
  }) {
    if (!currentSchedule || isLocked) {
      return;
    }
    const shift = options.shift ?? null;
    const employee = roster.find(
      (item) => item.id === (shift?.employee_id ?? options.employeeId),
    );
    const fallbackStation =
      options.stationCode !== undefined
        ? options.stationCode
        : shift
          ? shift.station_code
          : defaultStationForEmployee(employee);
    const fallbackRole = shift
      ? roleForShiftOrPrimary(shift, employee)
      : defaultRoleForEmployeeAtStation(employee, fallbackStation) ??
        employee?.primary_payroll_role ??
        null;
    const existingOverride = cashierOverridesByDay.get(shift?.business_date ?? options.businessDate);
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
    });
  }

  function handleEmployeeEmptyCellClick(employee: EmployeeRosterRow, businessDate: string) {
    if (
      !currentSchedule ||
      isLocked ||
      quickCreateShiftMutation.isPending ||
      deleteShiftMutation.isPending
    ) {
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
      quickCreateShiftMutation.isPending ||
      deleteShiftMutation.isPending
    ) {
      return;
    }
    deleteShiftMutation.mutate(shift);
  }

  function submitShiftDialog(allowNoneConfirmed = false) {
    if (!currentSchedule || !shiftDialog || !shiftDialog.employeeId) {
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
    const payrollRole =
      shiftDialog.payrollRole ??
      defaultRoleForEmployeeAtStation(employee, shiftDialog.stationCode) ??
      employee?.primary_payroll_role ??
      null;
    saveShiftMutation.mutate({
      scheduleId: currentSchedule.id,
      payload: {
        business_date: shiftDialog.businessDate,
        employee_id: shiftDialog.employeeId,
        payroll_role: payrollRole,
        station_code: shiftDialog.stationCode,
        planned_start_at: composeDateTime(
          shiftDialog.businessDate,
          shiftDialog.startTime,
        ),
        planned_end_at: composeEndDateTime(
          shiftDialog.businessDate,
          shiftDialog.startTime,
          shiftDialog.endTime,
        ),
        comment_private: shiftDialog.comment.trim() || null,
      },
    });
    submitCashierAllowanceOverride(shiftDialog);
  }

  function submitCashierAllowanceOverride(state: ShiftDialogState) {
    if (!currentSchedule || !state.allowanceDirty || state.mode !== "edit") {
      return;
    }
    if (state.allowanceSelection === "auto") {
      if (state.allowanceOverrideId) {
        removeCashierAllowanceOverrideMutation.mutate({
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
    saveCashierAllowanceOverrideMutation.mutate({
      scheduleId: currentSchedule.id,
      payload,
      overrideId: state.allowanceOverrideId,
    });
  }

  function submitCopyWeek() {
    const toDate =
      copyDialog.targetMode === "next"
        ? toIsoDate(addDays(parseIsoDate(selectedWeekStart), 7))
        : copyDialog.customDate;
    copyWeekMutation.mutate(toDate);
  }

  function requestForecastRecompute() {
    if (forceRefreshIiko) {
      setForceRefreshConfirmOpen(true);
      return;
    }
    runForecastRecompute(false);
  }

  function runForecastRecompute(forceRefresh: boolean) {
    recomputeForecastMutation.mutate({
      date_from: forecastRange.from,
      date_to: forecastRange.to,
      force_refresh_iiko: forceRefresh,
    });
  }

  function openForecastDialog(forecast: RevenueForecastRead) {
    setForecastDialog({
      forecast,
      amount: amountInputValue(forecast.manual_override_amount),
      reason: forecast.manual_override_reason ?? "",
      removeConfirmOpen: false,
    });
  }

  function submitForecastOverride() {
    if (!forecastDialog) {
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

  function movePeriod(days: number) {
    setAnchorDate((current) => toIsoDate(addDays(parseIsoDate(current), days)));
  }

  const shiftByEmployeeDay = useMemo(
    () => indexShiftsByEmployeeDay(currentSchedule?.shifts ?? []),
    [currentSchedule?.shifts],
  );
  const stationRows = useMemo(
    () => buildStationRows(currentSchedule?.shifts ?? []),
    [currentSchedule?.shifts],
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

  return (
    <div className="space-y-5">
      <PageHeader
        title="График сотрудников"
        action={
          <>
            <Button onClick={openCreateDialog} variant="outline">
              <Plus size={16} aria-hidden="true" />
              Создать график
            </Button>
            <Button
              disabled={!currentSchedule || currentSchedule.status !== "published"}
              onClick={() => setNewVersionOpen(true)}
              variant="outline"
            >
              <Copy size={16} aria-hidden="true" />
              Новая версия
            </Button>
            {isDraft ? (
              <Button
                disabled={publishMutation.isPending}
                onClick={() => setPublishOpen(true)}
              >
                {publishMutation.isPending ? (
                  <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                ) : (
                  <Send size={16} aria-hidden="true" />
                )}
                Опубликовать
              </Button>
            ) : null}
          </>
        }
      />

      <section className="flex flex-col gap-4 rounded-lg border bg-card p-4">
        <div className="grid gap-3 xl:grid-cols-[minmax(260px,1fr)_auto] xl:items-end">
          <div className="grid gap-2 sm:max-w-[520px]">
            <Label>Период</Label>
            <Select
              disabled={schedules.length === 0}
              onValueChange={(value) => setSelectedScheduleId(value)}
              value={selectedScheduleId ?? undefined}
            >
              <SelectTrigger>
                <SelectValue placeholder="Выберите график" />
              </SelectTrigger>
              <SelectContent>
                {schedules.map((schedule) => (
                  <SelectItem key={schedule.id} value={schedule.id}>
                    {formatDate(schedule.date_start)} — {formatDate(schedule.date_end)} ·{" "}
                    {statusLabels[schedule.status]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {currentSchedule ? (
              <>
                <Badge variant={isDraft ? "outline" : "default"}>
                  {statusLabels[currentSchedule.status]}
                </Badge>
                {isLocked ? (
                  <Badge className="gap-1" variant="outline">
                    <Lock size={13} aria-hidden="true" />
                    Только просмотр
                  </Badge>
                ) : null}
                <span className="text-sm text-muted-foreground">
                  {currentSchedule.shifts.length} смен
                </span>
              </>
            ) : null}
          </div>
        </div>

        <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
          <div className="flex flex-wrap items-center gap-2">
            <SegmentedButton
              active={viewMode === "employees"}
              icon={<Users size={16} aria-hidden="true" />}
              label="По сотрудникам"
              onClick={() => setViewMode("employees")}
            />
            <SegmentedButton
              active={viewMode === "stations"}
              icon={<CalendarDays size={16} aria-hidden="true" />}
              label="По станциям"
              onClick={() => setViewMode("stations")}
            />
            <SegmentedButton
              active={viewMode === "planFact"}
              icon={<BarChart3 size={16} aria-hidden="true" />}
              label="План-факт"
              onClick={() => setViewMode("planFact")}
            />
            <div className="mx-1 h-6 w-px bg-border" />
            <SegmentedButton
              active={scaleMode === "week"}
              label="Неделя"
              onClick={() => setScaleMode("week")}
            />
            <SegmentedButton
              active={scaleMode === "month"}
              label="Месяц"
              onClick={() => setScaleMode("month")}
            />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              aria-label="Предыдущий период"
              onClick={() => movePeriod(scaleMode === "week" ? -7 : -30)}
              size="icon"
              variant="outline"
            >
              <ChevronLeft size={16} aria-hidden="true" />
            </Button>
            <div className="min-w-[178px] text-center text-sm font-medium">
              {formatRange(visibleDays[0], visibleDays[visibleDays.length - 1])}
            </div>
            <Button
              aria-label="Следующий период"
              onClick={() => movePeriod(scaleMode === "week" ? 7 : 30)}
              size="icon"
              variant="outline"
            >
              <ChevronRight size={16} aria-hidden="true" />
            </Button>
            <Button
              disabled={!isDraft || copyWeekMutation.isPending}
              onClick={() =>
                setCopyDialog({
                  open: true,
                  targetMode: "next",
                  customDate: toIsoDate(addDays(parseIsoDate(selectedWeekStart), 7)),
                })
              }
              variant="outline"
            >
              <Copy size={16} aria-hidden="true" />
              Копировать неделю
            </Button>
          </div>
        </div>
      </section>

      {currentSchedule && viewMode === "planFact" ? (
        <Accordion>
          <AccordionItem value="schedule-inputs">
            <AccordionTrigger>
              <span>Прогноз и стоимость</span>
              <span className="text-xs font-normal text-muted-foreground">
                {displayedCostRun ? `расчёт от ${formatDateTime(displayedCostRun.run_at)}` : "свернуть/развернуть"}
              </span>
            </AccordionTrigger>
            <AccordionContent className="grid gap-4">
              <RevenueForecastPanel
                days={visibleDays}
                forecasts={forecastQuery.data ?? []}
                forceRefreshIiko={forceRefreshIiko}
                isLoading={forecastQuery.isLoading || warmForecastMutation.isPending}
                isRecomputing={recomputeForecastMutation.isPending}
                leadingWidth={EMPLOYEE_COLUMN_WIDTH}
                onCellClick={openForecastDialog}
                onForceRefreshChange={setForceRefreshIiko}
                onRecompute={requestForecastRecompute}
              />
              <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
                <CostForecastPanel
                  days={visibleDays}
                  isLoading={
                    latestCostQuery.isLoading ||
                    (selectedCostRunId ? selectedCostQuery.isLoading : false)
                  }
                  isRecomputing={runCostForecastMutation.isPending}
                  leadingWidth={EMPLOYEE_COLUMN_WIDTH}
                  onOpenHistory={() => setCostHistoryOpen(true)}
                  onRecompute={() => runCostForecastMutation.mutate()}
                  run={displayedCostRun}
                />
                <BudgetSummaryPanel
                  isRecomputing={runCostForecastMutation.isPending}
                  onOpenHistory={() => setCostHistoryOpen(true)}
                  onRecompute={() => runCostForecastMutation.mutate()}
                  run={displayedCostRun}
                />
              </div>
            </AccordionContent>
          </AccordionItem>
        </Accordion>
      ) : currentSchedule ? (
        <>
          <RevenueForecastPanel
            days={visibleDays}
            forecasts={forecastQuery.data ?? []}
            forceRefreshIiko={forceRefreshIiko}
            isLoading={forecastQuery.isLoading || warmForecastMutation.isPending}
            isRecomputing={recomputeForecastMutation.isPending}
            leadingWidth={leadingWidth}
            onCellClick={openForecastDialog}
            onForceRefreshChange={setForceRefreshIiko}
            onRecompute={requestForecastRecompute}
          />
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
            <CostForecastPanel
              days={visibleDays}
              isLoading={
                latestCostQuery.isLoading ||
                (selectedCostRunId ? selectedCostQuery.isLoading : false)
              }
              isRecomputing={runCostForecastMutation.isPending}
              leadingWidth={leadingWidth}
              onOpenHistory={() => setCostHistoryOpen(true)}
              onRecompute={() => runCostForecastMutation.mutate()}
              run={displayedCostRun}
            />
            <BudgetSummaryPanel
              isRecomputing={runCostForecastMutation.isPending}
              onOpenHistory={() => setCostHistoryOpen(true)}
              onRecompute={() => runCostForecastMutation.mutate()}
              run={displayedCostRun}
            />
          </div>
        </>
      ) : null}

      {!currentSchedule && !schedulesQuery.isLoading ? (
        <EmptyState
          action={<Button onClick={openCreateDialog}>Создать новый график</Button>}
          icon={<CalendarDays className="h-5 w-5" aria-hidden="true" />}
          title="Графика на этот период ещё нет. Создайте новый."
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
          roster={roster}
          shiftByEmployeeDay={shiftByEmployeeDay}
        />
      ) : (
        <StationScheduleGrid
          days={visibleDays}
          isLoading={scheduleQuery.isLoading}
          isLocked={isLocked}
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
        />
      )}

      <CreateScheduleDialog
        draft={createDraft}
        isSaving={createMutation.isPending}
        onChange={setCreateDraft}
        onOpenChange={setCreateOpen}
        onSubmit={() => createMutation.mutate(createDraft)}
        open={createOpen}
      />

      <ShiftDialog
        allowanceAssignment={shiftDialogAllowanceAssignment}
        employees={roster}
        isAllowanceLoading={shiftDialogAllowanceLoading}
        isRemovingAllowance={removeCashierAllowanceOverrideMutation.isPending}
        isSaving={saveShiftMutation.isPending || saveCashierAllowanceOverrideMutation.isPending}
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
            <AlertDialogCancel disabled={deleteShiftMutation.isPending}>Отмена</AlertDialogCancel>
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
            <AlertDialogCancel disabled={newVersionMutation.isPending}>Отмена</AlertDialogCancel>
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

      <AlertDialog open={forceRefreshConfirmOpen} onOpenChange={setForceRefreshConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Принудительно перечитать выручку из iiko?</AlertDialogTitle>
            <AlertDialogDescription>
              Может занять до минуты.
            </AlertDialogDescription>
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
    </div>
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
              <a href="/payroll/runs">
                <ExternalLink size={16} aria-hidden="true" />
                К расчётам payroll
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
              Факт доступен за {summary.covered_dates.length} из{" "}
              {planFactTotalDays(summary)} дней. Остальные дни ожидают payroll-расчёт.
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
        <CheckCircle2 size={15} aria-hidden="true" />
        в норме
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
            <SortableTh active={sort.key === "date"} direction={sort.direction} onClick={() => onSort("date")}>
              Дата
            </SortableTh>
            <th className="px-3 py-3 font-medium">План: смен/час/руб</th>
            <th className="px-3 py-3 font-medium">Факт: смен/час/руб</th>
            <SortableTh active={sort.key === "hours"} direction={sort.direction} onClick={() => onSort("hours")}>
              Δ часы
            </SortableTh>
            <SortableTh active={sort.key === "cost"} direction={sort.direction} onClick={() => onSort("cost")}>
              Δ стоим.
            </SortableTh>
            <th className="px-3 py-3 font-medium">Надбавка админа</th>
            <SortableTh active={sort.key === "status"} direction={sort.direction} onClick={() => onSort("status")}>
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
                {formatPlanFactTriplet(
                  row.actual_shifts,
                  row.actual_hours,
                  row.actual_cost,
                  true,
                )}
              </td>
              <td className={cn("px-3 py-3 tabular-nums", deviationTextClass(row.hours_deviation_pct, threshold))}>
                {formatPercent(row.hours_deviation_pct)}
              </td>
              <td className={cn("px-3 py-3 tabular-nums", deviationTextClass(row.cost_deviation_pct, threshold))}>
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
            <SortableTh active={sort.key === "name"} direction={sort.direction} onClick={() => onSort("name")}>
              ФИО
            </SortableTh>
            <th className="px-3 py-3 font-medium">Должн.</th>
            <th className="px-3 py-3 font-medium">План: смен/час/руб</th>
            <th className="px-3 py-3 font-medium">Факт: смен/час/руб</th>
            <SortableTh active={sort.key === "hours"} direction={sort.direction} onClick={() => onSort("hours")}>
              Δ часы
            </SortableTh>
            <SortableTh active={sort.key === "cost"} direction={sort.direction} onClick={() => onSort("cost")}>
              Δ стоим.
            </SortableTh>
            <SortableTh active={sort.key === "status"} direction={sort.direction} onClick={() => onSort("status")}>
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
                {formatPlanFactTriplet(
                  row.actual_shifts,
                  row.actual_hours,
                  row.actual_cost,
                  true,
                )}
              </td>
              <td className={cn("px-3 py-3 tabular-nums", deviationTextClass(row.hours_deviation_pct, threshold))}>
                {formatPercent(row.hours_deviation_pct)}
              </td>
              <td className={cn("px-3 py-3 tabular-nums", deviationTextClass(row.cost_deviation_pct, threshold))}>
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
        <ArrowUpDown className={cn("h-3.5 w-3.5", active ? "opacity-100" : "opacity-50")} aria-hidden="true" />
        {active ? <span className="sr-only">{direction === "asc" ? "по возрастанию" : "по убыванию"}</span> : null}
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

function RevenueForecastPanel({
  days,
  forecasts,
  forceRefreshIiko,
  isLoading,
  isRecomputing,
  leadingWidth,
  onCellClick,
  onForceRefreshChange,
  onRecompute,
}: {
  days: string[];
  forecasts: RevenueForecastRead[];
  forceRefreshIiko: boolean;
  isLoading: boolean;
  isRecomputing: boolean;
  leadingWidth: number;
  onCellClick: (forecast: RevenueForecastRead) => void;
  onForceRefreshChange: (checked: boolean) => void;
  onRecompute: () => void;
}) {
  const minWidth = leadingWidth + days.length * DAY_CELL_WIDTH;
  const forecastByDay = useMemo(
    () => new Map(forecasts.map((forecast) => [forecast.business_date, forecast])),
    [forecasts],
  );
  const isEmpty = !isLoading && forecasts.length === 0;

  return (
    <section className="overflow-hidden rounded-lg border bg-card">
      <div className="overflow-x-auto">
        <table className="border-separate border-spacing-0 text-sm" style={{ minWidth }}>
          <tbody>
            <tr>
              <td
                className="sticky left-0 z-20 border-b border-r bg-card px-3 py-3 align-top"
                rowSpan={2}
                style={{ width: leadingWidth, minWidth: leadingWidth }}
              >
                <div className="grid gap-3">
                  <div>
                    <div className="font-medium">Прогноз выручки</div>
                    <label className="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
                      <input
                        checked={forceRefreshIiko}
                        className="h-4 w-4 rounded border-border"
                        onChange={(event) => onForceRefreshChange(event.target.checked)}
                        type="checkbox"
                      />
                      Force-refresh iiko
                    </label>
                  </div>
                  <Button
                    disabled={isRecomputing}
                    onClick={onRecompute}
                    size="sm"
                    type="button"
                    variant={isEmpty ? "default" : "outline"}
                  >
                    {isRecomputing ? (
                      <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
                    ) : (
                      <RefreshCw size={15} aria-hidden="true" />
                    )}
                    {isEmpty ? "Рассчитать прогноз" : "Пересчитать"}
                  </Button>
                </div>
              </td>
              {days.map((day) => (
                <td
                  className="border-b border-r bg-muted/70 px-2 py-2 text-center font-medium text-muted-foreground"
                  key={day}
                  style={{ width: DAY_CELL_WIDTH, minWidth: DAY_CELL_WIDTH }}
                >
                  <div>{weekdayLabels[parseIsoDate(day).getDay()]}</div>
                  <div className="text-base text-foreground">{day.slice(8, 10)}</div>
                </td>
              ))}
            </tr>
            <tr>
              {isLoading ? (
                days.map((day) => (
                  <td className="border-b border-r p-3" key={day}>
                    <Skeleton className="h-12 w-full" />
                  </td>
                ))
              ) : isEmpty ? (
                <td className="border-b border-r px-3 py-4 text-center" colSpan={days.length}>
                  <Button disabled={isRecomputing} onClick={onRecompute} type="button">
                    {isRecomputing ? (
                      <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                    ) : (
                      <RefreshCw size={16} aria-hidden="true" />
                    )}
                    Рассчитать прогноз
                  </Button>
                </td>
              ) : (
                days.map((day) => (
                  <ForecastCell
                    forecast={forecastByDay.get(day)}
                    key={day}
                    onClick={onCellClick}
                  />
                ))
              )}
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  );
}

function ForecastCell({
  forecast,
  onClick,
}: {
  forecast: RevenueForecastRead | undefined;
  onClick: (forecast: RevenueForecastRead) => void;
}) {
  if (!forecast) {
    return (
      <td
        className="h-[76px] border-b border-r px-2 py-3 text-center"
        style={{ width: DAY_CELL_WIDTH, minWidth: DAY_CELL_WIDTH }}
      >
        <div className="text-lg font-semibold tabular-nums text-muted-foreground">—</div>
      </td>
    );
  }

  return (
    <td
      className="h-[76px] border-b border-r p-1.5 text-center"
      style={{ width: DAY_CELL_WIDTH, minWidth: DAY_CELL_WIDTH }}
    >
      <button
        className="flex h-full w-full flex-col items-center justify-center rounded-md px-1.5 hover:bg-primary/5 focus:outline-none focus:ring-2 focus:ring-ring"
        onClick={() => onClick(forecast)}
        type="button"
      >
        <div className={cn("text-lg font-semibold tabular-nums", forecastAmountClass(forecast))}>
          {forecast.forecast_amount === null ? "—" : formatMoney(forecast.forecast_amount)}
        </div>
        <div className="mt-1 flex min-h-5 items-center justify-center gap-1 text-[11px] leading-4">
          {forecast.event_review_recommended ? (
            <span
              aria-label="Праздничный день"
              title="Праздничный день, рекомендуем проверить прогноз вручную"
            >
              <AlertTriangle className="h-3.5 w-3.5 text-orange-600" aria-hidden="true" />
            </span>
          ) : null}
          {forecastStatusLabel(forecast) ? (
            <span className={forecastAmountClass(forecast)}>
              {forecastStatusLabel(forecast)}
            </span>
          ) : null}
        </div>
      </button>
    </td>
  );
}

function CostForecastPanel({
  days,
  isLoading,
  isRecomputing,
  leadingWidth,
  onOpenHistory,
  onRecompute,
  run,
}: {
  days: string[];
  isLoading: boolean;
  isRecomputing: boolean;
  leadingWidth: number;
  onOpenHistory: () => void;
  onRecompute: () => void;
  run: PayrollForecastRunRead | null;
}) {
  const minWidth = leadingWidth + days.length * DAY_CELL_WIDTH;
  const costByDay = useMemo(
    () => buildCostSummariesByDay(run?.estimates ?? []),
    [run?.estimates],
  );
  const isEmpty = !isLoading && !run;

  return (
    <section className="overflow-hidden rounded-lg border bg-card">
      <div className="overflow-x-auto">
        <table className="border-separate border-spacing-0 text-sm" style={{ minWidth }}>
          <tbody>
            <tr>
              <td
                className="sticky left-0 z-20 border-b border-r bg-card px-3 py-3 align-top"
                rowSpan={2}
                style={{ width: leadingWidth, minWidth: leadingWidth }}
              >
                <div className="grid gap-3">
                  <div>
                    <div className="font-medium">Стоимость графика</div>
                    <div className="mt-1 text-xs text-muted-foreground">
                      {run ? `Версия от ${formatDateTime(run.run_at)}` : "Стоимость ещё не рассчитана"}
                    </div>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <Button
                      disabled={isRecomputing}
                      onClick={onRecompute}
                      size="sm"
                      type="button"
                      variant={isEmpty ? "default" : "outline"}
                    >
                      {isRecomputing ? (
                        <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
                      ) : (
                        <Calculator size={15} aria-hidden="true" />
                      )}
                      {isRecomputing ? "Идёт расчёт..." : isEmpty ? "Рассчитать" : "Пересчитать"}
                    </Button>
                    <Button
                      disabled={!run}
                      onClick={onOpenHistory}
                      size="sm"
                      type="button"
                      variant="outline"
                    >
                      <History size={15} aria-hidden="true" />
                      История
                    </Button>
                  </div>
                </div>
              </td>
              {days.map((day) => (
                <td
                  className="border-b border-r bg-muted/70 px-2 py-2 text-center font-medium text-muted-foreground"
                  key={day}
                  style={{ width: DAY_CELL_WIDTH, minWidth: DAY_CELL_WIDTH }}
                >
                  <div>{weekdayLabels[parseIsoDate(day).getDay()]}</div>
                  <div className="text-base text-foreground">{day.slice(8, 10)}</div>
                </td>
              ))}
            </tr>
            <tr>
              {isLoading ? (
                days.map((day) => (
                  <td className="border-b border-r p-3" key={day}>
                    <Skeleton className="h-12 w-full" />
                  </td>
                ))
              ) : isEmpty ? (
                <td className="border-b border-r px-3 py-4 text-center" colSpan={days.length}>
                  <Button disabled={isRecomputing} onClick={onRecompute} type="button">
                    {isRecomputing ? (
                      <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                    ) : (
                      <Calculator size={16} aria-hidden="true" />
                    )}
                    Рассчитать стоимость
                  </Button>
                  <div className="mt-2 text-xs text-muted-foreground">
                    Запустите расчёт, чтобы увидеть стоимость смен
                  </div>
                </td>
              ) : (
                days.map((day) => (
                  <CostDayCell day={day} key={day} summary={costByDay.get(day)} />
                ))
              )}
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  );
}

function CostDayCell({
  day,
  summary,
}: {
  day: string;
  summary: CostDaySummary | undefined;
}) {
  if (!summary) {
    return (
      <td
        className="h-[76px] border-b border-r px-2 py-3 text-center"
        style={{ width: DAY_CELL_WIDTH, minWidth: DAY_CELL_WIDTH }}
        title="Нет смен или расчёт не содержит данных за день"
      >
        <div className="text-lg font-semibold tabular-nums text-muted-foreground">—</div>
      </td>
    );
  }

  return (
    <td
      className="h-[76px] border-b border-r p-1.5 text-center"
      style={{ width: DAY_CELL_WIDTH, minWidth: DAY_CELL_WIDTH }}
      title={costDayTitle(day, summary)}
    >
      <div className="flex h-full w-full flex-col items-center justify-center rounded-md px-1.5">
        <div
          className={cn(
            "text-lg font-semibold tabular-nums",
            summary.warningCount > 0 ? "text-orange-600" : "text-foreground",
          )}
        >
          {formatMoney(summary.total)}
        </div>
        <div className="mt-1 flex min-h-5 flex-wrap items-center justify-center gap-1 text-[11px] leading-4">
          {summary.warningCount > 0 ? (
            <>
              <AlertTriangle className="h-3.5 w-3.5 text-orange-600" aria-hidden="true" />
              <span className="text-orange-600">проверить</span>
            </>
          ) : null}
          {summary.reasons.slice(0, 2).map((reason) => (
            <span className={costReasonClass(reason)} key={reason}>
              {costReasonLabel(reason)}
            </span>
          ))}
        </div>
      </div>
    </td>
  );
}

function BudgetSummaryPanel({
  isRecomputing,
  onOpenHistory,
  onRecompute,
  run,
}: {
  isRecomputing: boolean;
  onOpenHistory: () => void;
  onRecompute: () => void;
  run: PayrollForecastRunRead | null;
}) {
  const fotLevel = run ? fotStatusLevel(run) : "none";

  return (
    <section className="rounded-lg border bg-card p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <div className="font-medium">Прогноз бюджета</div>
          <div className="mt-1 text-xs text-muted-foreground">
            {run ? `Расчёт от ${formatDateTime(run.run_at)}` : "Стоимость ещё не рассчитана"}
          </div>
        </div>
        <Percent className="h-5 w-5 text-muted-foreground" aria-hidden="true" />
      </div>

      {run ? (
        <div className="grid gap-2 text-sm">
          <SummaryRow label="Выручка прогноз" value={formatMoneyWithCurrency(run.total_revenue_forecast)} />
          <SummaryRow label="Стоимость смен" value={formatMoneyWithCurrency(run.total_shift_cost_estimate)} />
          <SummaryRow
            className={fotStatusClass(fotLevel)}
            label="ФОТ % от выручки"
            title={`Порог: ${formatPercent(run.fot_warning_threshold_pct)}`}
            value={`${formatPercent(run.fot_to_revenue_pct)} · ${fotStatusText(fotLevel)}`}
          />
          <div className="my-1 h-px bg-border" />
          <SummaryRow label="Смен всего" value={String(run.shifts_total)} />
          <SummaryRow
            className={run.shifts_with_warnings > 0 ? "text-orange-600" : undefined}
            label="С предупреждениями"
            value={String(run.shifts_with_warnings)}
          />
          <SummaryRow label="Автор" value={run.run_by_label ?? "—"} />
        </div>
      ) : (
        <div className="rounded-md border border-dashed px-3 py-5 text-center text-sm text-muted-foreground">
          Стоимость ещё не рассчитана. Запустите расчёт.
        </div>
      )}

      <div className="mt-4 flex flex-wrap gap-2">
        <Button disabled={isRecomputing} onClick={onRecompute} size="sm" type="button">
          {isRecomputing ? (
            <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
          ) : (
            <Calculator size={15} aria-hidden="true" />
          )}
          {isRecomputing ? "Идёт расчёт..." : run ? "Пересчитать" : "Рассчитать стоимость"}
        </Button>
        <Button disabled={!run} onClick={onOpenHistory} size="sm" type="button" variant="outline">
          <History size={15} aria-hidden="true" />
          История версий
        </Button>
      </div>
    </section>
  );
}

function SummaryRow({
  className,
  label,
  title,
  value,
}: {
  className?: string;
  label: string;
  title?: string;
  value: string;
}) {
  return (
    <div className="flex items-start justify-between gap-3" title={title}>
      <span className="text-muted-foreground">{label}</span>
      <span className={cn("text-right font-medium tabular-nums", className)}>{value}</span>
    </div>
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
          <SheetDescription>
            Выберите расчёт, чтобы отобразить его в графике.
          </SheetDescription>
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
  const validHistoryCount =
    forecast?.history_points.filter((point) => point.included).length ?? 0;

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
            <DialogDescription>
              Метод: среднее за 6 одинаковых дней недели
            </DialogDescription>
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
            <AlertDialogDescription>
              Будет применён расчётный.
            </AlertDialogDescription>
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
  onEditShift,
  onEmptyCellClick,
  onFilledCellClick,
  roster,
  shiftByEmployeeDay,
}: {
  cashierAllowanceByDay: Map<string, AllowanceAssignmentRead>;
  costByShiftId: Map<string, ShiftCostEstimateRead>;
  days: string[];
  isLoading: boolean;
  isLocked: boolean;
  onEditShift: (shift: ScheduledShiftRead) => void;
  onEmptyCellClick: (employee: EmployeeRosterRow, day: string) => void;
  onFilledCellClick: (shift: ScheduledShiftRead) => void;
  roster: EmployeeRosterRow[];
  shiftByEmployeeDay: Map<string, ScheduledShiftRead>;
}) {
  const minWidth = EMPLOYEE_COLUMN_WIDTH + days.length * DAY_CELL_WIDTH;

  return (
    <div className="overflow-hidden rounded-lg border bg-card">
      <div className="overflow-x-auto">
        <table className="border-separate border-spacing-0 text-sm" style={{ minWidth }}>
          <thead>
            <tr>
              <th
                className="sticky left-0 z-20 border-b border-r bg-muted/90 px-3 py-3 text-left font-medium text-muted-foreground"
                style={{ width: EMPLOYEE_COLUMN_WIDTH, minWidth: EMPLOYEE_COLUMN_WIDTH }}
              >
                Сотрудник
              </th>
              {days.map((day) => (
                <th
                  className="border-b border-r bg-muted/70 px-2 py-2 text-center font-medium text-muted-foreground"
                  key={day}
                  style={{ width: DAY_CELL_WIDTH, minWidth: DAY_CELL_WIDTH }}
                >
                  <div>{weekdayLabels[parseIsoDate(day).getDay()]}</div>
                  <div className="text-base text-foreground">{day.slice(8, 10)}</div>
                </th>
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
                    className="sticky left-0 z-10 border-b border-r bg-card px-3 py-3 align-top"
                    style={{ width: EMPLOYEE_COLUMN_WIDTH, minWidth: EMPLOYEE_COLUMN_WIDTH }}
                  >
                    <div className="font-medium leading-5">{employee.full_name}</div>
                    <div className="mt-1 text-xs text-muted-foreground">
                      {employee.position}
                      {employee.allowances.senior
                        ? " · ст"
                        : employee.allowances.deputy
                          ? " · зам"
                          : ""}
                    </div>
                  </td>
                  {days.map((day) => {
                    const shift = shiftByEmployeeDay.get(`${employee.id}:${day}`);
                    return (
                      <td
                        className={cn(
                          "group relative h-[72px] border-b border-r p-2 align-top",
                          !isLocked && "cursor-pointer hover:bg-primary/5",
                          isLocked && "bg-muted/10",
                        )}
                        key={day}
                        onClick={() => {
                          if (!shift && !isLocked) {
                            onEmptyCellClick(employee, day);
                          } else if (shift && !isLocked) {
                            onFilledCellClick(shift);
                          }
                        }}
                        style={{ width: DAY_CELL_WIDTH, minWidth: DAY_CELL_WIDTH }}
                      >
                        {shift ? (
                          <>
                            <ShiftPill
                              allowanceAssignment={cashierAllowanceByDay.get(day)}
                              employee={employee}
                              estimate={costByShiftId.get(shift.id)}
                              shift={shift}
                            />
                            {!isLocked ? (
                              <EditShiftButton
                                businessDate={day}
                                employeeId={employee.id}
                                onClick={() => onEditShift(shift)}
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
  onCellClick,
  onShiftDelete,
  onShiftClick,
  roster,
  rows,
}: {
  cashierAllowanceByDay: Map<string, AllowanceAssignmentRead>;
  costByShiftId: Map<string, ShiftCostEstimateRead>;
  days: string[];
  isLoading: boolean;
  isLocked: boolean;
  onCellClick: (station: string, day: string) => void;
  onShiftDelete: (shift: ScheduledShiftRead) => void;
  onShiftClick: (shift: ScheduledShiftRead) => void;
  roster: EmployeeRosterRow[];
  rows: Array<{ station: string; byDay: Map<string, ScheduledShiftRead[]> }>;
}) {
  const minWidth = STATION_COLUMN_WIDTH + days.length * DAY_CELL_WIDTH;

  return (
    <div className="overflow-hidden rounded-lg border bg-card">
      <div className="overflow-x-auto">
        <table className="border-separate border-spacing-0 text-sm" style={{ minWidth }}>
          <thead>
            <tr>
              <th
                className="sticky left-0 z-20 border-b border-r bg-muted/90 px-3 py-3 text-left font-medium text-muted-foreground"
                style={{ width: STATION_COLUMN_WIDTH, minWidth: STATION_COLUMN_WIDTH }}
              >
                Станция
              </th>
              {days.map((day) => (
                <th
                  className="border-b border-r bg-muted/70 px-2 py-2 text-center font-medium text-muted-foreground"
                  key={day}
                  style={{ width: DAY_CELL_WIDTH, minWidth: DAY_CELL_WIDTH }}
                >
                  <div>{weekdayLabels[parseIsoDate(day).getDay()]}</div>
                  <div className="text-base text-foreground">{day.slice(8, 10)}</div>
                </th>
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
                    className="sticky left-0 z-10 border-b border-r bg-card px-3 py-3 align-top font-medium"
                    style={{ width: STATION_COLUMN_WIDTH, minWidth: STATION_COLUMN_WIDTH }}
                  >
                    {row.station}
                  </td>
                  {days.map((day) => {
                    const dayShifts = row.byDay.get(day) ?? [];
                    return (
                      <td
                        className={cn(
                          "h-[86px] border-b border-r p-2 align-top",
                          dayShifts.length === 0 &&
                            !isLocked &&
                            "cursor-pointer hover:bg-primary/5",
                          isLocked && "bg-muted/10",
                        )}
                        key={day}
                        onClick={() => {
                          if (dayShifts.length === 0 && !isLocked) {
                            onCellClick(row.station, day);
                          }
                        }}
                        style={{ width: DAY_CELL_WIDTH, minWidth: DAY_CELL_WIDTH }}
                      >
                        <div className="space-y-1">
                          {dayShifts.map((shift) => (
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
        "group relative w-full rounded-md border px-2 py-1 pr-7 text-left text-xs",
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
      <div
        className={cn(
          "truncate font-medium",
          roleColorClasses(shift.payroll_role).primaryText,
        )}
      >
        {shift.employee_full_name}
      </div>
      <div
        className={cn(
          "tabular-nums",
          roleColorClasses(shift.payroll_role).secondaryText,
        )}
      >
        {formatShiftTime(shift)}
      </div>
      <div
        className={cn(
          "truncate",
          roleColorClasses(shift.payroll_role).secondaryText,
        )}
      >
        {payrollRoleLabel(shift.payroll_role)}
      </div>
      <AllowanceBadge badge={allowanceBadge} />
      {costByShiftId.get(shift.id) ? (
        <div
          className={cn(
            "mt-0.5 tabular-nums",
            shiftCostClass(costByShiftId.get(shift.id), shift.payroll_role),
          )}
        >
          {formatMoneyWithCurrency(costByShiftId.get(shift.id)?.total_cost_estimate ?? null)}
        </div>
      ) : null}
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
      className={cn("rounded-md border px-2 py-1.5 text-xs", colors.container)}
      title={shiftTitle(shift, estimate, employee, allowanceAssignment)}
    >
      <div className={cn("font-semibold tabular-nums", colors.primaryText)}>
        {formatShiftTime(shift)}
      </div>
      <div className={cn("mt-1 truncate", colors.secondaryText)}>
        {payrollRoleLabel(shift.payroll_role)}
      </div>
      <AllowanceBadge badge={allowanceBadge} />
      {estimate ? (
        <div className={cn("mt-1 tabular-nums", shiftCostClass(estimate, shift.payroll_role))}>
          {formatMoneyWithCurrency(estimate.total_cost_estimate)}
        </div>
      ) : null}
    </div>
  );
}

type AllowanceBadgeInfo = {
  label: string;
  className: string;
  title: string;
};

function AllowanceBadge({ badge }: { badge: AllowanceBadgeInfo | null }) {
  if (!badge) {
    return null;
  }
  return (
    <span
      className={cn(
        "mt-1 inline-flex h-5 min-w-7 items-center justify-center rounded-sm border px-1 text-[10px] font-medium leading-none",
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
  onSubmit,
  open,
}: {
  draft: ScheduleCreatePayload;
  isSaving: boolean;
  onChange: (draft: ScheduleCreatePayload) => void;
  onOpenChange: (open: boolean) => void;
  onSubmit: () => void;
  open: boolean;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Новый график</DialogTitle>
          <DialogDescription>Период черновика можно выбрать только при создании.</DialogDescription>
        </DialogHeader>
        <div className="grid gap-4">
          <div className="grid gap-2">
            <Label htmlFor="schedule-date-start">Период с</Label>
            <Input
              id="schedule-date-start"
              onChange={(event) => onChange({ ...draft, date_start: event.target.value })}
              type="date"
              value={draft.date_start}
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="schedule-date-end">Период по</Label>
            <Input
              id="schedule-date-end"
              onChange={(event) => onChange({ ...draft, date_end: event.target.value })}
              type="date"
              value={draft.date_end}
            />
          </div>
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
  const plannedHours = state
    ? hoursBetween(state.businessDate, state.startTime, state.endTime)
    : 0;
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
      <DialogContent>
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
                        : defaultRoleForEmployeeAtStation(employee, stationCode) ??
                          employee?.primary_payroll_role ??
                          null,
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
                      {employee.full_name} · {employee.position}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
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
                      {payrollRoleLabel(role.payroll_role)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
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
                    payrollRole: shouldResetEmployee ? null : payrollRole ?? state.payrollRole,
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
            <div className="grid gap-2">
              <Label htmlFor="shift-comment">Комментарий</Label>
              <Textarea
                id="shift-comment"
                onChange={(event) => patchState({ comment: event.target.value })}
                value={state.comment}
              />
            </div>
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
            Копировать смены недели {formatDate(selectedWeekStart)} —{" "}
            {formatDate(selectedWeekEnd)}
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
      deviation: formatCountDeviation(actual?.total_shifts ?? null, summary.planned.total_shifts, deviation?.shifts_pct ?? null),
      status: metricDeviationStatus(deviation?.shifts_pct ?? null, threshold),
      className: deviationTextClass(deviation?.shifts_pct ?? null, threshold),
    },
    {
      label: "Часов",
      planned: formatHoursValue(summary.planned.total_hours),
      actual: formatHoursValue(actual?.total_hours ?? null),
      deviation: formatNumberDeviation(actual?.total_hours ?? null, summary.planned.total_hours, deviation?.hours_pct ?? null),
      status: metricDeviationStatus(deviation?.hours_pct ?? null, threshold),
      className: deviationTextClass(deviation?.hours_pct ?? null, threshold),
    },
    {
      label: "Стоимость",
      planned: formatMoneyWithCurrency(summary.planned.total_cost),
      actual: formatMoneyWithCurrency(actual?.total_cost ?? null),
      deviation: formatMoneyDeviation(actual?.total_cost ?? null, summary.planned.total_cost, deviation?.cost_pct ?? null),
      status: metricDeviationStatus(deviation?.cost_pct ?? null, threshold),
      className: deviationTextClass(deviation?.cost_pct ?? null, threshold),
    },
    {
      label: "Выручка",
      planned: formatMoneyWithCurrency(summary.planned.total_revenue),
      actual: formatMoneyWithCurrency(actual?.total_revenue ?? null),
      deviation: formatMoneyDeviation(actual?.total_revenue ?? null, summary.planned.total_revenue, deviation?.revenue_pct ?? null),
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

function formatCountDeviation(
  actual: number | null,
  planned: number,
  pct: string | number | null,
) {
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

function sourceRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
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
    const current =
      index.get(estimate.business_date) ?? {
        total: 0,
        warningCount: 0,
        reasons: [],
      };
    current.total += decimalToNumber(estimate.total_cost_estimate) ?? 0;
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

function buildStationRows(shifts: ScheduledShiftRead[]) {
  const stations = new Map<string, Map<string, ScheduledShiftRead[]>>();
  shifts.forEach((shift) => {
    const station = stationForShift(shift);
    const byDay = stations.get(station) ?? new Map<string, ScheduledShiftRead[]>();
    const dayShifts = byDay.get(shift.business_date) ?? [];
    dayShifts.push(shift);
    byDay.set(shift.business_date, dayShifts);
    stations.set(station, byDay);
  });
  const orderedStations = [
    ...stationOrder,
    ...[...stations.keys()].filter((station) => !stationOrder.includes(station)).sort(),
  ];
  return orderedStations
    .filter((station) => station !== "(без станции)" || stations.has(station))
    .map((station) => ({
      station,
      byDay: stations.get(station) ?? new Map<string, ScheduledShiftRead[]>(),
    }));
}

function defaultScheduleDraft(): ScheduleCreatePayload {
  const start = nextOrCurrentTuesday(new Date());
  return {
    date_start: toIsoDate(start),
    date_end: toIsoDate(addDays(start, 26)),
    notes: "",
  };
}

function pickDefaultSchedule(schedules: ScheduleRead[], visibleDays: string[]) {
  const firstDay = visibleDays[0];
  const lastDay = visibleDays[visibleDays.length - 1];
  return (
    schedules.find(
      (schedule) =>
        schedule.status === "published" &&
        rangesOverlap(schedule.date_start, schedule.date_end, firstDay, lastDay),
    ) ??
    schedules.find((schedule) => schedule.status === "draft") ??
    schedules[0]
  );
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

function stationForPayrollRole(role: string) {
  const map: Record<string, string | null> = {
    administrator: "Касса",
    pizza: "Пицца",
    sushi: "Роллы",
    shawarma: "Горячий цех",
    prep: null,
    Кассир: "Касса",
  };
  return map[role] ?? "(без станции)";
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
    ? map[employee.default_cooking_station] ?? employee.default_cooking_station
    : null;
}

function primaryAvailableRole(employee: EmployeeRosterRow | undefined) {
  return (
    employee?.available_roles.find((role) => role.is_primary) ?? employee?.available_roles[0]
  );
}

function roleForShiftOrPrimary(
  shift: ScheduledShiftRead,
  employee: EmployeeRosterRow | undefined,
) {
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
    employee.available_roles.find((role) =>
      stationsMatch(role.default_station_code, stationCode),
    )?.payroll_role ?? null
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
  return PAYROLL_ROLE_LABELS[role as PayrollRole] ?? role;
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
  return reason ? labels[reason] ?? reason : "авто";
}

function buildWeekDays(anchorIso: string) {
  const start = startOfTuesdayWeek(parseIsoDate(anchorIso));
  return Array.from({ length: 7 }, (_, index) => toIsoDate(addDays(start, index)));
}

function buildMonthDays(anchorIso: string) {
  const anchor = parseIsoDate(anchorIso);
  const start = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
  const end = new Date(anchor.getFullYear(), anchor.getMonth() + 1, 0);
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

function nextOrCurrentTuesday(value: Date) {
  const date = new Date(value);
  date.setHours(0, 0, 0, 0);
  const delta = (2 - date.getDay() + 7) % 7;
  return addDays(date, delta);
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

function formatDate(value: string) {
  const [year, month, day] = value.split("-");
  return `${day}.${month}.${year}`;
}

function formatRange(start: string, end: string) {
  return `${formatDate(start)} — ${formatDate(end)}`;
}

function formatShiftTime(shift: ScheduledShiftRead) {
  return `${timeFromDateTime(shift.planned_start_at)}–${timeFromDateTime(shift.planned_end_at)}`;
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

function forecastStatusLabel(forecast: RevenueForecastRead) {
  if (forecast.quality_status === "manual_override") {
    return "override";
  }
  if (forecast.quality_status === "requires_review" || forecast.forecast_amount === null) {
    return "requires review";
  }
  return "";
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

function costReasonClass(reason: string) {
  if (reason === "no_rate" || reason === "no_category" || reason === "no_role") {
    return "text-red-600";
  }
  if (reason === "forecast_missing") {
    return "text-muted-foreground";
  }
  return "text-orange-600";
}

function costDayTitle(day: string, summary: CostDaySummary) {
  const reasons = summary.reasons.map(costReasonLabel).join(", ");
  return [
    formatDate(day),
    `Стоимость: ${formatMoneyWithCurrency(summary.total)}`,
    summary.warningCount > 0 ? `Предупреждения: ${summary.warningCount}` : null,
    reasons ? `Причины: ${reasons}` : null,
  ]
    .filter(Boolean)
    .join("\n");
}

function cashierAllowancePlanFactTitle(row: PlanFactDayRowRead) {
  return [
    `План: ${cashierAllowanceInfoText(row.planned_cashier_allowance)}`,
    `Факт: ${cashierAllowanceInfoText(row.actual_cashier_allowance)}`,
  ].join("\n");
}

function cashierAllowanceInfoText(
  value: PlanFactDayRowRead["planned_cashier_allowance"],
) {
  if (!value) {
    return "нет данных";
  }
  const recipient =
    value.recipient_role === "none" ? "никто" : value.recipient_full_name ?? "—";
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

function shiftCostClass(estimate: ShiftCostEstimateRead | undefined, role?: string) {
  return estimate?.quality_status === "requires_review"
    ? "text-orange-600"
    : roleColorClasses(role ?? "").secondaryText;
}

function roleColorClasses(role: string) {
  const fallback = {
    container: "border-border bg-background",
    primaryText: "text-foreground",
    secondaryText: "text-muted-foreground",
  };
  const classes: Record<
    string,
    { container: string; primaryText: string; secondaryText: string }
  > = {
    sushi: {
      container: "border-blue-300 bg-blue-50",
      primaryText: "text-blue-900",
      secondaryText: "text-blue-700",
    },
    shawarma: {
      container: "border-purple-300 bg-purple-50",
      primaryText: "text-purple-900",
      secondaryText: "text-purple-700",
    },
    administrator: {
      container: "border-pink-300 bg-pink-50",
      primaryText: "text-pink-900",
      secondaryText: "text-pink-700",
    },
    pizza: {
      container: "border-yellow-300 bg-yellow-50",
      primaryText: "text-yellow-950",
      secondaryText: "text-yellow-800",
    },
    prep: {
      container: "border-emerald-800 bg-emerald-900",
      primaryText: "text-white",
      secondaryText: "text-emerald-100",
    },
    Кассир: {
      container: "border-pink-300 bg-pink-50",
      primaryText: "text-pink-900",
      secondaryText: "text-pink-700",
    },
  };
  return classes[role] ?? fallback;
}

function fotStatusLevel(run: PayrollForecastRunRead): FotStatusLevel {
  const value = decimalToNumber(run.fot_to_revenue_pct);
  const threshold = decimalToNumber(run.fot_warning_threshold_pct) ?? 28;
  if (value === null) {
    return "none";
  }
  if (value < threshold) {
    return "ok";
  }
  if (value < threshold + 4) {
    return "warning";
  }
  return "danger";
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

function fotStatusText(level: FotStatusLevel) {
  if (level === "ok") {
    return "ниже порога";
  }
  if (level === "warning") {
    return "внимание";
  }
  if (level === "danger") {
    return "критично";
  }
  return "нет данных";
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
      : Number(String(value).replace(/[\s\u00a0]/g, "").replace(",", "."));
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

function breakdownLine(
  breakdown: Record<string, unknown>,
  key: string,
  label: string,
) {
  const value = breakdown[key];
  if (value === null || value === undefined || value === "") {
    return null;
  }
  return `${label}: ${String(value)}`;
}

function rangesOverlap(
  leftStart: string,
  leftEnd: string,
  rightStart: string,
  rightEnd: string,
) {
  return leftStart <= rightEnd && leftEnd >= rightStart;
}
