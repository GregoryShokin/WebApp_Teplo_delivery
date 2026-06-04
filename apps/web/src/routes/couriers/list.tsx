import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  CircleOff,
  Pencil,
  RefreshCw,
  Search,
  UsersRound,
  Wrench,
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
import { Badge } from "@/components/ui/badge";
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
import { Textarea } from "@/components/ui/textarea";
import { EmptyState } from "@/components/ui-app/EmptyState";
import { PageHeader } from "@/components/ui-app/PageHeader";
import {
  apiErrorMessage,
  assignAllActiveCouriersPrimary,
  getCourierList,
  setCourierCategory,
  type CourierDepositCategory,
  type CourierDepositStatusFilter,
  type CourierListCategoryFilter,
  type CourierListRow,
} from "@/lib/api";
import { cn } from "@/lib/utils";

import {
  COURIER_STATUS_LABELS,
  formatDate,
  todayInput,
} from "./utils";

type CategoryForm = {
  category: CourierDepositCategory;
  effectiveFrom: string;
  comment: string;
};

type PendingCategoryChange = {
  row: CourierListRow;
};

const CATEGORY_FILTER_LABELS: Record<CourierListCategoryFilter, string> = {
  primary: "Primary",
  secondary: "Secondary",
  uncategorized: "Без категории",
  all: "Все",
};

