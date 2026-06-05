import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CalendarDays,
  ChevronLeft,
  ChevronRight,
  ClipboardList,
  RefreshCw,
  UsersRound,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { EmptyState } from "@/components/ui-app/EmptyState";
import { PageHeader } from "@/components/ui-app/PageHeader";
import {
  apiErrorMessage,
  getCourierStatistics,
  getCourierStatisticsDetail,
  type CourierStatisticsDetail,
  type CourierStatisticsRow,
  type KpiValue,
} from "@/lib/api";
import { cn } from "@/lib/utils";

import {
  currentMonthKey,
  formatDate,
  formatDateTime,
  formatMetric,
  formatMonthLabel,
  previousMonthKey,
  scoreClass,
  shiftMonth,
  thresholdBadgeClass,
  thresholdLabel,
} from "./utils";

type CourierStatisticsRouteProps = {
  onNavigate: (path: string) => void;
};

export function CourierStatisticsRoute({ onNavigate }: CourierStatisticsRouteProps) {
  const queryClient = useQueryClient();
  const [month, setMonth] = useState(currentMonthKey);
  const [selectedCourierId, setSelectedCourierId] = useState<string | null>(null);

  const statisticsQuery = useQuery({
    queryKey: ["courier-statistics", month],
    queryFn: () => getCourierStatistics({ month }),
    staleTime: 30_000,
  });

  const selectedRow = useMemo(
    () =>
      (statisticsQuery.data ?? []).find((row) => row.kpi.courier_id === selectedCourierId) ?? null,
    [selectedCourierId, statisticsQuery.data],
  );

  const detailQuery = useQuery({
    queryKey: ["courier-statistics-detail", selectedCourierId, month],
    queryFn: () => getCourierStatisticsDetail(selectedCourierId ?? "", month),
    enabled: Boolean(selectedCourierId),
  });

  const rows = statisticsQuery.data ?? [];

  return (
    <div className="space-y-5">
      <PageHeader
        title="Статистика курьеров"
        description="KPI и оценки курьеров за период."
        action={
          <Button
            onClick={() => void queryClient.invalidateQueries({ queryKey: ["courier-statistics"] })}
            title="Обновить"
            variant="outline"
          >
            <RefreshCw size={16} aria-hidden="true" />
            Обновить
          </Button>
        }
      />

      <section className="flex flex-wrap items-end gap-3 rounded-lg border bg-card p-4">
        <div className="grid gap-2">
          <span className="text-sm font-medium">Период</span>
          <div className="flex items-center rounded-md border bg-background p-1">
            <Button
              onClick={() => setMonth((current) => shiftMonth(current, -1))}
              size="icon"
              title="Предыдущий месяц"
              variant="ghost"
            >
              <ChevronLeft size={16} aria-hidden="true" />
            </Button>
            <div className="flex h-9 min-w-[170px] items-center justify-center gap-2 px-3 text-sm font-medium">
              <CalendarDays className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
              {formatMonthLabel(month)}
            </div>
            <Button
              onClick={() => setMonth((current) => shiftMonth(current, 1))}
              size="icon"
              title="Следующий месяц"
              variant="ghost"
            >
              <ChevronRight size={16} aria-hidden="true" />
            </Button>
          </div>
        </div>

        <div className="grid gap-2">
          <span className="text-sm font-medium">Быстрый выбор</span>
          <div className="flex gap-2">
            <Button onClick={() => setMonth(currentMonthKey())} variant="outline">
              Текущий
            </Button>
            <Button onClick={() => setMonth(previousMonthKey())} variant="outline">
              Прошлый
            </Button>
          </div>
        </div>
      </section>

      {statisticsQuery.isLoading ? (
        <StatisticsSkeleton />
      ) : statisticsQuery.isError ? (
        <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {apiErrorMessage(statisticsQuery.error, "Не удалось загрузить статистику курьеров")}
        </div>
      ) : rows.length === 0 ? (
        <EmptyState
          icon={<UsersRound size={18} aria-hidden="true" />}
          title="Курьеры не найдены"
          description="Проверьте список активных курьеров."
        />
      ) : (
        <section className="grid gap-4 md:grid-cols-2 2xl:grid-cols-3">
          {rows.map((row) => (
            <CourierStatCard
              key={row.kpi.courier_id}
              onDetails={() => setSelectedCourierId(row.kpi.courier_id)}
              row={row}
            />
          ))}
        </section>
      )}

      <Sheet
        open={Boolean(selectedCourierId)}
        onOpenChange={(open) => {
          if (!open) {
            setSelectedCourierId(null);
          }
        }}
      >
        <SheetContent className="w-full overflow-y-auto sm:max-w-2xl">
          {detailQuery.isLoading ? (
            <DetailSkeleton />
          ) : detailQuery.isError ? (
            <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {apiErrorMessage(detailQuery.error, "Не удалось загрузить карточку курьера")}
            </div>
          ) : detailQuery.data ? (
            <CourierDetail
              detail={detailQuery.data}
              month={month}
              onAllEvaluations={() => {
                const courierId = detailQuery.data?.kpi.courier_id;
                if (courierId) {
                  onNavigate(`/couriers/evaluations?courier=${courierId}&month=${month}`);
                }
              }}
            />
          ) : selectedRow ? (
            <SheetHeader>
              <SheetTitle>{selectedRow.kpi.courier_name}</SheetTitle>
              <SheetDescription>{formatMonthLabel(month)}</SheetDescription>
            </SheetHeader>
          ) : null}
        </SheetContent>
      </Sheet>
    </div>
  );
}

