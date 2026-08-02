import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { Badge } from "@/components/ui/badge";
import { getUtilityCalendar } from "@/lib/api";

const MONTHS = [
  "январь",
  "февраль",
  "март",
  "апрель",
  "май",
  "июнь",
  "июль",
  "август",
  "сентябрь",
  "октябрь",
  "ноябрь",
  "декабрь",
];

function monthTitle(iso: string): string {
  const [year, month] = iso.split("-");
  return `${MONTHS[Number(month) - 1] ?? month} ${year}`;
}

/**
 * Коммунальные месяцы, за которые бумажку не принесли.
 *
 * Живёт в «Признании расходов» (решение владельца 02.08.2026), и это единственное правильное
 * место: остальные витрины показывают то, что в системе ЕСТЬ, — платежи, документы, начисления.
 * Месяц, за который квитанцию не принесли, не оставляет следа нигде, и обнаружить его можно
 * было только вспомнив. А признание расходов ровно об этом и спрашивает: чего мы ещё ждём.
 *
 * Показываем ТОЛЬКО пропуски. Месяц с документом уже виден в общей очереди признания, и
 * повторять его здесь значило бы утопить пропажу в списке благополучия.
 */
export function UtilityExpectations() {
  const query = useQuery({
    queryKey: ["utility-calendar"],
    queryFn: () => getUtilityCalendar(6),
  });

  const missing = useMemo(
    () => (query.data ?? []).filter((row) => row.state === "overdue" || row.state === "missing"),
    [query.data],
  );

  if (query.isLoading || missing.length === 0) return null;

  return (
    <div className="rounded-lg border border-amber-200 bg-amber-50/60 p-3">
      <div className="text-sm font-medium text-amber-900">Коммунальные платёжки не принесены</div>
      <p className="mt-0.5 text-xs text-amber-800">
        Расход за эти месяцы не признан, и долг перед арендодателем не посчитан: сумму знает
        только бумага — вывести её из договора, как арендную ставку, нельзя.
      </p>
      <div className="mt-2 flex flex-wrap gap-2">
        {missing.map((row) => (
          <Badge
            key={`${row.account_id}:${row.month}`}
            variant="outline"
            className={
              row.state === "overdue"
                ? "border-amber-400 bg-white text-amber-900"
                : "border-amber-200 bg-white text-amber-800"
            }
          >
            {row.account_title} · {monthTitle(row.month)}
            {row.state === "overdue" ? " · просрочено" : ""}
          </Badge>
        ))}
      </div>
    </div>
  );
}