export function CourierListRoute() {
  const queryClient = useQueryClient();
  const [statusFilter, setStatusFilter] = useState<CourierDepositStatusFilter>("active");
  const [categoryFilter, setCategoryFilter] = useState<CourierListCategoryFilter>("all");
  const [search, setSearch] = useState("");
  const [pendingChange, setPendingChange] = useState<PendingCategoryChange | null>(null);
  const [form, setForm] = useState<CategoryForm>(emptyForm());
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [bulkConfirmOpen, setBulkConfirmOpen] = useState(false);

  const listQuery = useQuery({
    queryKey: ["courier-list", statusFilter, categoryFilter],
    queryFn: () => getCourierList({ status: statusFilter, category: categoryFilter }),
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

  const setCategoryMutation = useMutation({
    mutationFn: (payload: { row: CourierListRow; form: CategoryForm }) =>
      setCourierCategory(payload.row.employee_id, {
        category: payload.form.category,
        comment: payload.form.comment.trim() || null,
        effective_from: payload.form.effectiveFrom,
      }),
    onSuccess: async () => {
      toast.success("Категория курьера обновлена");
      setPendingChange(null);
      setConfirmOpen(false);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["courier-list"] }),
        queryClient.invalidateQueries({ queryKey: ["courier-statistics"] }),
        queryClient.invalidateQueries({ queryKey: ["courier-deposits"] }),
      ]);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось обновить категорию"));
    },
  });

  const bulkMutation = useMutation({
    mutationFn: () => assignAllActiveCouriersPrimary({ effective_from: todayInput() }),
    onSuccess: async (result) => {
      toast.success(`Primary назначен активным курьерам: ${result.updated}`);
      setBulkConfirmOpen(false);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["courier-list"] }),
        queryClient.invalidateQueries({ queryKey: ["courier-statistics"] }),
        queryClient.invalidateQueries({ queryKey: ["courier-deposits"] }),
      ]);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось назначить primary"));
    },
  });

  const summary = listQuery.data?.summary;

  function openCategoryDialog(row: CourierListRow, category: CourierDepositCategory) {
    setPendingChange({ row });
    setForm({
      category,
      effectiveFrom: todayInput(),
      comment: "",
    });
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="Список курьеров"
        description="Справочник курьеров и назначение категорий primary/secondary."
        action={
          <>
            <Button
              onClick={() => void queryClient.invalidateQueries({ queryKey: ["courier-list"] })}
              title="Обновить"
              variant="outline"
            >
              <RefreshCw size={16} aria-hidden="true" />
              Обновить
            </Button>
            <Button onClick={() => setBulkConfirmOpen(true)} title="Development tools" variant="outline">
              <Wrench size={16} aria-hidden="true" />
              Назначить всем primary
              <Badge className="rounded-md" variant="secondary">
                development-tools
              </Badge>
            </Button>
          </>
        }
      />

      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <SummaryCard isLoading={listQuery.isLoading} title="Всего активных" value={summary?.active_total} />
        <SummaryCard isLoading={listQuery.isLoading} title="Primary" value={summary?.primary_total} />
        <SummaryCard isLoading={listQuery.isLoading} title="Secondary" value={summary?.secondary_total} />
        <SummaryCard
          isLoading={listQuery.isLoading}
          title="Уволенных за месяц"
          value={summary?.fired_this_month}
        />
      </section>

      {summary && summary.uncategorized_total > 0 ? (
        <div className="flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          <span>
            {summary.uncategorized_total} курьеров без категории. Назначьте чтобы дисциплина
            считалась корректно.
          </span>
        </div>
      ) : null}

      <section className="space-y-4 rounded-lg border bg-card p-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="grid gap-2">
            <Label>Статус</Label>
            <div className="flex rounded-md border bg-background p-1">
              {(["active", "fired", "all"] as const).map((status) => (
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
                  {COURIER_STATUS_LABELS[status]}
                </button>
              ))}
            </div>
          </div>

          <Label className="grid w-52 gap-2">
            <span>Категория</span>
            <Select
              onValueChange={(value) => setCategoryFilter(value as CourierListCategoryFilter)}
              value={categoryFilter}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(["all", "primary", "secondary", "uncategorized"] as const).map((category) => (
                  <SelectItem key={category} value={category}>
                    {CATEGORY_FILTER_LABELS[category]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Label>

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
        </div>
      </section>

      <section className="overflow-hidden rounded-lg border bg-card">
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
                  <TableHead className="min-w-[170px]">Категория</TableHead>
                  <TableHead>Дата назначения</TableHead>
                  <TableHead>Открытая смена сейчас</TableHead>
                  <TableHead className="text-right">Смен в месяце</TableHead>
                  <TableHead className="w-[140px] text-right">Действия</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((row) => (
                  <TableRow key={row.employee_id}>
                    <TableCell>
                      <div className="font-medium">{row.full_name}</div>
                      <div className="mt-1 text-xs text-muted-foreground">
                        {statusLabel(row.status)}
                      </div>
                    </TableCell>
                    <TableCell className="font-mono text-xs">{row.iiko_id}</TableCell>
                    <TableCell>
                      <Select
                        onValueChange={(value) => {
                          if (value !== "none") {
                            openCategoryDialog(row, value as CourierDepositCategory);
                          }
                        }}
                        value={row.category ?? "none"}
                      >
                        <SelectTrigger className="h-9 w-[150px]">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem disabled value="none">
                            Без категории
                          </SelectItem>
                          <SelectItem value="primary">Primary</SelectItem>
                          <SelectItem value="secondary">Secondary</SelectItem>
                        </SelectContent>
                      </Select>
                    </TableCell>
                    <TableCell>{formatDate(row.category_assigned_at)}</TableCell>
                    <TableCell>
                      <OpenShiftChip open={row.open_shift_now} />
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {row.shifts_in_month}
                    </TableCell>
                    <TableCell className="text-right">
                      <Button
                        onClick={() => openCategoryDialog(row, row.category ?? "primary")}
                        size="sm"
                        variant="outline"
                      >
                        <Pencil size={16} aria-hidden="true" />
                        {row.category ? "Изменить" : "Назначить"}
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </section>

      <Dialog
        open={Boolean(pendingChange)}
        onOpenChange={(open) => {
          if (!open) {
            setPendingChange(null);
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Категория курьера</DialogTitle>
            <DialogDescription>{pendingChange?.row.full_name ?? "Курьер"}</DialogDescription>
          </DialogHeader>
          <div className="grid gap-4">
            <Label className="grid gap-2">
              <span>Категория</span>
              <Select
                onValueChange={(value) =>
                  setForm((current) => ({
                    ...current,
                    category: value as CourierDepositCategory,
                  }))
                }
                value={form.category}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="primary">Primary</SelectItem>
                  <SelectItem value="secondary">Secondary</SelectItem>
                </SelectContent>
              </Select>
            </Label>
            <Label className="grid gap-2">
              <span>Дата начала действия</span>
              <Input
                onChange={(event) =>
                  setForm((current) => ({ ...current, effectiveFrom: event.target.value }))
                }
                type="date"
                value={form.effectiveFrom}
              />
            </Label>
            <Label className="grid gap-2">
              <span>Комментарий</span>
              <Textarea
                onChange={(event) =>
                  setForm((current) => ({ ...current, comment: event.target.value }))
                }
                placeholder="Опционально"
                value={form.comment}
              />
            </Label>
          </div>
          <DialogFooter>
            <Button onClick={() => setPendingChange(null)} type="button" variant="outline">
              Отмена
            </Button>
            <Button
              disabled={!form.effectiveFrom || setCategoryMutation.isPending}
              onClick={() => setConfirmOpen(true)}
              type="button"
            >
              Сохранить
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog onOpenChange={setConfirmOpen} open={confirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Подтвердить смену категории?</AlertDialogTitle>
            <AlertDialogDescription>
              При сохранении предыдущий период категории закроется датой{" "}
              {formatDate(previousDate(form.effectiveFrom))}. Продолжить?
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Отмена</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                if (pendingChange) {
                  setCategoryMutation.mutate({ row: pendingChange.row, form });
                }
              }}
            >
              Продолжить
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog onOpenChange={setBulkConfirmOpen} open={bulkConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Назначить всем активным primary?</AlertDialogTitle>
            <AlertDialogDescription>
              Для каждого активного курьера будет создан период primary с сегодняшней даты.
              Предыдущие открытые периоды закроются вчерашним днём.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Отмена</AlertDialogCancel>
            <AlertDialogAction onClick={() => bulkMutation.mutate()}>
              Назначить
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
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

function CourierListSkeleton() {
  return (
    <div className="space-y-3 p-4">
      {Array.from({ length: 6 }).map((_, index) => (
        <div className="grid grid-cols-[2fr_1fr_1fr_1fr] gap-3" key={index}>
          <Skeleton className="h-10" />
          <Skeleton className="h-10" />
          <Skeleton className="h-10" />
          <Skeleton className="h-10" />
        </div>
      ))}
    </div>
  );
}

function emptyForm(): CategoryForm {
  return {
    category: "primary",
    effectiveFrom: todayInput(),
    comment: "",
  };
}

function statusLabel(status: string) {
  if (status === "active") {
    return "Работает";
  }
  if (status === "requires_setup") {
    return "Резерв";
  }
  return "Уволен";
}

function previousDate(value: string) {
  if (!value) {
    return null;
  }
  const date = new Date(`${value}T00:00:00`);
  date.setDate(date.getDate() - 1);
  return [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, "0"),
    String(date.getDate()).padStart(2, "0"),
  ].join("-");
}