function CourierStatCard({ onDetails, row }: { onDetails: () => void; row: CourierStatisticsRow }) {
  const empty = isMonthEmpty(row);
  return (
    <Card className="shadow-none">
      <CardHeader className="space-y-3 pb-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="truncate text-base font-semibold">{row.kpi.courier_name}</h2>
            <div className="mt-1 text-xs text-muted-foreground">
              Основные смены: {row.kpi.primary_shifts_worked}/{row.kpi.primary_shifts_planned}
            </div>
          </div>
          <Button onClick={onDetails} size="sm" variant="outline">
            Подробнее
          </Button>
        </div>
        {empty ? (
          <div className="rounded-md border border-dashed px-3 py-2 text-sm text-muted-foreground">
            Нет данных за этот месяц
          </div>
        ) : null}
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-2 gap-2">
          <KpiBlock
            label="Скорость"
            suffix={row.kpi.speed_minutes.value === null ? undefined : "мин"}
            value={row.kpi.speed_minutes.value}
            emptyText="нет доставок"
            metric={row.kpi.speed_minutes}
          />
          <KpiBlock
            label="Дисциплина"
            suffix={row.kpi.discipline_percent.value === null ? undefined : "%"}
            value={row.kpi.discipline_percent.value}
            emptyText={row.kpi.primary_shifts_planned > 0 ? "нет явок" : "нет основных"}
            metric={row.kpi.discipline_percent}
            title="Дисциплина считается только по основным сменам"
          />
          <KpiBlock
            label="Производительность"
            suffix={row.kpi.productivity.value === null ? undefined : ""}
            value={row.kpi.productivity.value}
            emptyText="—"
            metric={row.kpi.productivity}
            note="доставок/смена"
          />
          <KpiBlock label="Помощь" value={row.kpi.help_count} note="смен помощи" />
        </div>

        <EvaluationSummary row={row} />
      </CardContent>
    </Card>
  );
}

function KpiBlock({
  emptyText,
  label,
  metric,
  note,
  suffix,
  title,
  value,
}: {
  emptyText?: string;
  label: string;
  metric?: KpiValue;
  note?: string;
  suffix?: string;
  title?: string;
  value: number | null;
}) {
  const hasValue = value !== null && value !== undefined;
  return (
    <div className="min-h-[102px] rounded-md border bg-background p-3" title={title}>
      <div className="text-xs font-medium text-muted-foreground">{label}</div>
      <div className="mt-2 flex items-baseline gap-1 text-lg font-semibold">
        {hasValue ? formatMetric(value) : (emptyText ?? "—")}
        {hasValue && suffix ? (
          <span className="text-xs text-muted-foreground">{suffix}</span>
        ) : null}
      </div>
      {metric ? (
        <div className="mt-2">
          <span className={thresholdBadgeClass(metric.threshold)}>
            {thresholdLabel(metric.threshold)}
          </span>
        </div>
      ) : null}
      {note ? <div className="mt-1 text-xs text-muted-foreground">{note}</div> : null}
    </div>
  );
}

function EvaluationSummary({ row }: { row: CourierStatisticsRow }) {
  const summary = row.evaluations;
  return (
    <div className="rounded-md border bg-background p-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-xs font-medium text-muted-foreground">Оценки</div>
          <div className={cn("mt-1 text-2xl font-semibold", scoreClass(summary.score_sum))}>
            {scoreValue(summary.score_sum)}
          </div>
        </div>
        <div className="mt-5 whitespace-nowrap text-sm tabular-nums text-muted-foreground">
          +{summary.positive_count} / −{summary.negative_count} / ={summary.neutral_count}
        </div>
      </div>
      <div className="mt-2 truncate text-sm text-muted-foreground">
        {summary.top_criteria.length > 0
          ? summary.top_criteria
              .slice(0, 2)
              .map((criterion) => `${criterion.label} (${criterion.count})`)
              .join(", ")
          : "Критериев нет"}
      </div>
    </div>
  );
}

