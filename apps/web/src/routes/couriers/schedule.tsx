import { useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CalendarRange,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleOff,
  Clock3,
  Download,
  Info,
  Pause,
  Pencil,
  RefreshCw,
  Search,
  Sparkles,
  Star,
  Trash2,
  UsersRound,
} from "lucide-react";
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
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { EmptyState } from "@/components/ui-app/EmptyState";
import { PageHeader } from "@/components/ui-app/PageHeader";
import {
  apiErrorMessage,
  deleteCourierShift,
  getCourierList,
  getCourierScheduleMatched,
  syncCourierAttendance,
  upsertCourierShift,
  type CourierDepositStatusFilter,
  type CourierListRow,
  type CourierListWorkStatusFilter,
  type CourierScheduleCategory,
  type CourierScheduleMatchedEntry,
  type CourierWorkStatus,
} from "@/lib/api";
import { usePermissions } from "@/lib/permissions";
import { cn } from "@/lib/utils";

import { currentMonthKey } from "./utils";

type CourierScheduleActiveTab = "grid" | "list";

type CourierScheduleRouteProps = {
  activeTab: CourierScheduleActiveTab;
  onNavigate: (path: string) => void;
};

type ScheduleCourier = {
  id: string;
  full_name: string;
  iiko_id: string;
  status: string;
  work_status: CourierWorkStatus | null;
};

type EditingShift = {
  courier: ScheduleCourier;
  dateKey: string;
  entry: CourierScheduleMatchedEntry | null;
};

type ShiftForm = {
  category: CourierScheduleCategory;
  startTime: string;
  endTime: string;
  comment: string;
};

const COURIER_SCHEDULE_ACTIVE_TAB_STORAGE_KEY = "couriers.schedule.activeTab";
const DEFAULT_START_TIME = "10:00";
const DEFAULT_END_TIME = "22:00";
const WEEKDAY_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
type CourierDirectoryStatusFilter = CourierDepositStatusFilter | "reserve";

const COURIER_DIRECTORY_STATUS_LABELS: Record<CourierDirectoryStatusFilter, string> = {
  active: "Активные",
  reserve: "В резерве",
  fired: "Уволенные",
  all: "Все",
};

