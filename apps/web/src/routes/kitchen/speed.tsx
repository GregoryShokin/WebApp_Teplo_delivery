import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Flame, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { PageHeader } from "@/components/ui-app/PageHeader";
import {
  apiErrorMessage,
  getKdsDashboard,
  type KdsDashboard,
  type KdsStatBlock,
} from "@/lib/api";
import { cn } from "@/lib/utils";

type Segment = "asap" | "preorder";
type Interval = "queue_cook" | "packing" | "courier_wait" | "road";

const INTERVAL_LABELS: Record<Interval, string> = {
  queue_cook: "Очередь + готовка",
  packing: "Упаковка",
  courier_wait: "Ожидание курьера",
  road: "Дорога",
};

const SEGMENT_LABELS: Record<Segment, string> = {
  asap: "Срочные (ASAP)",
  preorder: "Предзаказы",
};

const PERIOD_OPTIONS = [
  { label: "7 дней", days: 7 },
  { label: "30 дней", days: 30 },
] as const;

const PEAK_HOURS = new Set([16, 17, 18, 19]);

function isoDaysAgo(days: number): string {
  const date = new Date();
  date.setDate(date.getDate() - days);
  return date.toISOString().slice(0, 10);
}

function fmtMin(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${value} мин`;
}

function fmtPct(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${value}%`;
}

function statOf(dashboard: KdsDashboard, segment: Segment, interval: Interval) {
  return dashboard.by_hour
    .filter((row) => row.segment === segment)
    .map((row) => ({ hour: row.hour, stat: row[interval] as KdsStatBlock }))
    .sort((a, b) => a.hour - b.hour);
}