function CourierDetail({
  detail,
  month,
  onAllEvaluations,
}: {
  detail: CourierStatisticsDetail;
  month: string;
  onAllEvaluations: () => void;
}) {
  const kpi = detail.kpi;
  return (
    <div className="space-y-5">
      <SheetHeader>
        <SheetTitle>{kpi.courier_name}</SheetTitle>
        <SheetDescription>Период {formatMonthLabel(month)}</SheetDescription>
      </SheetHeader>

      <div className="grid gap-2 text-sm">
        <InfoRow label="iiko ID" value={detail.courier_iiko_id} />
        <InfoRow label="Последняя смена" value={lastShiftText(detail.last_shift)} />
      </div>

      <section className="grid gap-2 sm:grid-cols-2">
        <KpiBlock
          label="Скорость"
          suffix={kpi.speed_minutes.value === null ? undefined : "мин"}
          value={kpi.speed_minutes.value}
          emptyText="нет доставок"
          metric={kpi.speed_minutes}
          note="Среднее время от взятия заказа до доставки"
        />
        <KpiBlock
          label="Дисциплина"
          suffix={kpi.discipline_percent.value === null ? undefined : "%"}
          value={kpi.discipline_percent.value}
          emptyText={kpi.primary_shifts_planned > 0 ? "нет явок" : "нет основных"}
          metric={kpi.discipline_percent}
          note="Считается только для primary"
        />
        <KpiBlock
          label="Производительность"
          value={kpi.productivity.value}
          emptyText="—"
          metric={kpi.productivity}
          note="Доставок за отработанную смену"
        />
        <KpiBlock label="Помощь" value={kpi.help_count} note="Смен помощи с доставками" />
      </section>

      <section className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Запланировано</TableHead>
              <TableHead>Отработано</TableHead>
              <TableHead>Помощь</TableHead>
              <TableHead>No-show</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            <TableRow>
              <TableCell>{detail.discipline_breakdown.planned}</TableCell>
              <TableCell>{detail.discipline_breakdown.worked}</TableCell>
              <TableCell>{detail.discipline_breakdown.help}</TableCell>
              <TableCell>{detail.discipline_breakdown.no_show}</TableCell>
            </TableRow>
          </TableBody>
        </Table>
      </section>

      <section className="space-y-3">
        <div className="flex items-center justify-between gap-2">
          <h3 className="text-sm font-semibold">Последние оценки</h3>
          <Button onClick={onAllEvaluations} size="sm" variant="outline">
            <ClipboardList size={16} aria-hidden="true" />
            Все оценки курьера
          </Button>
        </div>
        {detail.latest_evaluations.length === 0 ? (
          <div className="rounded-md border border-dashed px-3 py-6 text-center text-sm text-muted-foreground">
            Оценок за месяц нет
          </div>
        ) : (
          <div className="space-y-2">
            {detail.latest_evaluations.map((evaluation) => (
              <div
                className="grid gap-1 rounded-md border bg-background px-3 py-2 text-sm"
                key={evaluation.id}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium">
                    {evaluation.criterion_label ?? `Критерий #${evaluation.criterion_id}`}
                  </span>
                  <span className={cn("font-semibold", scoreClass(evaluation.score_snapshot))}>
                    {scoreValue(evaluation.score_snapshot)}
                  </span>
                </div>
                <div className="text-xs text-muted-foreground">
                  {formatDate(evaluation.evaluated_at)} ·{" "}
                  {evaluation.author_name ?? "Автор не найден"}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-md border bg-background px-3 py-2">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-right font-medium">{value}</span>
    </div>
  );
}

function StatisticsSkeleton() {
  return (
    <section className="grid gap-4 md:grid-cols-2 2xl:grid-cols-3">
      {Array.from({ length: 6 }).map((_, index) => (
        <Card className="shadow-none" key={index}>
          <CardHeader>
            <Skeleton className="h-5 w-48" />
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid grid-cols-2 gap-2">
              <Skeleton className="h-24" />
              <Skeleton className="h-24" />
              <Skeleton className="h-24" />
              <Skeleton className="h-24" />
            </div>
            <Skeleton className="h-20" />
          </CardContent>
        </Card>
      ))}
    </section>
  );
}

function DetailSkeleton() {
  return (
    <div className="space-y-4">
      <Skeleton className="h-7 w-64" />
      <Skeleton className="h-4 w-40" />
      <Skeleton className="h-24" />
      <Skeleton className="h-40" />
    </div>
  );
}

function isMonthEmpty(row: CourierStatisticsRow) {
  return (
    row.kpi.deliveries_total === 0 &&
    row.kpi.primary_shifts_worked === 0 &&
    row.kpi.secondary_shifts_worked === 0 &&
    row.kpi.primary_shifts_planned === 0 &&
    row.evaluations.positive_count === 0 &&
    row.evaluations.negative_count === 0 &&
    row.evaluations.neutral_count === 0
  );
}

function scoreValue(score: number) {
  return score > 0 ? `+${score}` : String(score);
}

function lastShiftText(shift: CourierStatisticsDetail["last_shift"]) {
  if (!shift) {
    return "нет смен за месяц";
  }
  const opened = formatDateTime(shift.opened_at);
  const closed = shift.closed_at ? formatDateTime(shift.closed_at) : "открыта";
  return `${opened} → ${closed}`;
}