export function CourierScheduleRoute({ activeTab, onNavigate }: CourierScheduleRouteProps) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const canEditSchedule = permissions.canPerformAction("couriers.schedule.edit");
  const canSyncShifts = permissions.canPerformAction("couriers.shifts.sync");
  const [weekStart, setWeekStart] = useState(() => startOfMondayWeek(new Date()));
  const [primaryMode, setPrimaryMode] = useState(false);
  const [legendOpen, setLegendOpen] = useState(false);
  const [editingShift, setEditingShift] = useState<EditingShift | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<EditingShift | null>(null);
  const [form, setForm] = useState<ShiftForm>(() => emptyShiftForm("secondary"));

  const weekDays = useMemo(
    () => Array.from({ length: 7 }, (_, index) => addDays(weekStart, index)),
    [weekStart],
  );
  const weekDateKeys = useMemo(() => weekDays.map((day) => toDateKey(day)), [weekDays]);
  const from = weekDateKeys[0];
  const to = weekDateKeys[6];
  const rosterMonth = currentMonthKey();

  useEffect(() => {
    window.localStorage.setItem(COURIER_SCHEDULE_ACTIVE_TAB_STORAGE_KEY, activeTab);
  }, [activeTab]);

  const couriersQuery = useQuery({
    queryKey: ["courier-list", "courier-schedule-roster", rosterMonth],
    queryFn: () => getCourierList({ status: "active", month: rosterMonth }),
    staleTime: 60_000,
    enabled: activeTab === "grid",
  });

  const matchedQuery = useQuery({
    queryKey: ["courier-schedule-matched", from, to],
    queryFn: () => getCourierScheduleMatched(from, to),
    staleTime: 15_000,
    enabled: activeTab === "grid",
  });

  const activeCouriers = useMemo(
    () =>
      sortScheduleCouriers(
        (couriersQuery.data?.rows ?? []).map(courierListRowToScheduleCourier),
        matchedQuery.data ?? [],
        weekDateKeys,
      ),
    [couriersQuery.data?.rows, matchedQuery.data, weekDateKeys],
  );

  const entriesByCell = useMemo(() => indexEntries(matchedQuery.data ?? []), [matchedQuery.data]);

  const syncAttendanceMutation = useMutation({
    mutationFn: () => syncCourierAttendance(from, to),
    onSuccess: async (report) => {
      await invalidateCourierSchedule(queryClient);
      const changedRecords = report.new + report.updated;
      const errorsSuffix = report.errors.length > 0 ? `, ошибок: ${report.errors.length}` : "";
      toast.success(
        `iiko смены обновлены: ${changedRecords} записей, ${report.matched_couriers} фактов${errorsSuffix}`,
      );
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось загрузить смены из iiko"));
    },
  });

  const upsertMutation = useMutation({
    mutationFn: (payload: {
      courierId: string;
      workDate: string;
      values: {
        category: CourierScheduleCategory;
        planned_start_at?: string | null;
        planned_end_at?: string | null;
        comment?: string | null;
      };
    }) => upsertCourierShift(payload.courierId, payload.workDate, payload.values),
    onSuccess: async (_entry, variables) => {
      setEditingShift(null);
      await invalidateCourierSchedule(queryClient);
      toast.success(
        isPastDateKey(variables.workDate)
          ? "Смена в прошлом, дисциплина пересчитается"
          : variables.values.planned_start_at
            ? "Смена сохранена"
            : "Смена создана",
      );
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось сохранить смену"));
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (payload: { courierId: string; workDate: string }) =>
      deleteCourierShift(payload.courierId, payload.workDate),
    onSuccess: async () => {
      setEditingShift(null);
      setDeleteTarget(null);
      await invalidateCourierSchedule(queryClient);
      toast.success("Смена удалена");
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось удалить смену"));
    },
  });

  function handleTabChange(value: string) {
    if (!isCourierScheduleTab(value)) {
      return;
    }
    window.localStorage.setItem(COURIER_SCHEDULE_ACTIVE_TAB_STORAGE_KEY, value);
    onNavigate(courierScheduleTabPath(value));
  }

  function openEditor(
    courier: ScheduleCourier,
    dateKey: string,
    entry: CourierScheduleMatchedEntry | null,
  ) {
    if (!canEditSchedule) {
      return;
    }
    setEditingShift({ courier, dateKey, entry });
    setForm(formFromEntry(entry, primaryMode ? "primary" : "secondary"));
  }

  function handleCellClick(
    courier: ScheduleCourier,
    dateKey: string,
    entry: CourierScheduleMatchedEntry | null,
  ) {
    if (!canEditSchedule) {
      return;
    }
    if (entry?.category) {
      deleteMutation.mutate({ courierId: courier.id, workDate: dateKey });
      return;
    }
    if (entry?.status === "helping") {
      toast("Помощь без плана: есть iiko-смена, плана на день нет");
      return;
    }
    if (entry?.status === "not_counted") {
      toast("Факт без плана. Чтобы запланировать — нажмите карандашик");
      return;
    }
    upsertMutation.mutate({
      courierId: courier.id,
      workDate: dateKey,
      values: {
        category: primaryMode ? "primary" : "secondary",
      },
    });
  }

  function handleSaveShift() {
    if (!editingShift) {
      return;
    }
    upsertMutation.mutate({
      courierId: editingShift.courier.id,
      workDate: editingShift.dateKey,
      values: {
        category: form.category,
        planned_start_at: dateTimeWithMoscowOffset(editingShift.dateKey, form.startTime),
        planned_end_at: dateTimeWithMoscowOffset(editingShift.dateKey, form.endTime),
        comment: form.comment.trim() || null,
      },
    });
  }

  return (
    <div className="space-y-5">
      <Tabs className="space-y-5" onValueChange={handleTabChange} value={activeTab}>
        <TabsList>
          <TabsTrigger value="grid">График</TabsTrigger>
          <TabsTrigger value="list">Список курьеров</TabsTrigger>
        </TabsList>

        <TabsContent className="mt-0 space-y-5" value="grid">
          <PageHeader
            title="График курьеров"
            description="Расписание смен с разделением на основные и второстепенные."
            action={
              <div className="flex flex-wrap gap-2">
                {canSyncShifts ? (
                  <Button
                    disabled={syncAttendanceMutation.isPending}
                    onClick={() => syncAttendanceMutation.mutate()}
                    title="Запросить фактические смены из iiko за выбранную неделю"
                    variant="outline"
                  >
                    <Download size={16} aria-hidden="true" />
                    {syncAttendanceMutation.isPending ? "iiko..." : "Смены iiko"}
                  </Button>
                ) : null}
                <Button
                  onClick={() => void invalidateCourierSchedule(queryClient)}
                  title="Обновить"
                  variant="outline"
                >
                  <RefreshCw size={16} aria-hidden="true" />
                  Обновить
                </Button>
              </div>
            }
          />

          <section className="flex flex-wrap items-center justify-between gap-3 rounded-md border bg-card p-3">
            <div className="flex items-center rounded-md border bg-background p-1">
              <Button
                onClick={() => setWeekStart((current) => addDays(current, -7))}
                size="icon"
                title="Предыдущая неделя"
                variant="ghost"
              >
                <ChevronLeft size={16} aria-hidden="true" />
              </Button>
              <div className="flex h-9 min-w-[230px] items-center justify-center gap-2 px-3 text-sm font-medium">
                <CalendarRange className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
                {weekLabel(weekStart)}
              </div>
              <Button
                onClick={() => setWeekStart((current) => addDays(current, 7))}
                size="icon"
                title="Следующая неделя"
                variant="ghost"
              >
                <ChevronRight size={16} aria-hidden="true" />
              </Button>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <Button onClick={() => setWeekStart(startOfMondayWeek(new Date()))} variant="outline">
                Текущая
              </Button>
              {canEditSchedule ? (
                <Button
                  className={cn(primaryMode && "bg-teal-600 text-white hover:bg-teal-700")}
                  onClick={() => setPrimaryMode((current) => !current)}
                  title="Режим основных"
                  variant={primaryMode ? "default" : "outline"}
                >
                  <Sparkles size={16} aria-hidden="true" />
                  Режим основных
                </Button>
              ) : null}
              <Button onClick={() => setLegendOpen(true)} variant="outline">
                <Info size={16} aria-hidden="true" />
                Легенда
              </Button>
            </div>
          </section>

          {!canEditSchedule ? (
            <div className="rounded-md border bg-muted/40 px-3 py-2 text-sm text-muted-foreground">
              Режим просмотра. Создание, удаление и редактирование смен скрыты.
            </div>
          ) : null}

          <section className="overflow-hidden rounded-md border bg-card">
            {couriersQuery.isLoading || matchedQuery.isLoading ? (
              <MatrixSkeleton />
            ) : couriersQuery.isError || matchedQuery.isError ? (
              <div className="p-4">
                <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                  {apiErrorMessage(
                    couriersQuery.error ?? matchedQuery.error,
                    "Не удалось загрузить график курьеров",
                  )}
                </div>
              </div>
            ) : activeCouriers.length === 0 ? (
              <div className="p-4">
                <EmptyState
                  icon={<UsersRound size={18} aria-hidden="true" />}
                  title="Активные курьеры не найдены"
                  description="Проверьте должности и статусы сотрудников."
                />
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[980px] border-collapse text-sm">
                  <thead>
                    <tr className="border-b bg-muted/60">
                      <th className="sticky left-0 z-10 w-[240px] bg-muted/95 px-3 py-3 text-left font-medium">
                        Курьер
                      </th>
                      {weekDays.map((day, index) => (
                        <th
                          className="w-[108px] px-2 py-3 text-center font-medium"
                          key={toDateKey(day)}
                        >
                          <div>{WEEKDAY_LABELS[index]}</div>
                          <div className="text-xs text-muted-foreground">{day.getDate()}</div>
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody key={from}>
                    <AnimatePresence initial={false}>
                      {activeCouriers.map((courier) => (
                        <motion.tr
                          className="border-b last:border-b-0"
                          key={courier.id}
                          layout
                          transition={{ type: "spring", stiffness: 300, damping: 30 }}
                        >
                          <th className="sticky left-0 z-10 bg-card px-3 py-2 text-left font-medium">
                            <div className="truncate">{courier.full_name}</div>
                            <div className="truncate text-xs font-normal text-muted-foreground">
                              {courier.iiko_id || "iiko ID не указан"}
                            </div>
                            <CourierGridStatusChip
                              status={courier.status}
                              workStatus={courier.work_status}
                            />
                          </th>
                          {weekDays.map((day) => {
                            const dateKey = toDateKey(day);
                            const entry = entriesByCell.get(cellKey(courier.id, dateKey)) ?? null;
                            return (
                              <td className="p-1.5" key={dateKey}>
                                <ShiftCell
                                  dateKey={dateKey}
                                  entry={entry}
                                  isPending={upsertMutation.isPending || deleteMutation.isPending}
                                  readOnly={!canEditSchedule}
                                  onClick={() => handleCellClick(courier, dateKey, entry)}
                                  onEdit={() => openEditor(courier, dateKey, entry)}
                                />
                              </td>
                            );
                          })}
                        </motion.tr>
                      ))}
                    </AnimatePresence>
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </TabsContent>

        <TabsContent className="mt-0" value="list">
          <CourierDirectoryTab />
        </TabsContent>
      </Tabs>

      <ShiftEditorDialog
        editingShift={editingShift}
        form={form}
        isSaving={upsertMutation.isPending}
        onCancel={() => setEditingShift(null)}
        onDelete={() => {
          if (canEditSchedule && editingShift?.entry?.category) {
            setDeleteTarget(editingShift);
          }
        }}
        onFormChange={setForm}
        onSave={handleSaveShift}
      />

      <LegendDialog open={legendOpen} onOpenChange={setLegendOpen} />

      <AlertDialog
        onOpenChange={(open) => !open && setDeleteTarget(null)}
        open={Boolean(deleteTarget)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Удалить смену?</AlertDialogTitle>
            <AlertDialogDescription>
              Смена будет удалена из графика, а matching и KPI пересчитаются.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Отмена</AlertDialogCancel>
            <AlertDialogAction
              disabled={!canEditSchedule}
              onClick={() => {
                if (deleteTarget) {
                  deleteMutation.mutate({
                    courierId: deleteTarget.courier.id,
                    workDate: deleteTarget.dateKey,
                  });
                }
              }}
            >
              Удалить
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function ShiftCell({
  dateKey,
  entry,
  isPending,
  readOnly,
  onClick,
  onEdit,
}: {
  dateKey: string;
  entry: CourierScheduleMatchedEntry | null;
  isPending: boolean;
  readOnly: boolean;
  onClick: () => void;
  onEdit: () => void;
}) {
  const value = cellValue(entry);
  const hasEdit = Boolean(
    entry?.category || entry?.status === "helping" || entry?.status === "not_counted",
  );
  const isOpenShift = Boolean(entry?.opened_at && !entry.closed_at);

  return (
    <div
      className={cn(
        "group relative flex h-16 w-full items-center justify-center rounded-md border text-lg font-semibold tabular-nums transition-colors",
        cellClass(entry, dateKey),
        (isPending || readOnly) && "pointer-events-none",
        isPending && "opacity-70",
      )}
      onClick={onClick}
      role={readOnly ? undefined : "button"}
      tabIndex={readOnly ? undefined : 0}
      title={cellTitle(entry)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onClick();
        }
      }}
    >
      {value}
      {isOpenShift ? (
        <Clock3
          className="absolute bottom-1 right-1 h-3.5 w-3.5 animate-pulse"
          aria-hidden="true"
        />
      ) : null}
      {hasEdit && !readOnly ? (
        <button
          className="absolute right-1 top-1 rounded-sm bg-background/85 p-1 text-foreground opacity-0 shadow-sm transition-opacity hover:bg-background group-hover:opacity-100 focus:opacity-100"
          onClick={(event) => {
            event.stopPropagation();
            onEdit();
          }}
          title="Редактировать смену"
          type="button"
        >
          <Pencil className="h-3.5 w-3.5" aria-hidden="true" />
        </button>
      ) : null}
    </div>
  );
}

function ShiftEditorDialog({
  editingShift,
  form,
  isSaving,
  onCancel,
  onDelete,
  onFormChange,
  onSave,
}: {
  editingShift: EditingShift | null;
  form: ShiftForm;
  isSaving: boolean;
  onCancel: () => void;
  onDelete: () => void;
  onFormChange: (form: ShiftForm) => void;
  onSave: () => void;
}) {
  return (
    <Dialog
      open={Boolean(editingShift)}
      onOpenChange={(open) => {
        if (!open) {
          onCancel();
        }
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Смена курьера</DialogTitle>
          <DialogDescription>
            {editingShift
              ? `${editingShift.courier.full_name} · ${formatShortDate(editingShift.dateKey)}`
              : ""}
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4">
          <div className="grid gap-2">
            <Label>Категория</Label>
            <div className="grid gap-2 sm:grid-cols-2">
              <label className="flex items-center gap-2 rounded-md border p-3">
                <input
                  checked={form.category === "primary"}
                  onChange={() => onFormChange({ ...form, category: "primary" })}
                  type="radio"
                />
                <span className="flex items-center gap-2 text-sm font-medium">
                  <span className="h-3 w-3 rounded-sm bg-teal-300" aria-hidden="true" />1 — Основная
                </span>
              </label>
              <label className="flex items-center gap-2 rounded-md border p-3">
                <input
                  checked={form.category === "secondary"}
                  onChange={() => onFormChange({ ...form, category: "secondary" })}
                  type="radio"
                />
                <span className="flex items-center gap-2 text-sm font-medium">
                  <span className="h-3 w-3 rounded-sm bg-amber-400" aria-hidden="true" />2 —
                  Второстепенная
                </span>
              </label>
            </div>
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <Label className="grid gap-2">
              <span>Время начала</span>
              <Input
                onChange={(event) => onFormChange({ ...form, startTime: event.target.value })}
                type="time"
                value={form.startTime}
              />
            </Label>
            <Label className="grid gap-2">
              <span>Время конца</span>
              <Input
                onChange={(event) => onFormChange({ ...form, endTime: event.target.value })}
                type="time"
                value={form.endTime}
              />
            </Label>
          </div>

          <Label className="grid gap-2">
            <span>Комментарий</span>
            <Textarea
              onChange={(event) => onFormChange({ ...form, comment: event.target.value })}
              placeholder="Опционально"
              value={form.comment}
            />
          </Label>
        </div>

        <DialogFooter className="gap-2 sm:justify-between">
          <Button
            disabled={!editingShift?.entry?.category || isSaving}
            onClick={onDelete}
            type="button"
            variant="outline"
          >
            <Trash2 size={16} aria-hidden="true" />
            Удалить смену
          </Button>
          <div className="flex gap-2">
            <Button onClick={onCancel} type="button" variant="outline">
              Отмена
            </Button>
            <Button
              disabled={isSaving || !form.startTime || !form.endTime}
              onClick={onSave}
              type="button"
            >
              Сохранить
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function LegendDialog({
  onOpenChange,
  open,
}: {
  onOpenChange: (open: boolean) => void;
  open: boolean;
}) {
  const items = [
    ["bg-teal-200 text-teal-950", "План primary без факта"],
    ["bg-amber-300 text-amber-950", "План secondary без факта"],
    ["bg-emerald-500 text-white", "Факт совпал с primary"],
    ["bg-sky-500 text-white", "Факт совпал с secondary"],
    ["bg-rose-500 text-white", "Primary без факта в прошлом"],
    ["bg-slate-400 text-white", "Secondary без факта в прошлом"],
    ["bg-violet-500 text-white", "Помощь без плана"],
  ];
  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Легенда</DialogTitle>
          <DialogDescription>Цвета ячеек графика курьеров</DialogDescription>
        </DialogHeader>
        <div className="grid gap-2">
          {items.map(([className, label]) => (
            <div className="flex items-center gap-3 rounded-md border px-3 py-2" key={label}>
              <span className={cn("h-7 w-7 rounded-md border", className)} aria-hidden="true" />
              <span className="text-sm">{label}</span>
            </div>
          ))}
        </div>
      </DialogContent>
    </Dialog>
  );
}

function CourierDirectoryTab() {
  const queryClient = useQueryClient();
  const [statusFilter, setStatusFilter] = useState<CourierDirectoryStatusFilter>("active");
  const [search, setSearch] = useState("");
  const month = currentMonthKey();
  const listQuery = useQuery({
    queryKey: ["courier-list", statusFilter, month],
    queryFn: () => getCourierList({ ...courierDirectoryListParams(statusFilter), month }),
    staleTime: 30_000,
  });

  const rows = useMemo(() => {
    const query = search.trim().toLocaleLowerCase("ru");
    return [...(listQuery.data?.rows ?? [])].filter((row) => {
      if (!query) {
        return true;
      }
      return (
        row.full_name.toLocaleLowerCase("ru").includes(query) ||
        row.iiko_id.toLocaleLowerCase("ru").includes(query)
      );
    });
  }, [listQuery.data?.rows, search]);

  const summary = listQuery.data?.summary;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Список курьеров"
        description="Справочник сотрудников с должностью Курьер."
        action={
          <Button
            onClick={() => void queryClient.invalidateQueries({ queryKey: ["courier-list"] })}
            title="Обновить"
            variant="outline"
          >
            <RefreshCw size={16} aria-hidden="true" />
            Обновить
          </Button>
        }
      />

      <section className="grid gap-3 sm:grid-cols-4">
        <SummaryCard
          isLoading={listQuery.isLoading}
          title="Всего активных"
          value={summary?.active_total}
        />
        <SummaryCard
          isLoading={listQuery.isLoading}
          title="Курьеров в резерве"
          value={summary?.reserve_total}
        />
        <SummaryCard
          isLoading={listQuery.isLoading}
          title="Уволенных за месяц"
          value={summary?.fired_this_month}
        />
        <SummaryCard
          isLoading={listQuery.isLoading}
          title="Открытая смена сейчас"
          value={summary?.open_shift_now_total}
        />
      </section>

      <section className="flex flex-wrap items-end gap-3 rounded-md border bg-card p-4">
        <div className="grid gap-2">
          <Label>Статус</Label>
          <div className="flex rounded-md border bg-background p-1">
            {(["active", "reserve", "fired", "all"] as const).map((status) => (
              <button
                className={cn(
                  "h-8 rounded-sm px-3 text-sm font-medium transition-colors",
                  statusFilter === status
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:bg-muted hover:text-foreground",
                )}
                key={status}
                onClick={() => setStatusFilter(status)}
                type="button"
              >
                {COURIER_DIRECTORY_STATUS_LABELS[status]}
              </button>
            ))}
          </div>
        </div>

        <Label className="grid min-w-[260px] flex-1 gap-2">
          <span>Поиск по ФИО</span>
          <div className="relative">
            <Search
              className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              className="pl-9"
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Введите имя или iiko ID"
              value={search}
            />
          </div>
        </Label>
      </section>

      <section className="overflow-hidden rounded-md border bg-card">
        {listQuery.isLoading ? (
          <CourierListSkeleton />
        ) : listQuery.isError ? (
          <div className="p-4">
            <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {apiErrorMessage(listQuery.error, "Не удалось загрузить список курьеров")}
            </div>
          </div>
        ) : rows.length === 0 ? (
          <div className="p-4">
            <EmptyState
              icon={<UsersRound size={18} aria-hidden="true" />}
              title="Курьеры не найдены"
              description="Измените фильтры или проверьте синхронизацию сотрудников iiko."
            />
          </div>
        ) : (
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="min-w-[240px]">Курьер</TableHead>
                  <TableHead>iiko ID</TableHead>
                  <TableHead>Статус</TableHead>
                  <TableHead>Открытая смена сейчас</TableHead>
                  <TableHead className="text-right">Смен в месяце (1 / 2)</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((row) => (
                  <TableRow key={row.employee_id}>
                    <TableCell>
                      <div className="font-medium">{row.full_name}</div>
                    </TableCell>
                    <TableCell className="font-mono text-xs">{row.iiko_id}</TableCell>
                    <TableCell>
                      <CourierStatusChip status={row.status} workStatus={row.work_status} />
                    </TableCell>
                    <TableCell>
                      <OpenShiftChip open={row.open_shift_now} />
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {row.primary_shifts_in_month} / {row.secondary_shifts_in_month}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </section>
    </div>
  );
}

function courierDirectoryListParams(statusFilter: CourierDirectoryStatusFilter): {
  status: CourierDepositStatusFilter;
  work_status: CourierListWorkStatusFilter;
} {
  if (statusFilter === "reserve") {
    return { status: "active", work_status: "reserve" };
  }
  if (statusFilter === "active") {
    return { status: "active", work_status: "working" };
  }
  return { status: statusFilter, work_status: "all" };
}

function SummaryCard({
  isLoading,
  title,
  value,
}: {
  isLoading: boolean;
  title: string;
  value: number | undefined;
}) {
  return (
    <Card className="shadow-none">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">{title}</CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-8 w-16" />
        ) : (
          <div className="text-2xl font-semibold tabular-nums">{value ?? 0}</div>
        )}
      </CardContent>
    </Card>
  );
}

function CourierStatusChip({
  status,
  workStatus,
}: {
  status: CourierListRow["status"];
  workStatus: CourierListRow["work_status"];
}) {
  if (status === "active" && workStatus === "reserve") {
    return (
      <span className="inline-flex items-center gap-1 rounded-md border border-amber-200 bg-amber-50 px-2 py-1 text-xs font-medium text-amber-800">
        <Pause className="h-3.5 w-3.5" aria-hidden="true" />В резерве
      </span>
    );
  }
  if (status === "active") {
    return (
      <span className="inline-flex items-center gap-1 rounded-md border border-emerald-200 bg-emerald-50 px-2 py-1 text-xs font-medium text-emerald-800">
        <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
        Работает
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 rounded-md border border-border bg-muted px-2 py-1 text-xs font-medium text-muted-foreground">
      <CircleOff className="h-3.5 w-3.5" aria-hidden="true" />
      Уволен
    </span>
  );
}

function CourierGridStatusChip({
  status,
  workStatus,
}: {
  status: ScheduleCourier["status"];
  workStatus: ScheduleCourier["work_status"];
}) {
  if (status !== "active") {
    return (
      <span className="mt-1 inline-flex h-5 items-center gap-1 rounded-sm border border-border bg-muted px-1.5 text-[11px] font-normal text-muted-foreground">
        <CircleOff className="h-3 w-3" aria-hidden="true" />
        Уволен
      </span>
    );
  }
  if (workStatus !== "reserve") {
    return null;
  }
  return (
    <span className="mt-1 inline-flex h-5 items-center gap-1 rounded-sm border border-border bg-muted px-1.5 text-[11px] font-normal text-muted-foreground">
      <Pause className="h-3 w-3" aria-hidden="true" />В резерве
    </span>
  );
}

function OpenShiftChip({ open }: { open: boolean }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs font-medium",
        open
          ? "border-emerald-200 bg-emerald-50 text-emerald-800"
          : "border-border bg-muted text-muted-foreground",
      )}
    >
      {open ? (
        <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
      ) : (
        <CircleOff className="h-3.5 w-3.5" aria-hidden="true" />
      )}
      {open ? "Открыта" : "Нет"}
    </span>
  );
}

function MatrixSkeleton() {
  return (
    <div className="space-y-2 p-4">
      {Array.from({ length: 8 }).map((_, index) => (
        <div className="grid grid-cols-[220px_repeat(7,1fr)] gap-2" key={index}>
          <Skeleton className="h-14" />
          {Array.from({ length: 7 }).map((__, dayIndex) => (
            <Skeleton className="h-14" key={dayIndex} />
          ))}
        </div>
      ))}
    </div>
  );
}

function CourierListSkeleton() {
  return (
    <div className="space-y-3 p-4">
      {Array.from({ length: 6 }).map((_, index) => (
        <div className="grid grid-cols-[2fr_1fr_1fr_1fr_1fr] gap-3" key={index}>
          <Skeleton className="h-10" />
          <Skeleton className="h-10" />
          <Skeleton className="h-10" />
          <Skeleton className="h-10" />
          <Skeleton className="h-10" />
        </div>
      ))}
    </div>
  );
}

async function invalidateCourierSchedule(queryClient: ReturnType<typeof useQueryClient>) {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: ["courier-schedule"] }),
    queryClient.invalidateQueries({ queryKey: ["courier-schedule-matched"] }),
    queryClient.invalidateQueries({ queryKey: ["courier-statistics"] }),
    queryClient.invalidateQueries({ queryKey: ["courier-list"] }),
  ]);
}

function indexEntries(entries: CourierScheduleMatchedEntry[]) {
  return new Map(
    entries.map((entry) => [cellKey(entry.courier_employee_id, entry.work_date), entry]),
  );
}

function courierListRowToScheduleCourier(row: CourierListRow): ScheduleCourier {
  return {
    id: row.employee_id,
    full_name: row.full_name,
    iiko_id: row.iiko_id,
    status: row.status,
    work_status: row.work_status,
  };
}

function sortScheduleCouriers(
  couriers: ScheduleCourier[],
  entries: CourierScheduleMatchedEntry[],
  weekDateKeys: string[],
) {
  const weekDates = new Set(weekDateKeys);
  const entriesByCourier = new Map<string, CourierScheduleMatchedEntry[]>();
  for (const entry of entries) {
    if (!weekDates.has(entry.work_date)) {
      continue;
    }
    const courierEntries = entriesByCourier.get(entry.courier_employee_id) ?? [];
    courierEntries.push(entry);
    entriesByCourier.set(entry.courier_employee_id, courierEntries);
  }

  return [...couriers].sort((left, right) =>
    compareSchedulePriority(
      schedulePriority(left, entriesByCourier.get(left.id) ?? []),
      schedulePriority(right, entriesByCourier.get(right.id) ?? []),
    ),
  );
}

type SchedulePriority = {
  group: 1 | 2 | 3;
  nearestDate: string;
  workRank: number;
  fullName: string;
  id: string;
};

function schedulePriority(
  courier: ScheduleCourier,
  entries: CourierScheduleMatchedEntry[],
): SchedulePriority {
  const nearestPrimary = nearestEntryDate(entries, "primary");
  if (nearestPrimary) {
    return priorityPayload(courier, 1, nearestPrimary);
  }
  const nearestSecondary = nearestEntryDate(entries, "secondary");
  if (nearestSecondary) {
    return priorityPayload(courier, 2, nearestSecondary);
  }
  return priorityPayload(courier, 3, "");
}

function priorityPayload(
  courier: ScheduleCourier,
  group: SchedulePriority["group"],
  nearestDate: string,
): SchedulePriority {
  return {
    group,
    nearestDate,
    workRank: courierWorkRank(courier),
    fullName: courier.full_name,
    id: courier.id,
  };
}

function nearestEntryDate(
  entries: CourierScheduleMatchedEntry[],
  category: CourierScheduleCategory,
) {
  let nearest: string | null = null;
  for (const entry of entries) {
    if (entry.category !== category) {
      continue;
    }
    if (nearest === null || entry.work_date < nearest) {
      nearest = entry.work_date;
    }
  }
  return nearest;
}

function courierWorkRank(courier: ScheduleCourier) {
  if (courier.status !== "active") {
    return 2;
  }
  return courier.work_status === "reserve" ? 1 : 0;
}

function compareSchedulePriority(left: SchedulePriority, right: SchedulePriority) {
  if (left.group !== right.group) {
    return left.group - right.group;
  }
  if (left.nearestDate !== right.nearestDate) {
    return left.nearestDate.localeCompare(right.nearestDate);
  }
  if (left.group === 3 && left.workRank !== right.workRank) {
    return left.workRank - right.workRank;
  }
  return left.fullName.localeCompare(right.fullName, "ru") || left.id.localeCompare(right.id);
}

function cellKey(courierId: string, dateKey: string) {
  return `${courierId}:${dateKey}`;
}

function cellValue(entry: CourierScheduleMatchedEntry | null) {
  if (!entry) {
    return "";
  }
  if (entry.category === "primary") {
    return "1";
  }
  if (entry.category === "secondary") {
    return "2";
  }
  if (entry.status === "helping") {
    return <Star className="h-5 w-5 fill-current" aria-hidden="true" />;
  }
  if (entry.status === "not_counted") {
    return "·";
  }
  return "";
}

function cellClass(entry: CourierScheduleMatchedEntry | null, dateKey: string) {
  if (!entry) {
    return "border-border bg-background text-muted-foreground hover:bg-muted/60";
  }
  if (entry.status === "helping") {
    return "border-violet-600 bg-violet-500 text-white hover:bg-violet-600";
  }
  if (entry.status === "not_counted") {
    return "border-slate-200 bg-slate-100 text-slate-500 hover:bg-slate-200";
  }
  if (entry.status === "matched_primary") {
    return "border-emerald-600 bg-emerald-500 text-white hover:bg-emerald-600";
  }
  if (entry.status === "matched_secondary") {
    return "border-sky-600 bg-sky-500 text-white hover:bg-sky-600";
  }
  if (entry.status === "no_show_primary" || entry.status === "short_primary") {
    return "border-rose-600 bg-rose-500 text-white hover:bg-rose-600";
  }
  if (entry.status === "no_show_secondary" || entry.status === "short_secondary") {
    return "border-slate-500 bg-slate-400 text-white hover:bg-slate-500";
  }
  if (entry.category === "primary") {
    return isPastDateKey(dateKey)
      ? "border-rose-600 bg-rose-500 text-white hover:bg-rose-600"
      : "border-teal-300 bg-teal-200 text-teal-950 hover:bg-teal-300";
  }
  if (entry.category === "secondary") {
    return isPastDateKey(dateKey)
      ? "border-slate-500 bg-slate-400 text-white hover:bg-slate-500"
      : "border-amber-400 bg-amber-300 text-amber-950 hover:bg-amber-400";
  }
  return "border-border bg-background text-muted-foreground hover:bg-muted/60";
}

function cellTitle(entry: CourierScheduleMatchedEntry | null) {
  if (!entry) {
    return "";
  }
  if (entry.status === "helping") {
    return "Помощь без плана: есть iiko-смена, плана на день нет";
  }
  if (entry.status === "not_counted") {
    return "Факт без плана и без доставок";
  }
  return entry.comment || "";
}

function formFromEntry(
  entry: CourierScheduleMatchedEntry | null,
  fallbackCategory: CourierScheduleCategory,
): ShiftForm {
  return {
    category: entry?.category ?? fallbackCategory,
    startTime: timeValue(entry?.planned_start_at) ?? DEFAULT_START_TIME,
    endTime: timeValue(entry?.planned_end_at) ?? DEFAULT_END_TIME,
    comment: entry?.comment ?? "",
  };
}

function emptyShiftForm(category: CourierScheduleCategory): ShiftForm {
  return {
    category,
    startTime: DEFAULT_START_TIME,
    endTime: DEFAULT_END_TIME,
    comment: "",
  };
}

function timeValue(value: string | null | undefined) {
  if (!value || value.length < 16) {
    return null;
  }
  return value.slice(11, 16);
}

function dateTimeWithMoscowOffset(dateKey: string, timeValue: string) {
  return `${dateKey}T${timeValue}:00+03:00`;
}

function startOfMondayWeek(value: Date) {
  const date = new Date(value.getFullYear(), value.getMonth(), value.getDate());
  const day = date.getDay();
  const offset = day === 0 ? -6 : 1 - day;
  return addDays(date, offset);
}

function addDays(value: Date, days: number) {
  const date = new Date(value);
  date.setDate(date.getDate() + days);
  return date;
}

function toDateKey(value: Date) {
  return [
    value.getFullYear(),
    String(value.getMonth() + 1).padStart(2, "0"),
    String(value.getDate()).padStart(2, "0"),
  ].join("-");
}

function isPastDateKey(value: string) {
  return value < toDateKey(new Date());
}

function weekLabel(weekStart: Date) {
  const weekEnd = addDays(weekStart, 6);
  return `Неделя ${isoWeek(weekStart)} (${dateRangeShort(weekStart, weekEnd)})`;
}

function isoWeek(value: Date) {
  const date = new Date(Date.UTC(value.getFullYear(), value.getMonth(), value.getDate()));
  const day = date.getUTCDay() || 7;
  date.setUTCDate(date.getUTCDate() + 4 - day);
  const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
  return Math.ceil(((date.getTime() - yearStart.getTime()) / 86_400_000 + 1) / 7);
}

function dateRangeShort(start: Date, end: Date) {
  const startMonth = monthName(start);
  const endMonth = monthName(end);
  if (startMonth === endMonth) {
    return `${start.getDate()}–${end.getDate()} ${endMonth}`;
  }
  return `${start.getDate()} ${startMonth}–${end.getDate()} ${endMonth}`;
}

function monthName(value: Date) {
  return new Intl.DateTimeFormat("ru-RU", { month: "long" }).format(value);
}

function formatShortDate(value: string) {
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  }).format(new Date(`${value}T00:00:00`));
}

function isCourierScheduleTab(value: string): value is CourierScheduleActiveTab {
  return value === "grid" || value === "list";
}

function courierScheduleTabPath(value: CourierScheduleActiveTab) {
  return `/couriers/schedule/${value}`;
}
