import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CalendarDays,
  ChevronLeft,
  ChevronRight,
  Copy,
  LoaderCircle,
  Lock,
  Plus,
  Send,
  Users,
} from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
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
  getSchedule,
  listSchedules,
  patchShift,
  publishSchedule,
  upsertShift,
  type EmployeeRosterRow,
  type ScheduleCreatePayload,
  type ScheduleRead,
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
  const [deleteTarget, setDeleteTarget] = useState<ScheduledShiftRead | null>(null);
  const [publishOpen, setPublishOpen] = useState(false);
  const [newVersionOpen, setNewVersionOpen] = useState(false);
  const [copyDialog, setCopyDialog] = useState<CopyWeekState>(() => ({
    open: false,
    targetMode: "next",
    customDate: toIsoDate(addDays(initialWeekStart, 7)),
  }));

  const visibleDays = useMemo(
    () => (scaleMode === "week" ? buildWeekDays(anchorDate) : buildMonthDays(anchorDate)),
    [anchorDate, scaleMode],
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

  const schedules = useMemo(
    () => [...(schedulesQuery.data ?? [])].sort(compareSchedulesForSelect),
    [schedulesQuery.data],
  );
  const roster = useMemo(
    () => [...(rosterQuery.data ?? [])].sort(compareRosterRows),
    [rosterQuery.data],
  );
  const currentSchedule = scheduleQuery.data ?? null;
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

  async function invalidateCurrentSchedule() {
    await queryClient.invalidateQueries({ queryKey: ["schedule", selectedScheduleId] });
    await queryClient.invalidateQueries({ queryKey: ["schedules"] });
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

      <CopyWeekDialog
        copyDialog={copyDialog}
        isSaving={copyWeekMutation.isPending}
        onChange={setCopyDialog}
        onSubmit={submitCopyWeek}
        selectedWeekEnd={selectedWeekEnd}
        selectedWeekStart={selectedWeekStart}
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
    </div>
  );
}

function EmployeeScheduleGrid({
  days,
  isLoading,
  isLocked,
  onCellClick,
  roster,
  shiftByEmployeeDay,
}: {
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
                        {shift ? <ShiftPill shift={shift} /> : null}
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
  days,
  isLoading,
  isLocked,
  onCellClick,
  onShiftClick,
  rows,
}: {
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
                            title={shiftTitle(shift)}
                            type="button"
                          >
                            <div className="truncate font-medium">{shift.employee_full_name}</div>
                            <div className="tabular-nums text-muted-foreground">
                              {formatShiftTime(shift)}
                            </div>
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

function ShiftPill({ shift }: { shift: ScheduledShiftRead }) {
  return (
    <div
      className="rounded-md border border-primary/20 bg-primary/10 px-2 py-1.5 text-xs"
      title={shiftTitle(shift)}
    >
      <div className="font-semibold tabular-nums text-primary">{formatShiftTime(shift)}</div>
      <div className="mt-1 truncate text-muted-foreground">
        {shift.station_code || stationForPayrollRole(shift.payroll_role)}
      </div>
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

function shiftTitle(shift: ScheduledShiftRead) {
  const station = shift.station_code || stationForPayrollRole(shift.payroll_role);
  return [
    `${shift.employee_full_name}: ${formatShiftTime(shift)}`,
    station,
    shift.comment_private,
  ]
    .filter(Boolean)
    .join("\n");
}

function rangesOverlap(
  leftStart: string,
  leftEnd: string,
  rightStart: string,
  rightEnd: string,
) {
  return leftStart <= rightEnd && leftEnd >= rightStart;
}
