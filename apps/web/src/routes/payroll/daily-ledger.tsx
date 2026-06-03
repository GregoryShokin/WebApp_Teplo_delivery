import axios from "axios";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  LoaderCircle,
  Lock,
  RefreshCw,
} from "lucide-react";
import { Fragment, useEffect, useMemo, useState, type ReactNode } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
import { EmptyState } from "@/components/ui-app/EmptyState";
import { PageHeader } from "@/components/ui-app/PageHeader";
import {
  buildShiftLedgerWeek,
  getShiftLedgerMatrix,
  patchShiftLedgerEntry,
  apiErrorMessage,
  type ShiftLedgerAvailableRole,
  type ShiftLedgerEntry,
  type ShiftLedgerMatrix,
  type ShiftLedgerMatrixDay,
  type ShiftLedgerMatrixEmployee,
  type ShiftLedgerMatrixShift,
} from "@/lib/api";
import { PAYROLL_ROLE_LABELS } from "@/lib/i18n/employee";
import { cn } from "@/lib/utils";

type SaveRoleVariables = {
  queryKey: readonly ["shift-ledger-matrix", string];
  shift: ShiftLedgerMatrixShift;
  payrollRole: string;
};

type DayHeader = ShiftLedgerMatrix["days"][number];

type PayrollDailyLedgerRouteProps = {
  embedded?: boolean;
  headerSlot?: ReactNode;
};

const COLLAPSED_TIMES_STORAGE_KEY = "daily-ledger-times-collapsed-days";
const ROLE_COLUMN_WIDTH = 154;
const TIME_COLUMN_WIDTH = 88;
const EMPLOYEE_COLUMN_WIDTH = 240;
const ROLE_COLUMN_CLASS = "w-[154px] min-w-[154px]";
const TIME_COLUMN_CLASS = "w-[88px] min-w-[88px]";
const EMPLOYEE_COLUMN_CLASS = "w-[240px] min-w-[240px]";

