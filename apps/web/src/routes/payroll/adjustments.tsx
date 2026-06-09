import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Banknote,
  CircleMinus,
  LoaderCircle,
  Lock,
  MoreHorizontal,
  Pencil,
  Plus,
  Search,
  Trash2,
} from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import { EmployeeCombobox } from "@/components/ui-app/EmployeeCombobox";
import { EmptyState } from "@/components/ui-app/EmptyState";
import { PageHeader } from "@/components/ui-app/PageHeader";
import {
  apiErrorMessage,
  createPayrollAdjustment,
  createPayrollAdjustmentCategory,
  deletePayrollAdjustment,
  getEmployees,
  getPayrollAdjustmentCategories,
  getPayrollAdjustments,
  patchPayrollAdjustment,
  type Employee,
  type PayrollAdjustment,
  type PayrollAdjustmentCategory,
  type PayrollAdjustmentPayload,
  type PayrollAdjustmentType,
} from "@/lib/api";
import { cn } from "@/lib/utils";

type PayrollAdjustmentsRouteProps = {
  embedded?: boolean;
  headerSlot?: ReactNode;
  onNavigate: (path: string) => void;
};

type AdjustmentDraft = {
  employee_id: string;
  work_date: string;
  type: PayrollAdjustmentType;
  category_id: string;
  custom_label: string;
  amount: string;
  comment: string;
  mode: "category" | "custom";
};

const typeLabels: Record<PayrollAdjustmentType, string> = {
  bonus: "Премия",
  penalty: "Штраф",
};

const targetPositions = new Set(["Повар", "Кассир"]);

// Премии и штрафы доступны поварам и кассирам, а также сотрудникам-субститутам —
// тем, у кого есть активная подменная роль повара/кассира.
function isAdjustmentEligible(employee: Employee): boolean {
  if (targetPositions.has(employee.position ?? "")) {
    return true;
  }
  return employee.assignments.some(
    (assignment) => assignment.is_substitute && !assignment.is_pending,
  );
}

