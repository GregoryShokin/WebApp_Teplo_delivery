import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Calculator,
  CalendarDays,
  ChevronLeft,
  ChevronRight,
  Copy,
  History,
  LoaderCircle,
  Lock,
  Percent,
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
  deleteShift,
  getEmployeesRoster,
  getForecastRange,
  getLatestRun,
  getRun,
  getSchedule,
  listRuns,
  listSchedules,
  overrideForecast,
  patchShift,
  publishSchedule,
  recomputeForecast,
  removeForecastOverride,
  runCostForecast,
  upsertShift,
  type EmployeeRosterRow,
  type PayrollForecastRunRead,
  type RevenueForecastRead,
  type RevenueForecastRecomputePayload,
  type ScheduleCreatePayload,
  type ScheduleRead,
  type ShiftCostEstimateRead,
  type ScheduledShiftRead,
  type ScheduledShiftUpsertPayload,
} from "@/lib/api";
import { cn } from "@/lib/utils";

type ViewMode = "employees" | "stations";
type ScaleMode = "week" | "month";

type ShiftDialogState = {
  mode: "create" | "edit";
  shift: ScheduledShiftRead | null;
  employeeId: string;
  businessDate: string;
  stationCode: string | null;
  startTime: string;
  endTime: string;
  comment: string;
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
const stationOptions = ["Пицца", "Роллы", "Касса", "Шаурма"];
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
  const [selectedScheduleId, setSelectedScheduleId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [createDraft, setCreateDraft] = useState<ScheduleCreatePayload>(() =>
    defaultScheduleDraft(),
  );
  const [shiftDialog, setShiftDialog] = useState<ShiftDialogState | null>(null);
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

  const schedules = useMemo(
    () => [...(schedulesQuery.data ?? [])].sort(compareSchedulesForSelect),
    [schedulesQuery.data],
  );
  const roster = useMemo(
    () => [...(rosterQuery.data ?? [])].sort(compareRosterRows),
    [rosterQuery.data],
  );
  const currentSchedule = scheduleQuery.data ?? null;
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
    mutationFn: (variables: {
      scheduleId: string;
      shiftId?: string;
      payload: ScheduledShiftUpsertPayload;
    }) =>
      variables.shiftId
        ? patchShift(variables.scheduleId, variables.shiftId, variables.payload)
        : upsertShift(variables.scheduleId, variables.payload),
    onSuccess: async () => {
      toast.success("Смена сохранена");
      setShiftDialog(null);
      await invalidateCurrentSchedule();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить смену")),
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
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось рассчитать прогноз")),
  });

  const recomputeForecastMutation = useMutation({
    mutationFn: (payload: RevenueForecastRecomputePayload) => recomputeForecast(payload),
    onSuccess: async (result) => {
      toast.success(`Пересчитано ${result.recomputed} дней`);
      setForceRefreshConfirmOpen(false);
      await queryClient.invalidateQueries({ queryKey: ["forecast"] });
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

  async function invalidateCurrentSchedule() {
    await queryClient.invalidateQueries({ queryKey: ["schedule", selectedScheduleId] });
    await queryClient.invalidateQueries({ queryKey: ["schedules"] });
  }

  async function invalidateCostForecast() {
    await queryClient.invalidateQueries({ queryKey: ["cost-forecast", currentSchedule?.id] });
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
    const fallbackStation =
      options.stationCode ??
      defaultStationForEmployee(roster.find((employee) => employee.id === options.employeeId));
    setShiftDialog({
      mode: shift ? "edit" : "create",
      shift,
      employeeId: shift?.employee_id ?? options.employeeId ?? "",
      businessDate: shift?.business_date ?? options.businessDate,
      stationCode: shift?.station_code ?? fallbackStation,
      startTime: shift ? timeFromDateTime(shift.planned_start_at) : "10:00",
      endTime: shift ? timeFromDateTime(shift.planned_end_at) : "22:00",
      comment: shift?.comment_private ?? "",
    });
  }

  function submitShiftDialog() {
    if (!currentSchedule || !shiftDialog || !shiftDialog.employeeId) {
      toast.error("Выберите сотрудника");
      return;
    }
    saveShiftMutation.mutate({
      scheduleId: currentSchedule.id,
      shiftId: shiftDialog.shift?.id,
      payload: {
        business_date: shiftDialog.businessDate,
        employee_id: shiftDialog.employeeId,
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

      {currentSchedule ? (
        <RevenueForecastPanel
          days={visibleDays}
          forecasts={forecastQuery.data ?? []}
          forceRefreshIiko={forceRefreshIiko}
          isLoading={forecastQuery.isLoading || warmForecastMutation.isPending}
          isRecomputing={recomputeForecastMutation.isPending}
          leadingWidth={viewMode === "employees" ? EMPLOYEE_COLUMN_WIDTH : STATION_COLUMN_WIDTH}
          onCellClick={openForecastDialog}
          onForceRefreshChange={setForceRefreshIiko}
          onRecompute={requestForecastRecompute}
        />
      ) : null}

      {currentSchedule ? (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
          <CostForecastPanel
            days={visibleDays}
            isLoading={
              latestCostQuery.isLoading || (selectedCostRunId ? selectedCostQuery.isLoading : false)
            }
            isRecomputing={runCostForecastMutation.isPending}
            leadingWidth={viewMode === "employees" ? EMPLOYEE_COLUMN_WIDTH : STATION_COLUMN_WIDTH}
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
      ) : null}

      {!currentSchedule && !schedulesQuery.isLoading ? (
        <EmptyState
          action={<Button onClick={openCreateDialog}>Создать новый график</Button>}
          icon={<CalendarDays className="h-5 w-5" aria-hidden="true" />}
          title="Графика на этот период ещё нет. Создайте новый."
        />
      ) : viewMode === "employees" ? (
        <EmployeeScheduleGrid
          days={visibleDays}
          isLoading={scheduleQuery.isLoading || rosterQuery.isLoading}
          isLocked={isLocked}
          onCellClick={(employee, day, shift) =>
            openShiftDialog({
              employeeId: employee.id,
              businessDate: day,
              shift,
            })
          }
          costByShiftId={costEstimatesByShiftId}
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
          costByShiftId={costEstimatesByShiftId}
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
        employees={roster}
        isSaving={saveShiftMutation.isPending}
        onDelete={(shift) => setDeleteTarget(shift)}
        onOpenChange={(open) => {
          if (!open) {
            setShiftDialog(null);
          }
        }}
        onSubmit={submitShiftDialog}
        setValue={setShiftDialog}
        state={shiftDialog}
      />

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
                    day={day}
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
  day,
  forecast,
  onClick,
}: {
  day: string;
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
  costByShiftId,
  days,
  isLoading,
  isLocked,
  onCellClick,
  roster,
  shiftByEmployeeDay,
}: {
  costByShiftId: Map<string, ShiftCostEstimateRead>;
  days: string[];
  isLoading: boolean;
  isLocked: boolean;
  onCellClick: (
    employee: EmployeeRosterRow,
    day: string,
    shift: ScheduledShiftRead | undefined,
  ) => void;
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
                      {employee.allowances.senior ? " ★" : ""}
                    </div>
                  </td>
                  {days.map((day) => {
                    const shift = shiftByEmployeeDay.get(`${employee.id}:${day}`);
                    return (
                      <td
                        className={cn(
                          "h-[72px] border-b border-r p-2 align-top",
                          isLocked ? "bg-muted/10" : "cursor-pointer hover:bg-primary/5",
                        )}
                        key={day}
                        onClick={() => onCellClick(employee, day, shift)}
                        style={{ width: DAY_CELL_WIDTH, minWidth: DAY_CELL_WIDTH }}
                      >
                        {shift ? (
                          <ShiftPill
                            estimate={costByShiftId.get(shift.id)}
                            shift={shift}
                          />
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
  costByShiftId,
  days,
  isLoading,
  isLocked,
  onCellClick,
  onShiftClick,
  rows,
}: {
  costByShiftId: Map<string, ShiftCostEstimateRead>;
  days: string[];
  isLoading: boolean;
  isLocked: boolean;
  onCellClick: (station: string, day: string) => void;
  onShiftClick: (shift: ScheduledShiftRead) => void;
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
                  {days.map((day) => (
                    <td
                      className={cn(
                        "h-[86px] border-b border-r p-2 align-top",
                        isLocked ? "bg-muted/10" : "cursor-pointer hover:bg-primary/5",
                      )}
                      key={day}
                      onClick={() => onCellClick(row.station, day)}
                      style={{ width: DAY_CELL_WIDTH, minWidth: DAY_CELL_WIDTH }}
                    >
                      <div className="space-y-1">
                        {(row.byDay.get(day) ?? []).map((shift) => (
                          <button
                            className="w-full rounded-md border bg-background px-2 py-1 text-left text-xs hover:border-primary/50"
                            key={shift.id}
                            onClick={(event) => {
                              event.stopPropagation();
                              onShiftClick(shift);
                            }}
                            title={shiftTitle(shift, costByShiftId.get(shift.id))}
                            type="button"
                          >
                            <div className="truncate font-medium">{shift.employee_full_name}</div>
                            <div className="tabular-nums text-muted-foreground">
                              {formatShiftTime(shift)}
                            </div>
                            {costByShiftId.get(shift.id) ? (
                              <div
                                className={cn(
                                  "mt-0.5 tabular-nums",
                                  shiftCostClass(costByShiftId.get(shift.id)),
                                )}
                              >
                                {formatMoneyWithCurrency(
                                  costByShiftId.get(shift.id)?.total_cost_estimate ?? null,
                                )}
                              </div>
                            ) : null}
                          </button>
                        ))}
                      </div>
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ShiftPill({
  estimate,
  shift,
}: {
  estimate: ShiftCostEstimateRead | undefined;
  shift: ScheduledShiftRead;
}) {
  return (
    <div
      className="rounded-md border border-primary/20 bg-primary/10 px-2 py-1.5 text-xs"
      title={shiftTitle(shift, estimate)}
    >
      <div className="font-semibold tabular-nums text-primary">{formatShiftTime(shift)}</div>
      <div className="mt-1 truncate text-muted-foreground">
        {shift.station_code || stationForPayrollRole(shift.payroll_role)}
      </div>
      {estimate ? (
        <div className={cn("mt-1 tabular-nums", shiftCostClass(estimate))}>
          {formatMoneyWithCurrency(estimate.total_cost_estimate)}
        </div>
      ) : null}
    </div>
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
  employees,
  isSaving,
  onDelete,
  onOpenChange,
  onSubmit,
  setValue,
  state,
}: {
  employees: EmployeeRosterRow[];
  isSaving: boolean;
  onDelete: (shift: ScheduledShiftRead) => void;
  onOpenChange: (open: boolean) => void;
  onSubmit: () => void;
  setValue: (state: ShiftDialogState | null) => void;
  state: ShiftDialogState | null;
}) {
  const selectedEmployee = employees.find((employee) => employee.id === state?.employeeId);
  const plannedHours = state
    ? hoursBetween(state.businessDate, state.startTime, state.endTime)
    : 0;

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
          <DialogTitle>Смена</DialogTitle>
          <DialogDescription>
            {state ? formatDate(state.businessDate) : ""}
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
                  patchState({
                    employeeId: value === NO_VALUE ? "" : value,
                    stationCode: defaultStationForEmployee(employee),
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
                  {employees.map((employee) => (
                    <SelectItem key={employee.id} value={employee.id}>
                      {employee.full_name} · {employee.position}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="grid gap-2">
              <Label>Станция</Label>
              <Select
                onValueChange={(value) =>
                  patchState({ stationCode: value === NO_VALUE ? null : value })
                }
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
  return role === "Кассир" ? "Касса" : "(без станции)";
}

function defaultStationForEmployee(employee: EmployeeRosterRow | undefined) {
  if (!employee) {
    return null;
  }
  if (employee.position === "Кассир") {
    return "Касса";
  }
  const map: Record<string, string> = {
    pizza: "Пицца",
    sushi: "Роллы",
    shawarma: "Шаурма",
  };
  return employee.default_cooking_station
    ? map[employee.default_cooking_station] ?? employee.default_cooking_station
    : null;
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

function shiftCostClass(estimate: ShiftCostEstimateRead | undefined) {
  return estimate?.quality_status === "requires_review" ? "text-orange-600" : "text-muted-foreground";
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
) {
  const station = shift.station_code || stationForPayrollRole(shift.payroll_role);
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