export function PayrollDailyLedgerRoute({
  embedded = false,
  headerSlot,
}: PayrollDailyLedgerRouteProps = {}) {
  const queryClient = useQueryClient();
  const maxDate = todayIsoDate();
  const [selectedDate, setSelectedDate] = useState(maxDate);
  const [expandedEmployees, setExpandedEmployees] = useState<Record<string, boolean>>({});
  const [roleOverrides, setRoleOverrides] = useState<Record<string, string>>({});
  const [savingIds, setSavingIds] = useState<Record<string, boolean>>({});
  const [collapsedTimeDays, setCollapsedTimeDays] = useState<string[] | null>(
    readCollapsedTimeDays,
  );

  const matrixQueryKey = useMemo(
    () => ["shift-ledger-matrix", selectedDate] as const,
    [selectedDate],
  );

  const matrixQuery = useQuery({
    queryKey: matrixQueryKey,
    queryFn: () => getShiftLedgerMatrix(selectedDate),
  });

  const fallbackDays = useMemo(() => buildWeekHeaders(selectedDate), [selectedDate]);
  const days = matrixQuery.data?.days ?? fallbackDays;
  const collapsedTimeDayDates = useMemo(
    () => new Set(collapsedTimeDays ?? days.map((day) => day.date)),
    [collapsedTimeDays, days],
  );
  const areAllCurrentTimesCollapsed =
    days.length > 0 && days.every((day) => collapsedTimeDayDates.has(day.date));
  const employees = useMemo(
    () => [...(matrixQuery.data?.employees ?? [])].sort(compareEmployees),
    [matrixQuery.data?.employees],
  );

  useEffect(() => {
    if (collapsedTimeDays === null && matrixQuery.data?.days.length) {
      setCollapsedTimeDays(matrixQuery.data.days.map((day) => day.date));
    }
  }, [collapsedTimeDays, matrixQuery.data?.days]);

  useEffect(() => {
    if (collapsedTimeDays !== null) {
      persistCollapsedTimeDays(collapsedTimeDays);
    }
  }, [collapsedTimeDays]);

  useEffect(() => {
    setExpandedEmployees({});
    setRoleOverrides({});
    setSavingIds({});
  }, [selectedDate]);

  const buildMutation = useMutation({
    mutationFn: () => buildShiftLedgerWeek(selectedDate),
    onSuccess: (matrix) => {
      queryClient.setQueryData(matrixQueryKey, matrix);
      toast.success("Неделя обновлена из iiko");
    },
    onError: (error) => toast.error((error as Error).message),
  });

  const roleMutation = useMutation({
    mutationFn: ({ shift, payrollRole }: SaveRoleVariables) =>
      patchShiftLedgerEntry(shift.ledger_entry_id, { payroll_role: payrollRole }),
    onMutate: ({ shift, payrollRole }) => {
      setRoleOverride(shift.ledger_entry_id, payrollRole);
      setSaving(shift.ledger_entry_id, true);
    },
    onSuccess: (entry, variables) => {
      queryClient.setQueryData<ShiftLedgerMatrix>(variables.queryKey, (current) =>
        current ? updateShiftInMatrix(current, entry) : current,
      );
      clearRoleOverride(variables.shift.ledger_entry_id);
      toast.success("Роль сохранена");
    },
    onError: (error, variables) => {
      clearRoleOverride(variables.shift.ledger_entry_id);
      toast.error(
        isPayrollLockedError(error) ? "ЗП за эту неделю закрыта" : apiErrorMessage(error),
      );
    },
    onSettled: (_entry, _error, variables) => {
      if (variables) {
        setSaving(variables.shift.ledger_entry_id, false);
      }
    },
  });

  function handleDateChange(value: string) {
    if (!value) {
      return;
    }
    if (value > maxDate) {
      setSelectedDate(maxDate);
      toast.warning("Будущие даты недоступны");
      return;
    }
    setSelectedDate(value);
  }

  function saveRole(shift: ShiftLedgerMatrixShift, payrollRole: string) {
    if (shift.payroll_locked) {
      return;
    }
    const currentRole = roleValue(shift, roleOverrides);
    if (currentRole === payrollRole || savingIds[shift.ledger_entry_id]) {
      return;
    }
    roleMutation.mutate({
      queryKey: matrixQueryKey,
      shift,
      payrollRole,
    });
  }

  function toggleDay(date: string) {
    setCollapsedTimeDays((current) => {
      const next = new Set(current ?? days.map((day) => day.date));
      if (next.has(date)) {
        next.delete(date);
      } else {
        next.add(date);
      }
      return sortIsoDates([...next]);
    });
  }

  function toggleAllCurrentDays() {
    setCollapsedTimeDays((current) => {
      const next = new Set(current ?? days.map((day) => day.date));
      const currentWeekDates = days.map((day) => day.date);
      const shouldExpandAll = currentWeekDates.every((date) => next.has(date));

      currentWeekDates.forEach((date) => {
        if (shouldExpandAll) {
          next.delete(date);
        } else {
          next.add(date);
        }
      });

      return sortIsoDates([...next]);
    });
  }

  function setSaving(id: string, isSaving: boolean) {
    setSavingIds((current) => {
      const next = { ...current };
      if (isSaving) {
        next[id] = true;
      } else {
        delete next[id];
      }
      return next;
    });
  }

  function setRoleOverride(id: string, value: string) {
    setRoleOverrides((current) => ({ ...current, [id]: value }));
  }

  function clearRoleOverride(id: string) {
    setRoleOverrides((current) => {
      const next = { ...current };
      delete next[id];
      return next;
    });
  }

  const buildButton = (
    <Button onClick={() => buildMutation.mutate()} disabled={buildMutation.isPending}>
      {buildMutation.isPending ? (
        <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
      ) : (
        <RefreshCw size={16} aria-hidden="true" />
      )}
      Обновить неделю из iiko
    </Button>
  );

  return (
    <div className="space-y-5">
      {embedded ? null : (
        <>
          <PageHeader title="Учёт смен — Смены" action={buildButton} />
          {headerSlot}
        </>
      )}

      <section className="flex flex-col gap-3 rounded-lg border bg-card p-4 sm:flex-row sm:items-end sm:justify-between">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
          <div className="grid gap-2">
            <Label htmlFor="shift-ledger-date">Дата окончания недели</Label>
            <Input
              className="w-full sm:w-[190px]"
              id="shift-ledger-date"
              max={maxDate}
              onChange={(event) => handleDateChange(event.target.value)}
              type="date"
              value={selectedDate}
            />
          </div>
          <Button
            data-testid="daily-ledger-collapse-toggle-all"
            onClick={toggleAllCurrentDays}
            type="button"
            variant="outline"
          >
            {areAllCurrentTimesCollapsed ? (
              <ChevronRight size={16} aria-hidden="true" />
            ) : (
              <ChevronDown size={16} aria-hidden="true" />
            )}
            {areAllCurrentTimesCollapsed ? "Развернуть все" : "Свернуть все"}
          </Button>
        </div>
        <div className="flex flex-wrap items-center gap-3 text-sm text-muted-foreground">
          {embedded ? buildButton : null}
          <span>{formatWeekRange(days)}</span>
          <span className="inline-flex items-center gap-2">
            <AlertTriangle className="h-4 w-4 text-amber-600" aria-hidden="true" />
            {unresolvedCount(employees)} требуют выбора
          </span>
        </div>
      </section>

      {!matrixQuery.isLoading && employees.length === 0 ? (
        <EmptyState
          icon={<AlertTriangle className="h-5 w-5" aria-hidden="true" />}
          title="За выбранную неделю нет открытых смен"
        />
      ) : (
        <ShiftLedgerMatrixTable
          collapsedTimeDayDates={collapsedTimeDayDates}
          days={days}
          employees={employees}
          expandedEmployees={expandedEmployees}
          isLoading={matrixQuery.isLoading}
          onRoleChange={saveRole}
          onToggleTimeColumns={toggleDay}
          onToggleEmployee={(employeeId) =>
            setExpandedEmployees((current) => ({
              ...current,
              [employeeId]: !current[employeeId],
            }))
          }
          roleOverrides={roleOverrides}
          savingIds={savingIds}
        />
      )}
    </div>
  );
}