export function PayrollAdjustmentsRoute({
  embedded = false,
  headerSlot,
  onNavigate,
}: PayrollAdjustmentsRouteProps) {
  void onNavigate;
  const queryClient = useQueryClient();
  const defaultPeriod = useMemo(() => currentPayrollWeek(), []);
  const [typeFilter, setTypeFilter] = useState<PayrollAdjustmentType | "all">("all");
  const [employeeFilter, setEmployeeFilter] = useState(
    () => new URLSearchParams(window.location.search).get("employee") ?? "all",
  );
  const [dateFrom, setDateFrom] = useState(defaultPeriod.start);
  const [dateTo, setDateTo] = useState(defaultPeriod.end);
  const [search, setSearch] = useState("");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingAdjustment, setEditingAdjustment] = useState<PayrollAdjustment | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<PayrollAdjustment | null>(null);

  const employeesQuery = useQuery({
    queryKey: ["employees", "payroll-adjustments"],
    queryFn: () => getEmployees({ status: "all" }),
  });
  const adjustmentsQuery = useQuery({
    queryKey: ["payroll-adjustments", typeFilter, employeeFilter, dateFrom, dateTo],
    queryFn: () =>
      getPayrollAdjustments({
        type: typeFilter,
        employeeId: employeeFilter === "all" ? undefined : employeeFilter,
        dateFrom,
        dateTo,
      }),
  });

  const targetEmployees = useMemo(
    () =>
      (employeesQuery.data ?? [])
        .filter((employee) => employee.status === "active" && isAdjustmentEligible(employee))
        .sort(compareEmployeesForSelect),
    [employeesQuery.data],
  );

  const adjustments = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return [...(adjustmentsQuery.data ?? [])]
      .filter((adjustment) => {
        if (!needle) {
          return true;
        }
        return (
          adjustment.employee_full_name.toLowerCase().includes(needle) ||
          adjustmentLabel(adjustment).toLowerCase().includes(needle)
        );
      })
      .sort((left, right) => right.work_date.localeCompare(left.work_date));
  }, [adjustmentsQuery.data, search]);

  const totals = useMemo(
    () =>
      adjustments.reduce(
        (sum, adjustment) => {
          const amount = numericAmount(adjustment.amount);
          if (adjustment.type === "bonus") {
            sum.bonus += amount;
          } else {
            sum.penalty += amount;
          }
          return sum;
        },
        { bonus: 0, penalty: 0 },
      ),
    [adjustments],
  );

  const saveMutation = useMutation({
    mutationFn: ({ id, payload }: { id?: string; payload: PayrollAdjustmentPayload }) =>
      id ? patchPayrollAdjustment(id, payload) : createPayrollAdjustment(payload),
    onSuccess: async (_adjustment, variables) => {
      toast.success(variables.id ? "Корректировка обновлена" : "Корректировка добавлена");
      setDialogOpen(false);
      setEditingAdjustment(null);
      await queryClient.invalidateQueries({ queryKey: ["payroll-adjustments"] });
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось сохранить корректировку"));
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (adjustment: PayrollAdjustment) => deletePayrollAdjustment(adjustment.id),
    onSuccess: async () => {
      toast.success("Корректировка удалена");
      setDeleteTarget(null);
      await queryClient.invalidateQueries({ queryKey: ["payroll-adjustments"] });
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось удалить корректировку"));
    },
  });

  const columns: Array<DataTableColumn<PayrollAdjustment>> = [
    {
      key: "date",
      header: "Дата",
      cell: (adjustment) => (
        <div className="min-w-[96px] font-medium">{formatDate(adjustment.work_date)}</div>
      ),
    },
    {
      key: "employee",
      header: "Сотрудник",
      cell: (adjustment) => (
        <div className="min-w-[180px]">
          <div className="font-medium">{adjustment.employee_full_name}</div>
          <div className="text-xs text-muted-foreground">{adjustment.employee_position}</div>
        </div>
      ),
    },
    {
      key: "type",
      header: "Тип",
      cell: (adjustment) => <AdjustmentTypeBadge type={adjustment.type} />,
    },
    {
      key: "category",
      header: "Категория",
      cell: (adjustment) => (
        <div className="min-w-[160px] truncate">{adjustmentLabel(adjustment)}</div>
      ),
    },
    {
      key: "amount",
      header: "Сумма",
      className: "font-semibold tabular-nums",
      cell: (adjustment) => formatMoney(numericAmount(adjustment.amount)),
    },
    {
      key: "comment",
      header: "Комментарий",
      cell: (adjustment) => (
        <div className="max-w-[260px] truncate text-sm text-muted-foreground">
          {adjustment.comment || "—"}
        </div>
      ),
    },
    {
      key: "actions",
      header: "",
      className: "text-right",
      cell: (adjustment) => (
        <div className="flex justify-end">
          {adjustment.is_locked ? (
            <Button disabled size="icon" title="Период зафиксирован" type="button" variant="ghost">
              <Lock size={16} aria-hidden="true" />
            </Button>
          ) : (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button size="icon" title="Действия" type="button" variant="ghost">
                  <MoreHorizontal size={16} aria-hidden="true" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem
                  onClick={() => {
                    setEditingAdjustment(adjustment);
                    setDialogOpen(true);
                  }}
                >
                  <Pencil size={15} aria-hidden="true" />
                  Редактировать
                </DropdownMenuItem>
                <DropdownMenuItem onClick={() => setDeleteTarget(adjustment)}>
                  <Trash2 size={15} aria-hidden="true" />
                  Удалить
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>
      ),
    },
  ];

  function openCreateDialog() {
    setEditingAdjustment(null);
    setDialogOpen(true);
  }

  const addButton = (
    <Button onClick={openCreateDialog}>
      <Plus size={16} aria-hidden="true" />
      Добавить
    </Button>
  );

  return (
    <div className="space-y-5">
      {embedded ? (
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <h2 className="text-lg font-semibold tracking-normal">Премии и штрафы</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Ручные корректировки для поваров, кассиров и подменных сотрудников.
            </p>
          </div>
          {addButton}
        </div>
      ) : (
        <>
          <PageHeader
            title="Расчёты ЗП — Премии и штрафы"
            description="Ручные корректировки для поваров, кассиров и подменных сотрудников."
            action={addButton}
          />

          {headerSlot}
        </>
      )}

      <section className="grid gap-3 rounded-lg border bg-card p-3 lg:grid-cols-[180px_minmax(180px,260px)_150px_150px_1fr] lg:items-end">
        <Label className="grid gap-2">
          <span>Тип</span>
          <Select
            onValueChange={(value) => setTypeFilter(value as PayrollAdjustmentType | "all")}
            value={typeFilter}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Все</SelectItem>
              <SelectItem value="bonus">Премии</SelectItem>
              <SelectItem value="penalty">Штрафы</SelectItem>
            </SelectContent>
          </Select>
        </Label>

        <div className="grid gap-2">
          <span className="text-sm font-medium leading-none">Сотрудник</span>
          <EmployeeCombobox
            allOptionLabel="Все сотрудники"
            employees={targetEmployees}
            onChange={setEmployeeFilter}
            value={employeeFilter}
          />
        </div>

        <Label className="grid gap-2">
          <span>С даты</span>
          <Input
            onChange={(event) => setDateFrom(event.target.value)}
            type="date"
            value={dateFrom}
          />
        </Label>
        <Label className="grid gap-2">
          <span>По дату</span>
          <Input onChange={(event) => setDateTo(event.target.value)} type="date" value={dateTo} />
        </Label>

        <Label className="grid gap-2">
          <span>Поиск</span>
          <div className="flex h-10 items-center gap-2 rounded-md border border-input bg-background px-3">
            <Search size={16} className="text-muted-foreground" aria-hidden="true" />
            <input
              className="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Сотрудник или категория"
              value={search}
            />
          </div>
        </Label>
      </section>

      <section className="flex flex-wrap items-center gap-3 rounded-lg border bg-muted/30 px-4 py-3 text-sm">
        <span className="font-medium">Итого за период</span>
        <span>премии {formatMoney(totals.bonus)}</span>
        <span className="text-muted-foreground">·</span>
        <span>штрафы {formatMoney(totals.penalty)}</span>
      </section>

      {adjustments.length === 0 && !adjustmentsQuery.isLoading ? (
        <EmptyState
          icon={<Banknote className="h-5 w-5" aria-hidden="true" />}
          title="Премии и штрафы за выбранный период не зафиксированы"
          description="Добавьте корректировку, чтобы она попала в ближайший пересчёт ЗП."
        />
      ) : (
        <DataTable
          columns={columns}
          rows={adjustments}
          isLoading={adjustmentsQuery.isLoading || employeesQuery.isLoading}
          getRowKey={(adjustment) => adjustment.id}
          emptyMessage="Премии и штрафы за выбранный период не зафиксированы"
        />
      )}

      <AdjustmentDialog
        adjustment={editingAdjustment}
        employees={targetEmployees}
        isPending={saveMutation.isPending}
        onOpenChange={(open) => {
          if (!open && !saveMutation.isPending) {
            setDialogOpen(false);
            setEditingAdjustment(null);
          }
        }}
        onSubmit={(payload) => saveMutation.mutate({ id: editingAdjustment?.id, payload })}
        open={dialogOpen}
      />

      <AlertDialog
        open={Boolean(deleteTarget)}
        onOpenChange={(open) => !open && setDeleteTarget(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Удалить корректировку?</AlertDialogTitle>
            <AlertDialogDescription>
              {deleteTarget
                ? `${formatDate(deleteTarget.work_date)} · ${deleteTarget.employee_full_name} · ${adjustmentLabel(deleteTarget)}`
                : "Корректировка будет удалена из расчёта после пересчёта ЗП."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteMutation.isPending}>Отмена</AlertDialogCancel>
            <AlertDialogAction
              disabled={!deleteTarget || deleteMutation.isPending}
              onClick={() => deleteTarget && deleteMutation.mutate(deleteTarget)}
            >
              {deleteMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <Trash2 size={16} aria-hidden="true" />
              )}
              Удалить
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function AdjustmentDialog({
  adjustment,
  employees,
  isPending,
  onOpenChange,
  onSubmit,
  open,
}: {
  adjustment: PayrollAdjustment | null;
  employees: Employee[];
  isPending: boolean;
  onOpenChange: (open: boolean) => void;
  onSubmit: (payload: PayrollAdjustmentPayload) => void;
  open: boolean;
}) {
  const [draft, setDraft] = useState<AdjustmentDraft>(() =>
    draftFromAdjustment(adjustment, employees),
  );
  const [confirmCreateOpen, setConfirmCreateOpen] = useState(false);
  const [categoryOpen, setCategoryOpen] = useState(false);
  const categoriesQuery = useQuery({
    queryKey: ["payroll-adjustment-categories", draft.type],
    queryFn: () => getPayrollAdjustmentCategories(draft.type),
    enabled: open,
  });

  useEffect(() => {
    if (open) {
      setDraft(draftFromAdjustment(adjustment, employees));
      setConfirmCreateOpen(false);
      setCategoryOpen(false);
    }
  }, [adjustment, employees, open]);

  const categories = categoriesQuery.data ?? [];
  const selectedCategory = categories.find((category) => category.id === draft.category_id) ?? null;
  const canSubmit =
    Boolean(draft.employee_id) &&
    Boolean(draft.work_date) &&
    numericAmount(draft.amount) > 0 &&
    (draft.mode === "custom" ? draft.custom_label.trim().length > 0 : Boolean(draft.category_id)) &&
    !isPending;

  function payload(): PayrollAdjustmentPayload {
    return {
      employee_id: draft.employee_id,
      work_date: draft.work_date,
      type: draft.type,
      amount: decimalInputPayload(draft.amount),
      comment: draft.comment.trim() || null,
      category_id: draft.mode === "category" ? draft.category_id : null,
      custom_label: draft.mode === "custom" ? draft.custom_label.trim() : null,
    };
  }

  function save() {
    if (!canSubmit) {
      return;
    }
    if (!adjustment) {
      setConfirmCreateOpen(true);
      return;
    }
    onSubmit(payload());
  }

  function setType(type: PayrollAdjustmentType) {
    setDraft((current) => ({
      ...current,
      type,
      category_id: "",
      custom_label: "",
      mode: "category",
      amount: "",
    }));
  }

  function selectCategory(value: string) {
    if (value === "__custom__") {
      setDraft((current) => ({ ...current, category_id: "", custom_label: "", mode: "custom" }));
      return;
    }
    if (value === "__new__") {
      setCategoryOpen(true);
      return;
    }
    const category = categories.find((item) => item.id === value);
    setDraft((current) => ({
      ...current,
      category_id: value,
      custom_label: "",
      mode: "category",
      amount: category?.default_amount ?? current.amount,
    }));
  }

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>
              {adjustment ? "Редактировать корректировку" : "Новая премия / штраф"}
            </DialogTitle>
            <DialogDescription>
              Дата должна относиться к открытому или пересчитываемому периоду ЗП.
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-4">
            <div className="grid gap-2">
              <span className="text-sm font-medium">Тип</span>
              <div className="grid gap-2 sm:grid-cols-2">
                {(["bonus", "penalty"] as PayrollAdjustmentType[]).map((type) => (
                  <label
                    className={cn(
                      "flex items-center gap-2 rounded-md border bg-background px-3 py-2 text-sm",
                      draft.type === type ? "border-primary" : undefined,
                    )}
                    key={type}
                  >
                    <input
                      checked={draft.type === type}
                      disabled={isPending}
                      name="adjustment-type"
                      onChange={() => setType(type)}
                      type="radio"
                    />
                    <span>{typeLabels[type]}</span>
                  </label>
                ))}
              </div>
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <div className="grid gap-2">
                <span className="text-sm font-medium leading-none">Сотрудник</span>
                <EmployeeCombobox
                  disabled={isPending}
                  employees={employees}
                  onChange={(value) =>
                    setDraft((current) => ({ ...current, employee_id: value }))
                  }
                  value={draft.employee_id}
                />
              </div>

              <Label className="grid gap-2">
                <span>Дата</span>
                <Input
                  disabled={isPending}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, work_date: event.target.value }))
                  }
                  type="date"
                  value={draft.work_date}
                />
              </Label>
            </div>

            <div className="grid gap-4 sm:grid-cols-[1fr_160px]">
              <Label className="grid gap-2">
                <span>Категория</span>
                <Select
                  disabled={isPending || categoriesQuery.isLoading}
                  onValueChange={selectCategory}
                  value={draft.mode === "custom" ? "__custom__" : draft.category_id}
                >
                  <SelectTrigger>
                    <SelectValue
                      placeholder={categoriesQuery.isLoading ? "Загрузка..." : "Выберите категорию"}
                    />
                  </SelectTrigger>
                  <SelectContent>
                    {categories.map((category) => (
                      <SelectItem value={category.id} key={category.id}>
                        {category.display_name}
                      </SelectItem>
                    ))}
                    <SelectItem value="__custom__">Другое</SelectItem>
                    <SelectItem value="__new__">+ Добавить категорию</SelectItem>
                  </SelectContent>
                </Select>
              </Label>

              <Label className="grid gap-2">
                <span>Сумма, ₽</span>
                <Input
                  disabled={isPending}
                  inputMode="decimal"
                  min={0}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, amount: event.target.value }))
                  }
                  placeholder={selectedCategory?.default_amount ?? "0"}
                  step="0.01"
                  type="number"
                  value={draft.amount}
                />
              </Label>
            </div>

            {draft.mode === "custom" ? (
              <Label className="grid gap-2">
                <span>Название</span>
                <Input
                  disabled={isPending}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, custom_label: event.target.value }))
                  }
                  value={draft.custom_label}
                />
              </Label>
            ) : null}

            <Label className="grid gap-2">
              <span>Комментарий</span>
              <textarea
                className="min-h-24 rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                disabled={isPending}
                onChange={(event) =>
                  setDraft((current) => ({ ...current, comment: event.target.value }))
                }
                value={draft.comment}
              />
            </Label>

            {adjustment ? (
              <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
                Изменение затронет существующий расчёт. После сохранения пересчитайте run.
              </div>
            ) : null}
          </div>

          <DialogFooter>
            <Button
              disabled={isPending}
              onClick={() => onOpenChange(false)}
              type="button"
              variant="outline"
            >
              Отмена
            </Button>
            <Button disabled={!canSubmit} onClick={save} type="button">
              {isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <Plus size={16} aria-hidden="true" />
              )}
              Сохранить
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <CategoryDialog
        onCreated={(category) => {
          setDraft((current) => ({
            ...current,
            category_id: category.id,
            custom_label: "",
            mode: "category",
            amount: category.default_amount ?? current.amount,
          }));
        }}
        onOpenChange={setCategoryOpen}
        open={categoryOpen}
        type={draft.type}
      />

      <AlertDialog open={confirmCreateOpen} onOpenChange={setConfirmCreateOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Добавить корректировку?</AlertDialogTitle>
            <AlertDialogDescription>
              {typeLabels[draft.type]} {formatMoney(numericAmount(draft.amount))} попадёт в расчёт
              после пересчёта периода.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isPending}>Назад</AlertDialogCancel>
            <AlertDialogAction
              disabled={!canSubmit || isPending}
              onClick={() => {
                setConfirmCreateOpen(false);
                onSubmit(payload());
              }}
            >
              Создать
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

