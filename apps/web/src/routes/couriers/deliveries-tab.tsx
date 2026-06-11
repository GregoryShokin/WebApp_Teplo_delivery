import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import {
  formatDateTime as formatDateTimeUtil,
  mondayWeekRange,
  subDaysFromToday,
  todayInput,
} from "./utils";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { EmptyState } from "@/components/ui-app/EmptyState";
import {
  getCourierIikoDeliveries,
  syncCourierIikoDeliveries,
  type CourierIikoDeliveryRow,
} from "@/lib/api";

const PAGE_SIZE = 50;

type RangePreset = "today" | "week" | "month";

function presetRange(preset: RangePreset): { from: string; to: string } {
  if (preset === "today") {
    const today = todayInput();
    return { from: today, to: today };
  }
  if (preset === "week") {
    return mondayWeekRange();
  }
  return { from: subDaysFromToday(30), to: todayInput() };
}

function formatDateTime(iso: string | null): string {
  return formatDateTimeUtil(iso);
}

function formatMinutes(value: string | null): string {
  if (!value) return "—";
  const minutes = Number(value);
  if (!Number.isFinite(minutes) || minutes <= 0) return "—";
  const h = Math.floor(minutes / 60);
  const m = Math.round(minutes % 60);
  if (h === 0) return `${m}м`;
  if (m === 0) return `${h}ч`;
  return `${h}ч ${m}м`;
}

function formatRubles(value: string | null | number): string {
  if (value === null || value === undefined) return "—";
  const num = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(num)) return "—";
  return new Intl.NumberFormat("ru-RU", {
    style: "currency",
    currency: "RUB",
    maximumFractionDigits: 0,
  }).format(num);
}

function statusBadge(status: string | null) {
  if (!status) return <span className="text-muted-foreground">—</span>;
  const lower = status.toLowerCase();
  if (lower === "closed" || lower === "delivered") {
    return <Badge className="bg-emerald-100 text-emerald-700">Доставлен</Badge>;
  }
  if (lower === "onway" || lower === "on_way") {
    return <Badge className="bg-amber-100 text-amber-700">В пути</Badge>;
  }
  if (lower === "cancelled" || lower === "canceled") {
    return <Badge className="bg-slate-200 text-slate-600">Отменён</Badge>;
  }
  return <Badge variant="outline">{status}</Badge>;
}

