import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, LoaderCircle, UsersRound } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui-app/EmptyState";
import {
  apiErrorMessage,
  getDishwasherEmployees,
  getDishwasherShifts,
  putDishwasherShift,
} from "@/lib/api";

const WEEKDAY_LABELS = ["Вс", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"];

type MonthRange = {
  year: number;
  month: number; // 0-based
};

function currentMonth(): MonthRange {
  const now = new Date();
  return { year: now.getFullYear(), month: now.getMonth() };
}

function pad2(value: number) {
  return String(value).padStart(2, "0");
}

function isoDate(year: number, month: number, day: number) {
  return `${year}-${pad2(month + 1)}-${pad2(day)}`;
}

function daysInMonth(year: number, month: number) {
  return new Date(year, month + 1, 0).getDate();
}

function monthLabel({ year, month }: MonthRange) {
  return new Intl.DateTimeFormat("ru-RU", { month: "long", year: "numeric" }).format(
    new Date(year, month, 1),
  );
}

function shiftKey(employeeId: string, workDate: string) {
  return `${employeeId}__${workDate}`;
}

export function DishwasherScheduleSection({ canEdit }: { canEdit: boolean }) {
  const queryClient = useQueryClient();
  const [range, setRange] = useState<MonthRange>(currentMonth);

  const total = daysInMonth(range.year, range.month);
  const periodStart = isoDate(range.year, range.month, 1);
  const periodEnd = isoDate(range.year, range.month, total);
  const allDays = useMemo(
    () => Array.from({ length: total }, (_, index) => index + 1),
    [total],
  );

  const employeesQuery = useQuery({
    queryKey: ["dishwasher-employees"],
    queryFn: getDishwasherEmployees,
  });
  const shiftsQuery = useQuery({
    queryKey: ["dishwasher-shifts", periodStart, periodEnd],
    queryFn: () => getDishwasherShifts({ period_start: periodStart, period_end: periodEnd }),
  });

  const workedSet = useMemo(() => {
    const set = new Set<string>();
    for (const shift of shiftsQuery.data ?? []) {
      set.add(shiftKey(shift.employee_id, shift.work_date));
    }
    return set;
  }, [shiftsQuery.data]);

  const toggleMutation = useMutation({
    mutationFn: (payload: { employee_id: string; work_date: string; worked: boolean }) =>
      putDishwasherShift(payload),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: ["dishwasher-shifts", periodStart, periodEnd],
      });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось изменить смену")),
  });

  const employees = employeesQuery.data ?? [];

  function shiftMonth(direction: -1 | 1) {
    setRange((current) => {
      const next = new Date(current.year, current.month + direction, 1);
      return { year: next.getFullYear(), month: next.getMonth() };
    });
  }

  function countShifts(employeeId: string, fromDay: number, toDay: number) {
    let count = 0;
    for (let day = fromDay; day <= toDay; day += 1) {
      if (workedSet.has(shiftKey(employeeId, isoDate(range.year, range.month, day)))) {
        count += 1;
      }
    }
    return count;
  }

  function handleToggle(employeeId: string, day: number) {
    if (!canEdit || toggleMutation.isPending) {
      return;
    }
    const workDate = isoDate(range.year, range.month, day);
    const worked = !workedSet.has(shiftKey(employeeId, workDate));
    toggleMutation.mutate({ employee_id: employeeId, work_date: workDate, worked });
  }

  const firstHalfEnd = Math.min(15, total);

  return (
    <div className="space-y-4">
      <section className="flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-card p-4">
        <div>
          <div className="font-semibold">График мойщиц</div>
          <div className="mt-1 text-sm text-muted-foreground">
            Отметьте отработанные дни. Ставка за смену = пул ÷ дней месяца. 1–15 — выплата 15-го,
            16–{total} — выплата 1-го числа следующего месяца.
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button onClick={() => shiftMonth(-1)} size="icon" variant="outline" title="Предыдущий месяц">
            <ChevronLeft size={16} aria-hidden="true" />
          </Button>
          <div className="min-w-[160px] text-center text-sm font-medium capitalize">
            {monthLabel(range)}
          </div>
          <Button onClick={() => shiftMonth(1)} size="icon" variant="outline" title="Следующий месяц">
            <ChevronRight size={16} aria-hidden="true" />
          </Button>
          <Button onClick={() => setRange(currentMonth())} size="sm" variant="outline">
            Текущий месяц
          </Button>
        </div>
      </section>

      {employeesQuery.isLoading ? (
        <div className="flex items-center gap-2 px-1 py-4 text-sm text-muted-foreground">
          <LoaderCircle className="animate-spin" size={16} aria-hidden="true" /> Загрузка мойщиц…
        </div>
      ) : employees.length === 0 ? (
        <EmptyState
          icon={<UsersRound size={18} aria-hidden="true" />}
          title="Мойщицы не найдены"
          description="Проверьте, что на должности «Посудомойка» есть активные сотрудники."
        />
      ) : (
        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b bg-muted/60">
                <th className="sticky left-0 z-10 w-[220px] bg-muted/95 px-3 py-2 text-left font-medium">
                  Мойщица
                </th>
                {allDays.map((day) => {
                  const dateObj = new Date(range.year, range.month, day);
                  const isDivider = day === 16 && total >= 16;
                  return (
                    <th
                      className={`w-9 px-1 py-2 text-center font-medium ${
                        isDivider ? "border-l-2 border-l-border" : ""
                      }`}
                      key={day}
                    >
                      <div>{day}</div>
                      <div className="text-[10px] font-normal text-muted-foreground">
                        {WEEKDAY_LABELS[dateObj.getDay()]}
                      </div>
                    </th>
                  );
                })}
                <th className="w-[150px] px-2 py-2 text-right font-medium">Смен (½₁ / ½₂ / всего)</th>
              </tr>
            </thead>
            <tbody>
              {employees.map((employee) => {
                const firstHalf = countShifts(employee.id, 1, firstHalfEnd);
                const secondHalf = total > 15 ? countShifts(employee.id, 16, total) : 0;
                return (
                  <tr className="border-b last:border-b-0" key={employee.id}>
                    <th className="sticky left-0 z-10 bg-card px-3 py-2 text-left font-medium">
                      <div className="truncate">{employee.full_name}</div>
                    </th>
                    {allDays.map((day) => {
                      const workDate = isoDate(range.year, range.month, day);
                      const worked = workedSet.has(shiftKey(employee.id, workDate));
                      const isDivider = day === 16 && total >= 16;
                      return (
                        <td
                          className={`p-0.5 text-center ${
                            isDivider ? "border-l-2 border-l-border" : ""
                          }`}
                          key={day}
                        >
                          <button
                            aria-label={`${employee.full_name}, ${day}`}
                            aria-pressed={worked}
                            className={`flex h-7 w-7 items-center justify-center rounded-md border text-xs transition-colors ${
                              worked
                                ? "border-emerald-300 bg-emerald-100 text-emerald-800"
                                : "border-border bg-background text-muted-foreground hover:bg-muted"
                            } ${canEdit ? "cursor-pointer" : "cursor-default opacity-80"}`}
                            disabled={!canEdit || toggleMutation.isPending}
                            onClick={() => handleToggle(employee.id, day)}
                            type="button"
                          >
                            {worked ? "✓" : ""}
                          </button>
                        </td>
                      );
                    })}
                    <td className="px-2 py-2 text-right tabular-nums">
                      <span className="text-muted-foreground">
                        {firstHalf} / {secondHalf} /{" "}
                      </span>
                      <span className="font-semibold">{firstHalf + secondHalf}</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