function CategoryDialog({
  onCreated,
  onOpenChange,
  open,
  type,
}: {
  onCreated: (category: PayrollAdjustmentCategory) => void;
  onOpenChange: (open: boolean) => void;
  open: boolean;
  type: PayrollAdjustmentType;
}) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [amount, setAmount] = useState("");
  const [description, setDescription] = useState("");
  const mutation = useMutation({
    mutationFn: () =>
      createPayrollAdjustmentCategory({
        type,
        display_name: name.trim(),
        default_amount: amount.trim() ? decimalInputPayload(amount) : null,
        description: description.trim() || null,
      }),
    onSuccess: async (category) => {
      toast.success("Категория добавлена");
      onCreated(category);
      onOpenChange(false);
      setName("");
      setAmount("");
      setDescription("");
      await queryClient.invalidateQueries({ queryKey: ["payroll-adjustment-categories"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось создать категорию")),
  });
  const canSubmit = name.trim().length > 0 && !mutation.isPending;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Добавить категорию</DialogTitle>
          <DialogDescription>
            {typeLabels[type]} будет доступна в списке категорий.
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-4">
          <Label className="grid gap-2">
            <span>Название</span>
            <Input onChange={(event) => setName(event.target.value)} value={name} />
          </Label>
          <Label className="grid gap-2">
            <span>Сумма по умолчанию, ₽</span>
            <Input
              inputMode="decimal"
              onChange={(event) => setAmount(event.target.value)}
              step="0.01"
              type="number"
              value={amount}
            />
          </Label>
          <Label className="grid gap-2">
            <span>Описание</span>
            <textarea
              className="min-h-20 rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
              onChange={(event) => setDescription(event.target.value)}
              value={description}
            />
          </Label>
        </div>
        <DialogFooter>
          <Button
            disabled={mutation.isPending}
            onClick={() => onOpenChange(false)}
            type="button"
            variant="outline"
          >
            Отмена
          </Button>
          <Button disabled={!canSubmit} onClick={() => mutation.mutate()} type="button">
            {mutation.isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : (
              <Plus size={16} aria-hidden="true" />
            )}
            Добавить
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function AdjustmentTypeBadge({ type }: { type: PayrollAdjustmentType }) {
  const isBonus = type === "bonus";
  return (
    <Badge
      className={cn(
        "rounded-md shadow-none",
        isBonus
          ? "border-emerald-200 bg-emerald-50 text-emerald-700"
          : "border-rose-200 bg-rose-50 text-rose-700",
      )}
      variant="outline"
    >
      {isBonus ? (
        <Banknote size={13} className="mr-1" aria-hidden="true" />
      ) : (
        <CircleMinus size={13} className="mr-1" aria-hidden="true" />
      )}
      {typeLabels[type]}
    </Badge>
  );
}

function draftFromAdjustment(
  adjustment: PayrollAdjustment | null,
  employees: Employee[],
): AdjustmentDraft {
  if (!adjustment) {
    return {
      employee_id: employees[0]?.id ?? "",
      work_date: todayDateInputValue(),
      type: "bonus",
      category_id: "",
      custom_label: "",
      amount: "",
      comment: "",
      mode: "category",
    };
  }
  return {
    employee_id: adjustment.employee_id,
    work_date: adjustment.work_date,
    type: adjustment.type,
    category_id: adjustment.category_id ?? "",
    custom_label: adjustment.custom_label ?? "",
    amount: adjustment.amount,
    comment: adjustment.comment ?? "",
    mode: adjustment.category_id ? "category" : "custom",
  };
}

function adjustmentLabel(adjustment: PayrollAdjustment) {
  return adjustment.category_display_name || adjustment.custom_label || "Без категории";
}

function compareEmployeesForSelect(left: Employee, right: Employee) {
  if (left.status !== right.status) {
    if (left.status === "active") {
      return -1;
    }
    if (right.status === "active") {
      return 1;
    }
  }
  return left.full_name.localeCompare(right.full_name, "ru");
}

function currentPayrollWeek() {
  const today = new Date();
  const day = today.getDay();
  const daysSinceTuesday = (day + 5) % 7;
  const start = new Date(today);
  start.setDate(today.getDate() - daysSinceTuesday);
  const end = new Date(start);
  end.setDate(start.getDate() + 6);
  return { start: dateInputValue(start), end: dateInputValue(end) };
}

function todayDateInputValue() {
  return dateInputValue(new Date());
}

function dateInputValue(date: Date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function formatDate(value: string | null | undefined) {
  if (!value) {
    return "—";
  }
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "2-digit",
  }).format(new Date(`${value}T00:00:00`));
}

function formatMoney(value: number) {
  return new Intl.NumberFormat("ru-RU", {
    maximumFractionDigits: 0,
    style: "currency",
    currency: "RUB",
  }).format(value);
}

function numericAmount(value: string | number | null | undefined) {
  const amount = Number(String(value ?? "0").replace(",", "."));
  return Number.isFinite(amount) ? amount : 0;
}

function decimalInputPayload(value: string) {
  return String(numericAmount(value).toFixed(2));
}