export function CourierDeliveriesTab() {
  const queryClient = useQueryClient();
  const [preset, setPreset] = useState<RangePreset>("week");
  const [serviceType, setServiceType] = useState<string>("COURIER");
  const [includeCancelled, setIncludeCancelled] = useState(false);
  const [offset, setOffset] = useState(0);

  const range = useMemo(() => presetRange(preset), [preset]);

  const deliveriesQuery = useQuery({
    queryKey: ["courier-iiko-deliveries", range, serviceType, includeCancelled, offset],
    queryFn: () =>
      getCourierIikoDeliveries({
        from: range.from,
        to: range.to,
        service_type: serviceType !== "all" ? serviceType : undefined,
        include_cancelled: includeCancelled,
        limit: PAGE_SIZE,
        offset,
      }),
    staleTime: 30_000,
  });

  const syncMutation = useMutation({
    mutationFn: () => {
      const today = todayInput();
      const syncTo = range.to > today ? today : range.to;
      const syncFrom = range.from > today ? today : range.from;
      return syncCourierIikoDeliveries({ from: syncFrom, to: syncTo });
    },
    onSuccess: () => {
      toast.success("Синхронизация запущена. Обновляю...");
      setTimeout(() => {
        void queryClient.invalidateQueries({ queryKey: ["courier-iiko-deliveries"] });
      }, 1500);
    },
    onError: (error: unknown) => {
      const message = error instanceof Error ? error.message : "Не удалось запустить синхронизацию";
      toast.error(message);
    },
  });

  const items = deliveriesQuery.data?.items ?? [];
  const summary = deliveriesQuery.data?.summary;

  return (
    <div className="space-y-5">
      <div className="grid gap-4 sm:grid-cols-3">
        <Card>
          <CardContent className="pt-6">
            <div className="text-sm text-muted-foreground">Доставок за период</div>
            <div className="mt-2 text-3xl font-semibold">{summary?.count ?? "—"}</div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-6">
            <div className="text-sm text-muted-foreground">Средняя длительность</div>
            <div className="mt-2 text-3xl font-semibold">
              {summary?.avg_minutes != null
                ? `${Math.round(summary.avg_minutes)} мин`
                : "—"}
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-6">
            <div className="text-sm text-muted-foreground">Выручка за период</div>
            <div className="mt-2 text-3xl font-semibold">{formatRubles(summary?.revenue_total ?? null)}</div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardContent className="flex flex-wrap items-end gap-4 pt-6">
          <div className="space-y-1">
            <label className="text-sm font-medium text-muted-foreground">Период</label>
            <Select value={preset} onValueChange={(v) => { setPreset(v as RangePreset); setOffset(0); }}>
              <SelectTrigger className="w-48">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="today">Сегодня</SelectItem>
                <SelectItem value="week">Текущая неделя</SelectItem>
                <SelectItem value="month">Последние 30 дней</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <label className="text-sm font-medium text-muted-foreground">Тип сервиса</label>
            <Select value={serviceType} onValueChange={(v) => { setServiceType(v); setOffset(0); }}>
              <SelectTrigger className="w-48">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">Все</SelectItem>
                <SelectItem value="COURIER">Курьер</SelectItem>
                <SelectItem value="PICKUP">Самовывоз</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="flex items-center gap-2 pb-2">
            <Switch
              checked={includeCancelled}
              onCheckedChange={(checked) => { setIncludeCancelled(checked); setOffset(0); }}
              id="include-cancelled"
            />
            <label htmlFor="include-cancelled" className="text-sm">
              Показывать отменённые
            </label>
          </div>
          <div className="ml-auto flex gap-2">
            <Button
              variant="outline"
              onClick={() => void queryClient.invalidateQueries({ queryKey: ["courier-iiko-deliveries"] })}
              disabled={deliveriesQuery.isFetching}
            >
              <RefreshCw className="h-4 w-4" />
              Обновить
            </Button>
            <Button onClick={() => syncMutation.mutate()} disabled={syncMutation.isPending}>
              {syncMutation.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <RefreshCw className="h-4 w-4" />
              )}
              Синхронизировать
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="pt-6">
          {deliveriesQuery.isPending ? (
            <div className="space-y-2">
              {Array.from({ length: 6 }).map((_, i) => (
                <Skeleton key={i} className="h-12 w-full" />
              ))}
            </div>
          ) : items.length === 0 ? (
            <EmptyState
              title="За период доставок не найдено"
              description='Расширьте диапазон или нажмите "Синхронизировать" для подгрузки из iiko.'
            />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>№ заказа</TableHead>
                  <TableHead>Курьер</TableHead>
                  <TableHead>Статус</TableHead>
                  <TableHead>Открыт</TableHead>
                  <TableHead>Взял</TableHead>
                  <TableHead>Доставил</TableHead>
                  <TableHead>В пути</TableHead>
                  <TableHead className="text-right">Сумма</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {items.map((row: CourierIikoDeliveryRow) => (
                  <TableRow
                    key={row.id}
                    className={
                      row.status &&
                      ["cancelled", "canceled"].includes(row.status.toLowerCase())
                        ? "opacity-60"
                        : undefined
                    }
                  >
                    <TableCell className="font-medium">
                      {row.order_number ?? row.iiko_order_id.slice(0, 8)}
                    </TableCell>
                    <TableCell>{row.courier_employee_name ?? "—"}</TableCell>
                    <TableCell>{statusBadge(row.status)}</TableCell>
                    <TableCell>{formatDateTime(row.opened_at)}</TableCell>
                    <TableCell>{formatDateTime(row.taken_at)}</TableCell>
                    <TableCell>{formatDateTime(row.delivered_at)}</TableCell>
                    <TableCell>{formatMinutes(row.way_duration_minutes)}</TableCell>
                    <TableCell className="text-right">{formatRubles(row.revenue)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}

          {items.length > 0 && (
            <div className="mt-4 flex items-center justify-between">
              <span className="text-sm text-muted-foreground">
                Показано {items.length} записей, начиная с {offset + 1}
              </span>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                >
                  Назад
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={items.length < PAGE_SIZE}
                  onClick={() => setOffset(offset + PAGE_SIZE)}
                >
                  Вперёд
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
