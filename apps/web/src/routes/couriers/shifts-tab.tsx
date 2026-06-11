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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { EmptyState } from "@/components/ui-app/EmptyState";
import { getCourierIikoShifts, syncCourierAttendance, type CourierIikoShiftRow } from "@/lib/api";

const PAGE_SIZE = 50;

type RangePreset = "today" | "week" | "month" | "custom";
type OpenFilter = "all" | "open" | "closed";

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

function formatWorked(minutes: number | null): string {
  if (minutes === null || minutes < 0) return "—";
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  if (h === 0) return `${m}м`;
  if (m === 0) return `${h}ч`;
  return `${h}ч ${m}м`;
}

function formatDateTime(iso: string | null): string {
  return formatDateTimeUtil(iso);
}

function categoryBadge(category: "primary" | "secondary" | null) {
  if (category === "primary") {
    return <Badge className="bg-emerald-100 text-emerald-700">1 Основная</Badge>;
  }
  if (category === "secondary") {
    return <Badge className="bg-sky-100 text-sky-700">2 Второстепенная</Badge>;
  }
  return <span className="text-muted-foreground">—</span>;
}

function attendanceLabel(type: string): string {
  if (type === "P") return "Явка";
  return type || "—";
}

export function CourierShiftsTab() {
  const queryClient = useQueryClient();
  const [preset, setPreset] = useState<RangePreset>("week");
  const [openFilter, setOpenFilter] = useState<OpenFilter>("all");
  const [offset, setOffset] = useState(0);

  const range = useMemo(() => presetRange(preset), [preset]);

  const shiftsQuery = useQuery({
    queryKey: ["courier-iiko-shifts", range, openFilter, offset],
    queryFn: () =>
      getCourierIikoShifts({
        from: range.from,
        to: range.to,
        is_open: openFilter === "open" ? true : openFilter === "closed" ? false : undefined,
        limit: PAGE_SIZE,
        offset,
      }),
    staleTime: 30_000,
  });

  const syncMutation = useMutation({
    mutationFn: () => syncCourierAttendance(range.from, range.to),
    onSuccess: () => {
      toast.success("Синхронизация запущена. Обновляю...");
      setTimeout(() => {
        void queryClient.invalidateQueries({ queryKey: ["courier-iiko-shifts"] });
      }, 1500);
    },
    onError: (error: unknown) => {
      const message = error instanceof Error ? error.message : "Не удалось запустить синхронизацию";
      toast.error(message);
    },
  });

  const handleSync = () => {
    syncMutation.mutate();
  };

  const items = shiftsQuery.data?.items ?? [];

  return (
    <div className="space-y-5">
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
            <label className="text-sm font-medium text-muted-foreground">Тип</label>
            <Select value={openFilter} onValueChange={(v) => { setOpenFilter(v as OpenFilter); setOffset(0); }}>
              <SelectTrigger className="w-48">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">Все смены</SelectItem>
                <SelectItem value="open">Только открытые</SelectItem>
                <SelectItem value="closed">Только закрытые</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="ml-auto flex gap-2">
            <Button
              variant="outline"
              onClick={() => void queryClient.invalidateQueries({ queryKey: ["courier-iiko-shifts"] })}
              disabled={shiftsQuery.isFetching}
            >
              <RefreshCw className="h-4 w-4" />
              Обновить
            </Button>
            <Button onClick={handleSync} disabled={syncMutation.isPending}>
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
          {shiftsQuery.isPending ? (
            <div className="space-y-2">
              {Array.from({ length: 6 }).map((_, i) => (
                <Skeleton key={i} className="h-12 w-full" />
              ))}
            </div>
          ) : items.length === 0 ? (
            <EmptyState
              title="За период смен не найдено"
              description='Расширьте диапазон или нажмите "Синхронизировать" для подгрузки из iiko.'
            />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Курьер</TableHead>
                  <TableHead>Открыта</TableHead>
                  <TableHead>Закрыта</TableHead>
                  <TableHead>Длительность</TableHead>
                  <TableHead>Статус</TableHead>
                  <TableHead>Категория</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {items.map((row: CourierIikoShiftRow) => (
                  <TableRow key={row.id}>
                    <TableCell className="font-medium">
                      {row.employee_name ?? <span className="text-muted-foreground">iiko {row.iiko_employee_id.slice(0, 8)}</span>}
                    </TableCell>
                    <TableCell>{formatDateTime(row.opened_at)}</TableCell>
                    <TableCell>
                      {row.is_open ? (
                        <Badge className="bg-teal-100 text-teal-700">
                          <span className="mr-1 inline-block h-2 w-2 animate-pulse rounded-full bg-teal-600" />
                          Сейчас открыта
                        </Badge>
                      ) : (
                        formatDateTime(row.closed_at)
                      )}
                    </TableCell>
                    <TableCell>{formatWorked(row.worked_minutes)}</TableCell>
                    <TableCell>
                      <Badge variant="outline">{attendanceLabel(row.attendance_type)}</Badge>
                    </TableCell>
                    <TableCell>{categoryBadge(row.match_category)}</TableCell>
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
