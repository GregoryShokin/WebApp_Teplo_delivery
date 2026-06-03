import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CalendarDays, LoaderCircle, Plus, RefreshCw } from "lucide-react";
import { useEffect, useState, type Dispatch, type ReactNode, type SetStateAction } from "react";
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
  apiErrorStatus,
  cancelVacationPeriod,
  createVacationPeriod,
  getVacationRoster,
  patchVacationPeriod,
  type VacationConflictResponse,
  type VacationPeriodRead,
  type VacationRosterRow,
} from "@/lib/api";
import { cn } from "@/lib/utils";

type VacationsRouteProps = {
  embedded?: boolean;
  headerSlot?: ReactNode;
};

type VacationDialogState = {
  mode: "create" | "edit";
  period: VacationPeriodRead | null;
  employeeId: string;
  dateStart: string;
  dateEnd: string;
  comment: string;
  conflict: VacationConflictResponse | null;
};

type VacationValidationPeriod = {
  id: string;
  date_start: string;
  date_end: string;
};

const VACATION_MAX_PERIOD_DAYS = 10;
const VACATION_MIN_GAP_MONTHS = 2;
const VACATION_YEAR_STORAGE_KEY = "shifts.vacations.year";

export function VacationsRoute({ embedded = false, headerSlot }: VacationsRouteProps) {
  const queryClient = useQueryClient();
  const [year, setYear] = useState(readStoredVacationYear);
  const [vacationDialog, setVacationDialog] = useState<VacationDialogState | null>(null);

  useEffect(() => {
    window.localStorage.setItem(VACATION_YEAR_STORAGE_KEY, String(year));
  }, [year]);

  const vacationRosterQuery = useQuery({
    queryKey: ["vacations-roster", year],
    queryFn: () => getVacationRoster(year),
  });

  const saveVacationMutation = useMutation({
    mutationFn: (variables: { state: VacationDialogState; forceRemoveShifts?: boolean }) => {
      if (variables.state.mode === "edit" && variables.state.period) {
        return patchVacationPeriod(variables.state.period.id, {
          date_start: variables.state.dateStart,
          date_end: variables.state.dateEnd,
          comment: variables.state.comment.trim() || null,
          force_remove_conflicting_shifts: Boolean(variables.forceRemoveShifts),
        });
      }
      return createVacationPeriod({
        employee_id: variables.state.employeeId,
        date_start: variables.state.dateStart,
        date_end: variables.state.dateEnd,
        comment: variables.state.comment.trim() || null,
        force_remove_conflicting_shifts: Boolean(variables.forceRemoveShifts),
      });
    },
    onSuccess: async () => {
      toast.success("Отпуск сохранён");
      setVacationDialog(null);
      await invalidateVacationData();
    },
    onError: (error) => {
      const conflict = vacationConflictFromError(error);
      if (conflict) {
        setVacationDialog((current) => (current ? { ...current, conflict } : current));
        return;
      }
      toast.error(apiErrorMessage(error, "Не удалось сохранить отпуск"));
    },
  });

  const cancelVacationMutation = useMutation({
    mutationFn: (periodId: string) => cancelVacationPeriod(periodId),
    onSuccess: async () => {
      toast.success("Отпуск отменён");
      setVacationDialog(null);
      await invalidateVacationData();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось отменить отпуск")),
  });

  async function invalidateVacationData() {
    await queryClient.invalidateQueries({ queryKey: ["vacations-roster"] });
    await queryClient.invalidateQueries({ queryKey: ["schedule"] });
    await queryClient.invalidateQueries({ queryKey: ["schedules"] });
  }

  function handleYearChange(value: string) {
    const nextYear = Number(value);
    if (!Number.isInteger(nextYear) || nextYear < 2000 || nextYear > 2100) {
      return;
    }
    setYear(nextYear);
  }

  function openVacationDialog(employee?: VacationRosterRow, period?: VacationPeriodRead) {
    const today = toIsoDate(new Date());
    setVacationDialog({
      mode: period ? "edit" : "create",
      period: period ?? null,
      employeeId: employee?.employee_id ?? "",
      dateStart: period?.date_start ?? today,
      dateEnd: period?.date_end ?? today,
      comment: period?.comment ?? "",
      conflict: null,
    });
  }

  function submitVacationDialog(forceRemoveShifts = false) {
    if (!vacationDialog) {
      return;
    }
    const validationError = vacationDialogValidationError(
      vacationDialog,
      vacationRosterQuery.data ?? [],
    );
    if (validationError) {
      toast.error(validationError);
      return;
    }
    saveVacationMutation.mutate({ state: vacationDialog, forceRemoveShifts });
  }

  const yearControl = (
    <Label className="grid gap-1 text-sm">
      <span className="text-xs font-medium text-muted-foreground">Год</span>
      <Input
        className="w-[112px]"
        max={2100}
        min={2000}
        onChange={(event) => handleYearChange(event.target.value)}
        type="number"
        value={year}
      />
    </Label>
  );

  return (
    <div className="space-y-5">
      {embedded ? (
        <div className="flex justify-end">{yearControl}</div>
      ) : (
        <>
          <PageHeader
            title="Учёт смен — Отпуска"
            description="Планирование отпусков для активных поваров и кассиров."
            action={yearControl}
          />

          {headerSlot}
        </>
      )}

      <VacationsView
        errorMessage={
          vacationRosterQuery.isError
            ? apiErrorMessage(vacationRosterQuery.error, "Не удалось загрузить отпуска")
            : null
        }
        isError={vacationRosterQuery.isError}
        isLoading={vacationRosterQuery.isLoading}
        onCreate={() => openVacationDialog()}
        onAdd={openVacationDialog}
        onEdit={openVacationDialog}
        onRetry={() => vacationRosterQuery.refetch()}
        rows={vacationRosterQuery.data ?? []}
        year={year}
      />

      <VacationDialog
        employees={vacationRosterQuery.data ?? []}
        isCancelling={cancelVacationMutation.isPending}
        isSaving={saveVacationMutation.isPending}
        onCancelPeriod={(period) => cancelVacationMutation.mutate(period.id)}
        onOpenChange={(open) => {
          if (!open) {
            setVacationDialog(null);
          }
        }}
        onSubmit={() => submitVacationDialog(false)}
        setValue={setVacationDialog}
        state={vacationDialog}
      />

      <AlertDialog
        open={Boolean(vacationDialog?.conflict)}
        onOpenChange={(open) =>
          setVacationDialog((current) =>
            current ? { ...current, conflict: open ? current.conflict : null } : current,
          )
        }
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Есть смены в дни отпуска</AlertDialogTitle>
            <AlertDialogDescription>
              {vacationDialog?.conflict
                ? `Найдено смен: ${vacationDialog.conflict.conflicting_shifts.length}. Можно удалить смены из черновиков и сохранить отпуск. Опубликованный график нужно сначала открыть новой версией.`
                : ""}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel
              disabled={saveVacationMutation.isPending}
              onClick={() =>
                setVacationDialog((current) => (current ? { ...current, conflict: null } : current))
              }
              type="button"
            >
              Отмена
            </AlertDialogCancel>
            <AlertDialogAction
              disabled={saveVacationMutation.isPending}
              onClick={(event) => {
                event.preventDefault();
                setVacationDialog((current) =>
                  current ? { ...current, conflict: null } : current,
                );
                submitVacationDialog(true);
              }}
              type="button"
            >
              Удалить смены
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function VacationsView({
  errorMessage,
  isError,
  isLoading,
  onCreate,
  onAdd,
  onEdit,
  onRetry,
  rows,
  year,
}: {
  errorMessage: string | null;
  isError: boolean;
  isLoading: boolean;
  onCreate: () => void;
  onAdd: (employee: VacationRosterRow) => void;
  onEdit: (employee: VacationRosterRow, period: VacationPeriodRead) => void;
  onRetry: () => void;
  rows: VacationRosterRow[];
  year: number;
}) {
  if (isLoading) {
    return (
      <section className="rounded-lg border bg-card p-4">
        <Skeleton className="h-8 w-60" />
        <div className="mt-4 space-y-2">
          {Array.from({ length: 6 }).map((_, index) => (
            <Skeleton className="h-16 w-full" key={index} />
          ))}
        </div>
      </section>
    );
  }

  if (isError) {
    return (
      <EmptyState
        icon={<AlertTriangle className="h-5 w-5" aria-hidden="true" />}
        title="Отпуска не загрузились"
        description={errorMessage ?? "Не удалось получить список сотрудников для отпусков."}
        action={
          <Button onClick={onRetry} size="sm" type="button" variant="outline">
            <RefreshCw size={15} aria-hidden="true" />
            Повторить
          </Button>
        }
      />
    );
  }

  if (rows.length === 0) {
    return (
      <EmptyState
        icon={<CalendarDays className="h-5 w-5" aria-hidden="true" />}
        title="Нет сотрудников для отпусков"
        description="Отпуска доступны для активных поваров и кассиров."
        action={
          <Button disabled size="sm" type="button" variant="outline">
            <Plus size={15} aria-hidden="true" />
            Новый отпуск
          </Button>
        }
      />
    );
  }

  return (
    <section className="overflow-hidden rounded-lg border bg-card">
      <div className="flex flex-col gap-2 border-b px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="font-medium">Отпуска {year}</div>
          <div className="mt-1 text-sm text-muted-foreground">
            Лимит считается по календарному году, без переноса остатка.
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant="outline">{rows.length} сотрудников</Badge>
          <Button onClick={onCreate} size="sm" type="button">
            <Plus size={15} aria-hidden="true" />
            Новый отпуск
          </Button>
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[880px] text-sm">
          <thead>
            <tr className="border-b bg-muted/50 text-left text-muted-foreground">
              <th className="px-4 py-3 font-medium">Сотрудник</th>
              <th className="px-4 py-3 text-right font-medium">Использовано</th>
              <th className="px-4 py-3 text-right font-medium">Остаток</th>
              <th className="px-4 py-3 font-medium">Периоды</th>
              <th className="px-4 py-3 text-right font-medium">Действие</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr className="border-b last:border-b-0 hover:bg-muted/30" key={row.employee_id}>
                <td className="px-4 py-3">
                  <div className="font-medium">{row.employee_full_name}</div>
                  <div className="mt-1 text-xs text-muted-foreground">{row.position}</div>
                </td>
                <td className="px-4 py-3 text-right tabular-nums">
                  {row.used} из {row.limit}
                </td>
                <td className="px-4 py-3 text-right tabular-nums">
                  <span className={cn(row.remaining <= 3 && "text-amber-700")}>
                    {row.remaining}
                  </span>
                </td>
                <td className="px-4 py-3">
                  {row.periods.length > 0 ? (
                    <div className="flex flex-wrap gap-2">
                      {row.periods.map((period) => (
                        <button
                          className="rounded-md border bg-background px-2 py-1 text-left text-xs hover:bg-muted"
                          key={period.id}
                          onClick={() => onEdit(row, period)}
                          type="button"
                        >
                          <span className="font-medium">
                            {formatShortRange(period.date_start, period.date_end)}
                          </span>
                          <span className="ml-2 text-muted-foreground">
                            {period.days_count} дн.
                          </span>
                          <VacationStatusBadge status={period.status} />
                        </button>
                      ))}
                    </div>
                  ) : (
                    <span className="text-muted-foreground">Периодов нет</span>
                  )}
                </td>
                <td className="px-4 py-3 text-right">
                  <Button onClick={() => onAdd(row)} size="sm" type="button" variant="outline">
                    <Plus size={15} aria-hidden="true" />
                    Добавить
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function VacationStatusBadge({ status }: { status: VacationPeriodRead["status"] }) {
  const label = status === "paid" ? "оплачен" : status === "cancelled" ? "отменён" : "план";
  return (
    <span className="ml-2 rounded-sm border border-muted-foreground/30 px-1 text-[10px] uppercase text-muted-foreground">
      {label}
    </span>
  );
}

function VacationDialog({
  employees,
  isCancelling,
  isSaving,
  onCancelPeriod,
  onOpenChange,
  onSubmit,
  setValue,
  state,
}: {
  employees: VacationRosterRow[];
  isCancelling: boolean;
  isSaving: boolean;
  onCancelPeriod: (period: VacationPeriodRead) => void;
  onOpenChange: (open: boolean) => void;
  onSubmit: () => void;
  setValue: Dispatch<SetStateAction<VacationDialogState | null>>;
  state: VacationDialogState | null;
}) {
  const selectedEmployee = employees.find((employee) => employee.employee_id === state?.employeeId);
  const validationError = vacationDialogValidationError(state, employees);
  const dateEndMax = state?.dateStart
    ? toIsoDate(addDays(parseIsoDate(state.dateStart), VACATION_MAX_PERIOD_DAYS - 1))
    : undefined;
  const selectedDaysCount =
    state?.dateStart && state.dateEnd && state.dateEnd >= state.dateStart
      ? vacationDaysCount(state.dateStart, state.dateEnd)
      : null;

  function patchState(patch: Partial<VacationDialogState>) {
    setValue((current) => (current ? { ...current, ...patch, conflict: null } : current));
  }

  return (
    <Dialog open={Boolean(state)} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {state?.mode === "edit" ? "Редактировать отпуск" : "Новый отпуск"}
          </DialogTitle>
          <DialogDescription>
            {selectedEmployee
              ? `${selectedEmployee.employee_full_name} · остаток ${selectedEmployee.remaining} дн.`
              : "Выберите сотрудника и период отпуска."}
          </DialogDescription>
        </DialogHeader>
        {state ? (
          <div className="grid gap-4">
            <div className="grid gap-2">
              <Label>Сотрудник</Label>
              <Select
                disabled={state.mode === "edit"}
                onValueChange={(value) => patchState({ employeeId: value })}
                value={state.employeeId || undefined}
              >
                <SelectTrigger>
                  <SelectValue placeholder="Выберите сотрудника" />
                </SelectTrigger>
                <SelectContent>
                  {employees.map((employee) => (
                    <SelectItem key={employee.employee_id} value={employee.employee_id}>
                      {employee.employee_full_name} · остаток {employee.remaining} дн.
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="grid gap-2">
                <Label htmlFor="vacation-date-start">С</Label>
                <Input
                  id="vacation-date-start"
                  onChange={(event) => patchState({ dateStart: event.target.value })}
                  type="date"
                  value={state.dateStart}
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="vacation-date-end">По</Label>
                <Input
                  id="vacation-date-end"
                  max={dateEndMax}
                  min={state.dateStart || undefined}
                  onChange={(event) => patchState({ dateEnd: event.target.value })}
                  type="date"
                  value={state.dateEnd}
                />
              </div>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="vacation-comment">Комментарий</Label>
              <Textarea
                id="vacation-comment"
                onChange={(event) => patchState({ comment: event.target.value })}
                value={state.comment}
              />
            </div>
            <div
              className={cn(
                "rounded-md border bg-muted/30 px-3 py-2 text-sm",
                validationError && "border-destructive/40 bg-destructive/5 text-destructive",
              )}
            >
              <div>
                {selectedDaysCount !== null
                  ? `${selectedDaysCount} дн. · ${formatShortRange(state.dateStart, state.dateEnd)}`
                  : "Выберите период отпуска"}
              </div>
              <div className="mt-1 text-xs">
                Максимум {VACATION_MAX_PERIOD_DAYS} дней за один отпуск. Следующий отпуск — через{" "}
                {VACATION_MIN_GAP_MONTHS} месяца после предыдущего.
              </div>
              {validationError ? (
                <div className="mt-1 text-xs font-medium">{validationError}</div>
              ) : null}
            </div>
          </div>
        ) : null}
        <DialogFooter className="gap-2 sm:justify-between sm:space-x-0">
          <div>
            {state?.period && state.period.status !== "cancelled" ? (
              <Button
                disabled={isSaving || isCancelling}
                onClick={() => state.period && onCancelPeriod(state.period)}
                type="button"
                variant="outline"
              >
                Отменить отпуск
              </Button>
            ) : null}
          </div>
          <div className="flex flex-col-reverse gap-2 sm:flex-row">
            <Button
              disabled={isSaving || isCancelling}
              onClick={() => onOpenChange(false)}
              variant="outline"
            >
              Закрыть
            </Button>
            <Button
              disabled={isSaving || isCancelling || Boolean(validationError)}
              onClick={onSubmit}
            >
              {isSaving ? <LoaderCircle className="animate-spin" size={16} /> : null}
              Сохранить
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function vacationConflictFromError(error: unknown): VacationConflictResponse | null {
  if (apiErrorStatus(error) !== 409) {
    return null;
  }
  const data = (error as { response?: { data?: unknown } }).response?.data;
  if (!isRecord(data) || !Array.isArray(data.conflicting_shifts)) {
    return null;
  }
  return {
    detail: String(data.detail ?? "На период отпуска уже есть смены"),
    conflicting_shifts: data.conflicting_shifts.filter(isRecord).map((item) => ({
      shift_id: String(item.shift_id ?? ""),
      business_date: String(item.business_date ?? ""),
      schedule_id: String(item.schedule_id ?? ""),
      schedule_status: String(item.schedule_status ?? ""),
    })),
  };
}

function vacationDialogValidationError(
  state: VacationDialogState | null,
  employees: VacationRosterRow[],
) {
  if (!state) {
    return null;
  }
  if (!state.employeeId) {
    return "Выберите сотрудника";
  }
  if (!state.dateStart || !state.dateEnd) {
    return "Выберите даты отпуска";
  }
  if (state.dateEnd < state.dateStart) {
    return "Дата окончания не может быть раньше даты начала";
  }
  const daysCount = vacationDaysCount(state.dateStart, state.dateEnd);
  if (daysCount > VACATION_MAX_PERIOD_DAYS) {
    return `Один отпуск можно оформить максимум на ${VACATION_MAX_PERIOD_DAYS} дней.`;
  }

  const employee = employees.find((item) => item.employee_id === state.employeeId);
  if (!employee) {
    return "Сотрудник не найден в списке отпусков";
  }

  const candidateId = state.period?.id ?? "__new_vacation_period__";
  const candidate: VacationValidationPeriod = {
    id: candidateId,
    date_start: state.dateStart,
    date_end: state.dateEnd,
  };
  const periods: VacationValidationPeriod[] = employee.periods
    .filter((period) => period.status !== "cancelled" && period.id !== state.period?.id)
    .map((period) => ({
      id: period.id,
      date_start: period.date_start,
      date_end: period.date_end,
    }));
  const ordered = [...periods, candidate].sort((left, right) =>
    left.date_start.localeCompare(right.date_start),
  );

  for (let index = 1; index < ordered.length; index += 1) {
    const previous = ordered[index - 1];
    const next = ordered[index];
    if (previous.id !== candidateId && next.id !== candidateId) {
      continue;
    }
    const nextAllowedStart = toIsoDate(
      addMonths(parseIsoDate(previous.date_end), VACATION_MIN_GAP_MONTHS),
    );
    if (next.date_start < nextAllowedStart) {
      if (next.id === candidateId) {
        return `Следующий отпуск можно начать не раньше ${formatDate(nextAllowedStart)}.`;
      }
      return `До следующего отпуска ${formatDate(
        next.date_start,
      )} должно пройти ${VACATION_MIN_GAP_MONTHS} месяца.`;
    }
  }

  return null;
}

function vacationDaysCount(dateStart: string, dateEnd: string) {
  return eachIsoDate(dateStart, dateEnd).length;
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

function addDays(value: Date, days: number) {
  const date = new Date(value);
  date.setDate(date.getDate() + days);
  return date;
}

function addMonths(value: Date, months: number) {
  const targetMonthIndex = value.getMonth() + months;
  const targetYear = value.getFullYear() + Math.floor(targetMonthIndex / 12);
  const targetMonth = ((targetMonthIndex % 12) + 12) % 12;
  const daysInTargetMonth = new Date(targetYear, targetMonth + 1, 0).getDate();
  return new Date(targetYear, targetMonth, Math.min(value.getDate(), daysInTargetMonth));
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

function formatShortRange(start: string, end: string) {
  return `${formatShortDate(start)} — ${formatShortDate(end)}`;
}

function formatShortDate(value: string) {
  const [, month, day] = value.split("-");
  return `${day}.${month}`;
}

function readStoredVacationYear() {
  const stored = Number(window.localStorage.getItem(VACATION_YEAR_STORAGE_KEY));
  if (Number.isInteger(stored) && stored >= 2000 && stored <= 2100) {
    return stored;
  }
  return new Date().getFullYear();
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