export function KitchenSpeedRoute() {
  const queryClient = useQueryClient();
  const [periodDays, setPeriodDays] = useState<number>(7);
  const [segment, setSegment] = useState<Segment>("asap");
  const [interval, setInterval] = useState<Interval>("queue_cook");

  const dateFrom = useMemo(() => isoDaysAgo(periodDays - 1), [periodDays]);

  const dashboardQuery = useQuery({
    queryKey: ["kds-dashboard", dateFrom],
    queryFn: () => getKdsDashboard({ date_from: dateFrom }),
    staleTime: 30_000,
  });

  const dashboard = dashboardQuery.data;
  const hourly = dashboard ? statOf(dashboard, segment, interval) : [];
  const maxMedian = Math.max(1, ...hourly.map((row) => row.stat.median ?? 0));
  const peak = dashboard?.peak;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Скорость кухни"
        description="Путь заказа по 4 интервалам, медиана и p90 по часам. Акцент на пик 16–19ч."
        action={
          <Button
            onClick={() => void queryClient.invalidateQueries({ queryKey: ["kds-dashboard"] })}
            title="Обновить"
            variant="outline"
          >
            <RefreshCw size={16} aria-hidden="true" />
            Обновить
          </Button>
        }
      />

      <div className="flex flex-wrap items-center gap-2">
        <div className="flex gap-1">
          {PERIOD_OPTIONS.map((option) => (
            <Button
              key={option.days}
              size="sm"
              variant={periodDays === option.days ? "default" : "outline"}
              onClick={() => setPeriodDays(option.days)}
            >
              {option.label}
            </Button>
          ))}
        </div>
        <div className="flex gap-1">
          {(Object.keys(SEGMENT_LABELS) as Segment[]).map((value) => (
            <Button
              key={value}
              size="sm"
              variant={segment === value ? "default" : "outline"}
              onClick={() => setSegment(value)}
            >
              {SEGMENT_LABELS[value]}
            </Button>
          ))}
        </div>
      </div>

      {dashboardQuery.isError ? (
        <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">
          {apiErrorMessage(dashboardQuery.error, "Не удалось загрузить дашборд")}
        </div>
      ) : null}

      {dashboardQuery.isLoading ? (
        <Skeleton className="h-64 w-full" />
      ) : dashboard ? (
        <>
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <KpiCard title="Медиана упаковки" value={fmtMin(dashboard.kpi.median_packing_min)} />
            <KpiCard
              title="Медиана ожидания курьера"
              value={fmtMin(dashboard.kpi.median_courier_wait_min)}
            />
            <KpiCard
              title="Опоздание горячих"
              value={fmtPct(dashboard.kpi.hot_late_pct)}
              hint={`${dashboard.kpi.hot_late_n} зак.`}
              warn={(dashboard.kpi.hot_late_pct ?? 0) > 50}
            />
            <KpiCard
              title="Опоздание роллов"
              value={fmtPct(dashboard.kpi.rolls_late_pct)}
              hint={`${dashboard.kpi.rolls_late_n} зак.`}
            />
          </div>

          {peak ? (
            <Card className="border-orange-300/60 bg-orange-50/40 shadow-none dark:bg-orange-950/10">
              <CardHeader className="pb-2">
                <div className="flex items-center gap-2 text-sm font-semibold">
                  <Flame size={16} className="text-orange-500" aria-hidden="true" />
                  Пик 16:00–19:59 · {SEGMENT_LABELS.asap} · {peak.count} заказов с тапами
                </div>
              </CardHeader>
              <CardContent className="grid grid-cols-2 gap-3 lg:grid-cols-4">
                {(Object.keys(INTERVAL_LABELS) as Interval[]).map((key) => {
                  const stat = peak[key] as KdsStatBlock | undefined;
                  return (
                    <div key={key} className="rounded-md border border-border bg-background p-2">
                      <div className="text-xs text-muted-foreground">{INTERVAL_LABELS[key]}</div>
                      <div className="text-lg font-semibold">{fmtMin(stat?.median)}</div>
                      <div className="text-xs text-muted-foreground">
                        p90 {fmtMin(stat?.p90)} · n {stat?.n ?? 0}
                      </div>
                    </div>
                  );
                })}
              </CardContent>
            </Card>
          ) : null}

          <Card className="shadow-none">
            <CardHeader className="space-y-3 pb-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="text-sm font-semibold">
                  По часам · {SEGMENT_LABELS[segment]}
                </div>
                <div className="flex flex-wrap gap-1">
                  {(Object.keys(INTERVAL_LABELS) as Interval[]).map((key) => (
                    <Button
                      key={key}
                      size="sm"
                      variant={interval === key ? "default" : "outline"}
                      onClick={() => setInterval(key)}
                    >
                      {INTERVAL_LABELS[key]}
                    </Button>
                  ))}
                </div>
              </div>
            </CardHeader>
            <CardContent>
              {hourly.length === 0 ? (
                <div className="py-8 text-center text-sm text-muted-foreground">
                  Нет данных за период. Тапы появятся после работы планшета упаковки.
                </div>
              ) : (
                <div className="space-y-1.5">
                  {hourly.map(({ hour, stat }) => (
                    <div key={hour} className="flex items-center gap-2 text-xs">
                      <div
                        className={cn(
                          "w-12 shrink-0 tabular-nums",
                          PEAK_HOURS.has(hour) ? "font-semibold text-orange-600" : "text-muted-foreground",
                        )}
                      >
                        {String(hour).padStart(2, "0")}:00
                      </div>
                      <div className="relative h-6 flex-1 rounded bg-muted">
                        <div
                          className={cn(
                            "absolute inset-y-0 left-0 rounded",
                            PEAK_HOURS.has(hour) ? "bg-orange-500" : "bg-primary",
                          )}
                          style={{ width: `${((stat.median ?? 0) / maxMedian) * 100}%` }}
                        />
                      </div>
                      <div className="w-32 shrink-0 text-right tabular-nums text-muted-foreground">
                        {fmtMin(stat.median)} · p90 {fmtMin(stat.p90)} · n{stat.n}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>

          <Card className="shadow-none">
            <CardHeader className="pb-2">
              <div className="text-sm font-semibold">Пунктуальность предзаказов</div>
            </CardHeader>
            <CardContent className="grid grid-cols-2 gap-3 text-sm">
              <div className="rounded-md border border-border p-2">
                <div className="text-xs text-muted-foreground">Готов вовремя (vs обещание)</div>
                <div className="text-lg font-semibold">
                  {fmtPct(dashboard.preorder_punctuality.ready_on_time_pct)}
                </div>
                <div className="text-xs text-muted-foreground">
                  n {dashboard.preorder_punctuality.ready_n}
                </div>
              </div>
              <div className="rounded-md border border-border p-2">
                <div className="text-xs text-muted-foreground">Доставлен вовремя</div>
                <div className="text-lg font-semibold">
                  {fmtPct(dashboard.preorder_punctuality.delivered_on_time_pct)}
                </div>
                <div className="text-xs text-muted-foreground">
                  n {dashboard.preorder_punctuality.delivered_n}
                </div>
              </div>
            </CardContent>
          </Card>
        </>
      ) : null}
    </div>
  );
}

function KpiCard({
  title,
  value,
  hint,
  warn,
}: {
  title: string;
  value: string;
  hint?: string;
  warn?: boolean;
}) {
  return (
    <Card className="shadow-none">
      <CardContent className="space-y-1 p-3">
        <div className="text-xs text-muted-foreground">{title}</div>
        <div className={cn("text-2xl font-semibold tabular-nums", warn && "text-destructive")}>
          {value}
        </div>
        {hint ? <div className="text-xs text-muted-foreground">{hint}</div> : null}
      </CardContent>
    </Card>
  );
}