function ShiftLedgerMatrixTable({
  collapsedTimeDayDates,
  days,
  employees,
  expandedEmployees,
  isLoading,
  onRoleChange,
  onToggleTimeColumns,
  onToggleEmployee,
  roleOverrides,
  savingIds,
}: {
  collapsedTimeDayDates: Set<string>;
  days: DayHeader[];
  employees: ShiftLedgerMatrixEmployee[];
  expandedEmployees: Record<string, boolean>;
  isLoading: boolean;
  onRoleChange: (shift: ShiftLedgerMatrixShift, payrollRole: string) => void;
  onToggleTimeColumns: (date: string) => void;
  onToggleEmployee: (employeeId: string) => void;
  roleOverrides: Record<string, string>;
  savingIds: Record<string, boolean>;
}) {
  const expandedTimeDayCount = days.filter((day) => !collapsedTimeDayDates.has(day.date)).length;
  const tableMinWidth =
    EMPLOYEE_COLUMN_WIDTH +
    days.length * ROLE_COLUMN_WIDTH +
    expandedTimeDayCount * TIME_COLUMN_WIDTH * 2;

  return (
    <div className="overflow-hidden rounded-lg border bg-card">
      <div className="overflow-x-auto">
        <table
          className="border-separate border-spacing-0 text-sm transition-all duration-200"
          style={{ minWidth: `${tableMinWidth}px` }}
        >
          <thead>
            <tr>
              <th
                className={cn(
                  EMPLOYEE_COLUMN_CLASS,
                  "sticky left-0 z-30 border-b border-r bg-muted/80 px-3 py-3 text-left font-medium text-muted-foreground",
                )}
                rowSpan={2}
              >
                Сотрудник
              </th>
              {days.map((day) => {
                const areTimesCollapsed = collapsedTimeDayDates.has(day.date);

                return (
                  <th
                    className={cn(
                      "border-b border-r px-3 py-2 text-center font-medium transition-all duration-200",
                      day.is_today
                        ? "bg-primary/10 text-primary"
                        : "bg-muted/70 text-muted-foreground",
                    )}
                    colSpan={areTimesCollapsed ? 1 : 3}
                    key={day.date}
                  >
                    <div className="flex flex-wrap items-center justify-center gap-x-2 gap-y-1">
                      <span>{formatDayHeader(day.date)}</span>
                      {day.is_today ? (
                        <Badge
                          className="border-primary/30 bg-background text-primary"
                          variant="outline"
                        >
                          Сегодня
                        </Badge>
                      ) : null}
                      <Button
                        aria-label={
                          areTimesCollapsed
                            ? `Развернуть время ${formatDayHeader(day.date)}`
                            : `Свернуть время ${formatDayHeader(day.date)}`
                        }
                        className="h-7 w-7"
                        data-testid={`daily-ledger-day-toggle-${day.date}`}
                        onClick={() => onToggleTimeColumns(day.date)}
                        size="icon"
                        title={areTimesCollapsed ? "Развернуть время" : "Свернуть время"}
                        type="button"
                        variant="ghost"
                      >
                        {areTimesCollapsed ? (
                          <ChevronRight size={16} aria-hidden="true" />
                        ) : (
                          <ChevronDown size={16} aria-hidden="true" />
                        )}
                      </Button>
                    </div>
                  </th>
                );
              })}
            </tr>
            <tr>
              {days.map((day) => {
                const areTimesCollapsed = collapsedTimeDayDates.has(day.date);

                return (
                  <Fragment key={day.date}>
                    <th
                      className={cn(
                        ROLE_COLUMN_CLASS,
                        subHeaderClassName(day.is_today),
                        "text-left",
                      )}
                      data-testid={`daily-ledger-role-header-${day.date}`}
                    >
                      Роль
                    </th>
                    {areTimesCollapsed ? null : (
                      <>
                        <th
                          className={cn(TIME_COLUMN_CLASS, subHeaderClassName(day.is_today))}
                          data-testid={`daily-ledger-open-header-${day.date}`}
                        >
                          Открытие
                        </th>
                        <th
                          className={cn(TIME_COLUMN_CLASS, subHeaderClassName(day.is_today))}
                          data-testid={`daily-ledger-close-header-${day.date}`}
                        >
                          Закрытие
                        </th>
                      </>
                    )}
                  </Fragment>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <LoadingRows collapsedTimeDayDates={collapsedTimeDayDates} days={days} />
            ) : (
              employees.map((employee) => (
                <Fragment key={employee.id}>
                  <SummaryRow
                    collapsedTimeDayDates={collapsedTimeDayDates}
                    employee={employee}
                    expanded={Boolean(expandedEmployees[employee.id])}
                    onRoleChange={onRoleChange}
                    onToggle={() => onToggleEmployee(employee.id)}
                    roleOverrides={roleOverrides}
                    savingIds={savingIds}
                  />
                  {expandedEmployees[employee.id] ? (
                    <DetailRows
                      collapsedTimeDayDates={collapsedTimeDayDates}
                      employee={employee}
                      onRoleChange={onRoleChange}
                      roleOverrides={roleOverrides}
                      savingIds={savingIds}
                    />
                  ) : null}
                </Fragment>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function SummaryRow({
  collapsedTimeDayDates,
  employee,
  expanded,
  onRoleChange,
  onToggle,
  roleOverrides,
  savingIds,
}: {
  collapsedTimeDayDates: Set<string>;
  employee: ShiftLedgerMatrixEmployee;
  expanded: boolean;
  onRoleChange: (shift: ShiftLedgerMatrixShift, payrollRole: string) => void;
  onToggle: () => void;
  roleOverrides: Record<string, string>;
  savingIds: Record<string, boolean>;
}) {
  const canExpand = employee.days.some((day) => day.shifts.length > 1);

  return (
    <tr className="transition-colors hover:bg-muted/40">
      <td className={stickyBodyCellClassName("bg-card")}>
        <div className="flex items-center gap-2">
          <Button
            className="h-7 w-7 shrink-0"
            disabled={!canExpand}
            onClick={onToggle}
            size="icon"
            title={expanded ? "Свернуть смены" : "Развернуть смены"}
            type="button"
            variant="ghost"
          >
            {expanded ? (
              <ChevronDown size={16} aria-hidden="true" />
            ) : (
              <ChevronRight size={16} aria-hidden="true" />
            )}
          </Button>
          <span className="font-medium">{employee.full_name}</span>
        </div>
      </td>
      {employee.days.map((day) => {
        const areTimesCollapsed = collapsedTimeDayDates.has(day.date);

        return (
          <Fragment key={day.date}>
            <td className={cn(ROLE_COLUMN_CLASS, bodyCellClassName)}>
              <SummaryRoleCell
                day={day}
                onRoleChange={onRoleChange}
                roleOverrides={roleOverrides}
                savingIds={savingIds}
              />
            </td>
            {areTimesCollapsed ? null : (
              <>
                <td
                  className={cn(TIME_COLUMN_CLASS, bodyCellClassName, "text-center tabular-nums")}
                >
                  {formatTime(day.summary.earliest_open)}
                </td>
                <td
                  className={cn(TIME_COLUMN_CLASS, bodyCellClassName, "text-center tabular-nums")}
                >
                  {formatTime(day.summary.latest_close)}
                </td>
              </>
            )}
          </Fragment>
        );
      })}
    </tr>
  );
}

function DetailRows({
  collapsedTimeDayDates,
  employee,
  onRoleChange,
  roleOverrides,
  savingIds,
}: {
  collapsedTimeDayDates: Set<string>;
  employee: ShiftLedgerMatrixEmployee;
  onRoleChange: (shift: ShiftLedgerMatrixShift, payrollRole: string) => void;
  roleOverrides: Record<string, string>;
  savingIds: Record<string, boolean>;
}) {
  const rowCount = Math.max(...employee.days.map((day) => day.shifts.length));

  return Array.from({ length: rowCount }).map((_, shiftIndex) => (
    <tr className="bg-muted/20 transition-colors hover:bg-muted/30" key={shiftIndex}>
      <td className={stickyBodyCellClassName("bg-muted")}>
        <span className="ml-9 text-xs font-medium text-muted-foreground">
          Смена {shiftIndex + 1}
        </span>
      </td>
      {employee.days.map((day) => {
        const shift = day.shifts[shiftIndex];
        const areTimesCollapsed = collapsedTimeDayDates.has(day.date);

        return (
          <Fragment key={day.date}>
            <td className={cn(ROLE_COLUMN_CLASS, bodyCellClassName)}>
              {shift ? (
                <RoleSelectCell
                  availableRoles={day.available_roles}
                  isSaving={Boolean(savingIds[shift.ledger_entry_id])}
                  onChange={(payrollRole) => onRoleChange(shift, payrollRole)}
                  shift={shift}
                  value={roleValue(shift, roleOverrides)}
                />
              ) : (
                <DisabledRoleSelect placeholder="—" />
              )}
            </td>
            {areTimesCollapsed ? null : (
              <>
                <td
                  className={cn(TIME_COLUMN_CLASS, bodyCellClassName, "text-center tabular-nums")}
                >
                  {formatTime(shift?.opened_at ?? null)}
                </td>
                <td
                  className={cn(TIME_COLUMN_CLASS, bodyCellClassName, "text-center tabular-nums")}
                >
                  {formatTime(shift?.closed_at ?? null)}
                </td>
              </>
            )}
          </Fragment>
        );
      })}
    </tr>
  ));
}

function SummaryRoleCell({
  day,
  onRoleChange,
  roleOverrides,
  savingIds,
}: {
  day: ShiftLedgerMatrixDay;
  onRoleChange: (shift: ShiftLedgerMatrixShift, payrollRole: string) => void;
  roleOverrides: Record<string, string>;
  savingIds: Record<string, boolean>;
}) {
  if (day.shifts.length === 0) {
    return <DisabledRoleSelect placeholder="—" />;
  }
  if (day.shifts.length > 1) {
    return (
      <Badge className="h-8 rounded-md px-2 text-xs" variant="outline">
        {day.shifts.length} {shiftWord(day.shifts.length)}
      </Badge>
    );
  }

  const shift = day.shifts[0];
  return (
    <RoleSelectCell
      availableRoles={day.available_roles}
      isSaving={Boolean(savingIds[shift.ledger_entry_id])}
      onChange={(payrollRole) => onRoleChange(shift, payrollRole)}
      shift={shift}
      value={roleValue(shift, roleOverrides)}
    />
  );
}

function RoleSelectCell({
  availableRoles,
  isSaving,
  onChange,
  shift,
  value,
}: {
  availableRoles: ShiftLedgerAvailableRole[];
  isSaving: boolean;
  onChange: (payrollRole: string) => void;
  shift: ShiftLedgerMatrixShift;
  value: string;
}) {
  if (shift.payroll_locked) {
    return <LockedRoleSelect value={value} />;
  }

  if (availableRoles.length === 0) {
    return <DisabledRoleSelect placeholder="Нет ролей" />;
  }

  return (
    <div className="flex items-center gap-2">
      <Select disabled={isSaving} onValueChange={onChange} value={value || undefined}>
        <SelectTrigger
          className={cn(
            "h-8 w-[132px] bg-background",
            !shift.is_resolved && "border-amber-300 bg-amber-50/70",
          )}
        >
          <SelectValue placeholder="Выбрать" />
        </SelectTrigger>
        <SelectContent>
          {availableRoles.map((role) => (
            <SelectItem key={role.payroll_role} value={role.payroll_role}>
              {roleLabel(role.payroll_role)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {isSaving ? (
        <LoaderCircle className="h-4 w-4 shrink-0 animate-spin text-muted-foreground" />
      ) : null}
    </div>
  );
}

function LockedRoleSelect({ value }: { value: string }) {
  return (
    <div className="flex items-center gap-2">
      <Select disabled value={value || undefined}>
        <SelectTrigger className="h-8 w-[132px] bg-muted/30 text-muted-foreground">
          <span className="truncate">{value ? roleLabel(value) : "Нет ролей"}</span>
        </SelectTrigger>
      </Select>
      <span
        aria-label="ЗП за эту неделю закрыта"
        className="inline-flex"
        title="ЗП за эту неделю закрыта"
      >
        <Lock className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      </span>
    </div>
  );
}

function DisabledRoleSelect({ placeholder }: { placeholder: string }) {
  return (
    <Select disabled>
      <SelectTrigger className="h-8 w-[132px] bg-muted/30 text-muted-foreground">
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
    </Select>
  );
}

function LoadingRows({
  collapsedTimeDayDates,
  days,
}: {
  collapsedTimeDayDates: Set<string>;
  days: DayHeader[];
}) {
  return (
    <>
      {Array.from({ length: 4 }).map((_, rowIndex) => (
        <tr key={rowIndex}>
          <td className={stickyBodyCellClassName("bg-card")}>
            <Skeleton className="h-5 w-40" />
          </td>
          {days.map((day) => {
            const areTimesCollapsed = collapsedTimeDayDates.has(day.date);

            return (
              <Fragment key={day.date}>
                <td className={cn(ROLE_COLUMN_CLASS, bodyCellClassName)}>
                  <Skeleton className="h-8 w-[132px]" />
                </td>
                {areTimesCollapsed ? null : (
                  <>
                    <td className={cn(TIME_COLUMN_CLASS, bodyCellClassName)}>
                      <Skeleton className="mx-auto h-5 w-12" />
                    </td>
                    <td className={cn(TIME_COLUMN_CLASS, bodyCellClassName)}>
                      <Skeleton className="mx-auto h-5 w-12" />
                    </td>
                  </>
                )}
              </Fragment>
            );
          })}
        </tr>
      ))}
    </>
  );
}

const bodyCellClassName = "border-b border-r p-2 align-middle";

function stickyBodyCellClassName(backgroundClassName: "bg-card" | "bg-muted") {
  return cn(
    EMPLOYEE_COLUMN_CLASS,
    "sticky left-0 z-20 border-b border-r px-3 py-2 align-middle",
    backgroundClassName,
  );
}

function subHeaderClassName(isToday: boolean) {
  return cn(
    "border-b border-r px-2 py-2 text-center text-xs font-semibold uppercase text-muted-foreground",
    isToday ? "bg-primary/10" : "bg-muted/60",
  );
}

function updateShiftInMatrix(
  matrix: ShiftLedgerMatrix,
  entry: ShiftLedgerEntry,
): ShiftLedgerMatrix {
  return {
    ...matrix,
    employees: matrix.employees.map((employee) => ({
      ...employee,
      days: employee.days.map((day) => ({
        ...day,
        shifts: day.shifts.map((shift) =>
          shift.ledger_entry_id === entry.id
            ? {
                ...shift,
                payroll_role: entry.payroll_role,
                category: entry.category,
                is_resolved: entry.is_resolved,
                status: entry.status,
              }
            : shift,
        ),
      })),
    })),
  };
}

function roleValue(shift: ShiftLedgerMatrixShift, overrides: Record<string, string>) {
  return overrides[shift.ledger_entry_id] ?? shift.payroll_role ?? "";
}

function roleLabel(payrollRole: string) {
  return PAYROLL_ROLE_LABELS[payrollRole as keyof typeof PAYROLL_ROLE_LABELS] ?? payrollRole;
}

function unresolvedCount(employees: ShiftLedgerMatrixEmployee[]) {
  return employees.reduce(
    (total, employee) =>
      total +
      employee.days.reduce(
        (dayTotal, day) => dayTotal + day.shifts.filter((shift) => !shift.is_resolved).length,
        0,
      ),
    0,
  );
}

function compareEmployees(left: ShiftLedgerMatrixEmployee, right: ShiftLedgerMatrixEmployee) {
  return left.full_name.localeCompare(right.full_name, "ru");
}

function formatTime(value: string | null) {
  if (!value) {
    return "—";
  }
  return new Intl.DateTimeFormat("ru-RU", {
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "Europe/Moscow",
  }).format(new Date(value));
}

function formatDayHeader(dateIso: string) {
  const value = new Date(`${dateIso}T12:00:00`);
  const weekday = new Intl.DateTimeFormat("ru-RU", { weekday: "short" }).format(value);
  const dayMonth = new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "short",
  }).format(value);
  return `${weekday}, ${dayMonth}`;
}

function formatWeekRange(days: DayHeader[]) {
  const first = days[0]?.date;
  const last = days[days.length - 1]?.date;
  if (!first || !last) {
    return "Неделя не выбрана";
  }
  return `${formatShortDate(first)} - ${formatShortDate(last)}`;
}

function formatShortDate(dateIso: string) {
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
  }).format(new Date(`${dateIso}T12:00:00`));
}

function shiftWord(count: number) {
  if (count % 10 === 1 && count % 100 !== 11) {
    return "смена";
  }
  if ([2, 3, 4].includes(count % 10) && ![12, 13, 14].includes(count % 100)) {
    return "смены";
  }
  return "смен";
}

function buildWeekHeaders(selectedDate: string): DayHeader[] {
  const end = parseIsoDate(selectedDate);
  return Array.from({ length: 7 }).map((_, index) => {
    const date = new Date(end);
    date.setDate(end.getDate() - 6 + index);
    const iso = toIsoDate(date);
    return {
      date: iso,
      is_today: iso === todayIsoDate(),
    };
  });
}

function parseIsoDate(value: string) {
  return new Date(`${value}T12:00:00`);
}

function toIsoDate(value: Date) {
  const timezoneOffsetMs = value.getTimezoneOffset() * 60_000;
  return new Date(value.getTime() - timezoneOffsetMs).toISOString().slice(0, 10);
}

function todayIsoDate() {
  return toIsoDate(new Date());
}

function sortIsoDates(values: string[]) {
  return values.filter(isIsoDate).sort((left, right) => left.localeCompare(right));
}

function isPayrollLockedError(error: unknown) {
  return axios.isAxiosError(error) && error.response?.status === 409;
}

function readCollapsedTimeDays() {
  if (typeof window === "undefined") {
    return null;
  }

  try {
    const storedValue = window.localStorage.getItem(COLLAPSED_TIMES_STORAGE_KEY);
    if (storedValue === null) {
      return null;
    }

    const parsed = JSON.parse(storedValue);
    return Array.isArray(parsed)
      ? sortIsoDates(parsed.filter((item) => typeof item === "string"))
      : [];
  } catch {
    return null;
  }
}

function persistCollapsedTimeDays(values: string[]) {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(COLLAPSED_TIMES_STORAGE_KEY, JSON.stringify(sortIsoDates(values)));
}

function isIsoDate(value: string) {
  return /^\d{4}-\d{2}-\d{2}$/.test(value);
}
