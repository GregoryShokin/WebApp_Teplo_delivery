import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  DatabaseZap,
  Grid2X2,
  List,
  LoaderCircle,
  Save,
  Search,
  ShieldAlert,
  UserPlus,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import { EmptyState } from "@/components/ui-app/EmptyState";
import { PageHeader } from "@/components/ui-app/PageHeader";
import { StatusBadge } from "@/components/ui-app/StatusBadge";
import {
  type CookingStation,
  type Employee,
  type EmployeeCategory,
  type EmployeePatch,
  type EmployeeStatus,
  getEmployees,
  patchEmployee,
  syncEmployees,
} from "@/lib/api";
import {
  COOKING_STATION_LABELS,
  EMPLOYEE_CATEGORY_LABELS,
  EMPLOYEE_STATUS_LABELS,
} from "@/lib/i18n/employee";
import { cn } from "@/lib/utils";

const statusOptions: Array<EmployeeStatus | "all"> = ["all", "active", "requires_setup", "inactive"];
const categoryOptions = Object.keys(EMPLOYEE_CATEGORY_LABELS) as EmployeeCategory[];
const cookingStationOptions = Object.keys(COOKING_STATION_LABELS) as CookingStation[];

type Draft = Pick<
  Employee,
  "position" | "category" | "default_cooking_station" | "is_senior" | "is_deputy_senior"
>;
type ViewMode = "grid" | "table";
type StaffGroupFilter = "all" | "cook" | "staff";

export function StaffRoute() {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<EmployeeStatus | "all">("all");
  const [category, setCategory] = useState<EmployeeCategory | "all">("all");
  const [group, setGroup] = useState<StaffGroupFilter>("all");
  const [cookingStation, setCookingStation] = useState<CookingStation | "all">("all");
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [viewMode, setViewMode] = useState<ViewMode>("grid");
  const [selectedEmployeeId, setSelectedEmployeeId] = useState<string | null>(null);

  useEffect(() => {
    const timeoutId = window.setTimeout(() => setDebouncedSearch(search), 250);
    return () => window.clearTimeout(timeoutId);
  }, [search]);

  useEffect(() => {
    if (group !== "cook") {
      setCookingStation("all");
    }
  }, [group]);

  const employeesQuery = useQuery({
    queryKey: ["employees", status, category],
    queryFn: () =>
      getEmployees({
        status,
        category: category === "all" ? undefined : category,
      }),
  });

  const allEmployeesQuery = useQuery({
    queryKey: ["employees", "staff-filter-options"],
    queryFn: () => getEmployees({ status: "all" }),
  });

  const syncMutation = useMutation({
    mutationFn: syncEmployees,
    onSuccess: (result) => {
      toast.success(
        `Создано ${result.created}, обновлено ${result.updated}, деактивировано ${result.deactivated}`,
      );
      void queryClient.invalidateQueries({ queryKey: ["employees"] });
    },
    onError: (error) => {
      toast.error((error as Error).message);
    },
  });

  const optionEmployees = useMemo(
    () => allEmployeesQuery.data ?? employeesQuery.data ?? [],
    [allEmployeesQuery.data, employeesQuery.data],
  );

  const employees = useMemo(() => {
    const needle = debouncedSearch.trim().toLowerCase();
    return (employeesQuery.data ?? []).filter((employee) => {
      const matchesSearch = needle ? employee.full_name.toLowerCase().includes(needle) : true;
      const employeeIsCook = isCookPosition(employee.position);
      const matchesGroup =
        group === "all" ? true : group === "cook" ? employeeIsCook : !employeeIsCook;
      const matchesStation =
        group === "cook" && cookingStation !== "all"
          ? employee.default_cooking_station === cookingStation
          : true;
      return matchesSearch && matchesGroup && matchesStation;
    });
  }, [cookingStation, debouncedSearch, employeesQuery.data, group]);

  const selectedEmployee = useMemo(
    () => optionEmployees.find((employee) => employee.id === selectedEmployeeId) ?? null,
    [optionEmployees, selectedEmployeeId],
  );

  const stats = useMemo(() => {
    const active = optionEmployees.filter((employee) => employee.status === "active").length;
    const needsSetup = optionEmployees.filter(
      (employee) => employee.status === "requires_setup",
    ).length;
    return { active, needsSetup, total: optionEmployees.length };
  }, [optionEmployees]);

  const isInitialEmpty =
    !allEmployeesQuery.isLoading &&
    !employeesQuery.isLoading &&
    optionEmployees.length === 0 &&
    status === "all" &&
    category === "all" &&
    group === "all" &&
    cookingStation === "all" &&
    debouncedSearch.trim() === "";

  const tableColumns = useMemo<Array<DataTableColumn<Employee>>>(
    () => [
      {
        key: "employee",
        header: "Сотрудник",
        cell: (employee) => (
          <div className="flex min-w-[220px] items-center gap-3">
            <EmployeeAvatar employee={employee} />
            <div className="min-w-0">
              <div className="truncate font-medium">{employee.full_name}</div>
              <div className="truncate text-sm text-muted-foreground">
                {employee.position || "Должность не указана"}
              </div>
            </div>
          </div>
        ),
      },
      {
        key: "category",
        header: "Категория",
        cell: (employee) => categoryLabel(employee.category),
      },
      {
        key: "default_cooking_station",
        header: "Цех",
        cell: (employee) => stationLabel(employee.default_cooking_station),
      },
      {
        key: "allowances",
        header: "Надбавки",
        cell: (employee) => <EmployeeTags employee={employee} compact />,
      },
      {
        key: "status",
        header: "Статус",
        cell: (employee) => <StatusBadge status={employee.status} />,
      },
      {
        key: "sync",
        header: "Синхронизация",
        cell: (employee) => (
          <span className="text-muted-foreground">{formatDateTime(employee.iiko_sync_at)}</span>
        ),
      },
      {
        key: "action",
        header: "",
        className: "text-right",
        cell: (employee) => (
          <Button
            onClick={(event) => {
              event.stopPropagation();
              setSelectedEmployeeId(employee.id);
            }}
            size="sm"
            variant="outline"
          >
            Открыть
          </Button>
        ),
      },
    ],
    [],
  );

  return (
    <div className="space-y-5">
      <PageHeader
        title="Штат"
        description="Реестр сотрудников. Имена синхронизируются из iiko."
        action={
          <Button onClick={() => syncMutation.mutate()} disabled={syncMutation.isPending}>
            {syncMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : (
              <DatabaseZap size={16} aria-hidden="true" />
            )}
            Загрузить из iiko
          </Button>
        }
      />

      <section className="grid gap-3 md:grid-cols-3">
        <StaffMetric label="Всего в реестре" value={stats.total} />
        <StaffMetric label="Активны" value={stats.active} tone="success" />
        <StaffMetric label="Требуют проверки" value={stats.needsSetup} tone="warning" />
      </section>

      <section className="grid gap-3 rounded-lg border bg-card p-3 lg:grid-cols-[minmax(220px,1fr)_160px_160px_160px_160px_auto] lg:items-end">
        <Label className="grid gap-1 text-sm">
          <span className="text-xs font-medium uppercase text-muted-foreground">Поиск</span>
          <div className="flex h-10 items-center gap-2 rounded-md border border-input bg-background px-3">
            <Search size={16} className="text-muted-foreground" aria-hidden="true" />
            <input
              className="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Имя сотрудника"
              value={search}
            />
          </div>
        </Label>

        <Label className="grid gap-1 text-sm">
          <span className="text-xs font-medium uppercase text-muted-foreground">Статус</span>
          <Select
            onValueChange={(value) => setStatus(value as EmployeeStatus | "all")}
            value={status}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {statusOptions.map((option) => (
                <SelectItem value={option} key={option}>
                  {option === "all" ? "Все" : EMPLOYEE_STATUS_LABELS[option]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Label>

        <Label className="grid gap-1 text-sm">
          <span className="text-xs font-medium uppercase text-muted-foreground">Группа</span>
          <Select onValueChange={(value) => setGroup(value as StaffGroupFilter)} value={group}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Все</SelectItem>
              <SelectItem value="cook">Повара</SelectItem>
              <SelectItem value="staff">Администраторы</SelectItem>
            </SelectContent>
          </Select>
        </Label>

        <Label className="grid gap-1 text-sm">
          <span className="text-xs font-medium uppercase text-muted-foreground">Категория</span>
          <Select
            onValueChange={(value) => setCategory(value as EmployeeCategory | "all")}
            value={category}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Все</SelectItem>
              {categoryOptions.map((value) => (
                <SelectItem value={value} key={value}>
                  {EMPLOYEE_CATEGORY_LABELS[value]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Label>

        <Label className="grid gap-1 text-sm">
          <span className="text-xs font-medium uppercase text-muted-foreground">Цех</span>
          <Select
            disabled={group !== "cook"}
            onValueChange={(value) => setCookingStation(value as CookingStation | "all")}
            value={cookingStation}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Все</SelectItem>
              {cookingStationOptions.map((value) => (
                <SelectItem value={value} key={value}>
                  {COOKING_STATION_LABELS[value]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Label>

        <Tabs onValueChange={(value) => setViewMode(value as ViewMode)} value={viewMode}>
          <TabsList>
            <TabsTrigger className="gap-2" value="grid">
              <Grid2X2 size={15} aria-hidden="true" />
              Сетка
            </TabsTrigger>
            <TabsTrigger className="gap-2" value="table">
              <List size={15} aria-hidden="true" />
              Таблица
            </TabsTrigger>
          </TabsList>
        </Tabs>
      </section>

      {employeesQuery.isError ? (
        <div className="flex items-center gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          <ShieldAlert size={16} aria-hidden="true" />
          {(employeesQuery.error as Error).message}
        </div>
      ) : null}

      {isInitialEmpty ? (
        <EmptyState
          icon={<UserPlus className="h-5 w-5" aria-hidden="true" />}
          title="Сотрудники не загружены."
          description="Нажмите «Загрузить из iiko» для синхронизации."
          action={
            <Button
              className="h-11 px-5"
              onClick={() => syncMutation.mutate()}
              disabled={syncMutation.isPending}
            >
              {syncMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <DatabaseZap size={16} aria-hidden="true" />
              )}
              Загрузить из iiko
            </Button>
          }
        />
      ) : viewMode === "grid" ? (
        <StaffGrid
          employees={employees}
          isLoading={employeesQuery.isLoading}
          onSelect={(employee) => setSelectedEmployeeId(employee.id)}
        />
      ) : (
        <DataTable
          columns={tableColumns}
          rows={employees}
          isLoading={employeesQuery.isLoading}
          getRowKey={(employee) => employee.id}
          onRowClick={(employee) => setSelectedEmployeeId(employee.id)}
          emptyMessage="Сотрудники по выбранным фильтрам не найдены"
        />
      )}

      <Sheet
        open={Boolean(selectedEmployee)}
        onOpenChange={(open) => {
          if (!open) {
            setSelectedEmployeeId(null);
          }
        }}
      >
        <SheetContent className="w-full overflow-y-auto sm:max-w-xl" side="right">
          {selectedEmployee ? <StaffEditor employee={selectedEmployee} /> : null}
        </SheetContent>
      </Sheet>
    </div>
  );
}

function StaffGrid({
  employees,
  isLoading,
  onSelect,
}: {
  employees: Employee[];
  isLoading: boolean;
  onSelect: (employee: Employee) => void;
}) {
  if (isLoading) {
    return (
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {Array.from({ length: 6 }).map((_, index) => (
          <Card className="h-[172px] animate-pulse shadow-none" key={index}>
            <CardContent className="p-4">
              <div className="h-10 w-10 rounded-full bg-muted" />
              <div className="mt-4 h-5 w-2/3 rounded bg-muted" />
              <div className="mt-2 h-4 w-1/2 rounded bg-muted" />
              <div className="mt-5 h-6 w-28 rounded bg-muted" />
            </CardContent>
          </Card>
        ))}
      </div>
    );
  }

  if (employees.length === 0) {
    return (
      <EmptyState
        icon={<Search className="h-5 w-5" aria-hidden="true" />}
        title="Сотрудники не найдены"
        description="Измените поиск или фильтры."
      />
    );
  }

  return (
    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
      {employees.map((employee) => (
        <button
          className="group rounded-lg text-left outline-none focus-visible:ring-2 focus-visible:ring-ring"
          key={employee.id}
          onClick={() => onSelect(employee)}
          type="button"
        >
          <Card className="h-full shadow-none transition-colors group-hover:border-primary/40 group-hover:bg-accent/40">
            <CardContent className="grid h-full gap-4 p-4">
              <div className="flex items-start justify-between gap-3">
                <EmployeeAvatar employee={employee} />
                <StatusBadge status={employee.status} />
              </div>
              <div className="min-w-0">
                <div className="truncate text-lg font-semibold leading-6">{employee.full_name}</div>
                <div className="mt-1 truncate text-sm text-muted-foreground">
                  {employee.position || "Должность не указана"}
                </div>
              </div>
              <EmployeeTags employee={employee} />
            </CardContent>
          </Card>
        </button>
      ))}
    </div>
  );
}

function StaffEditor({ employee }: { employee: Employee }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<Draft>(() => toDraft(employee));

  useEffect(() => {
    setDraft(toDraft(employee));
  }, [employee]);

  const mutation = useMutation({
    mutationFn: (patch: EmployeePatch) => patchEmployee(employee.id, patch),
    onSuccess: (updatedEmployee) => {
      setDraft(toDraft(updatedEmployee));
      toast.success("Карточка сотрудника обновлена");
      void queryClient.invalidateQueries({ queryKey: ["employees"] });
    },
    onError: (error) => {
      toast.error((error as Error).message);
    },
  });

  const dirty =
    draft.position !== employee.position ||
    draft.category !== employee.category ||
    draft.default_cooking_station !== employee.default_cooking_station ||
    draft.is_senior !== employee.is_senior ||
    draft.is_deputy_senior !== employee.is_deputy_senior;
  const isCook = isCookPosition(draft.position);

  function autosaveField<K extends keyof Draft>(field: K, value: Draft[K]) {
    setDraft((current) => ({ ...current, [field]: value }));
    mutation.mutate({ [field]: value } as EmployeePatch);
  }

  return (
    <div className="space-y-5">
      <SheetHeader>
        <SheetTitle className="pr-8">Карточка сотрудника</SheetTitle>
        <SheetDescription>Поля реестра, которые используются графиком и зарплатой.</SheetDescription>
      </SheetHeader>

      <div className="flex items-start gap-3 rounded-lg border bg-card p-4">
        <EmployeeAvatar employee={employee} />
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="truncate text-lg font-semibold">{employee.full_name}</div>
              <div className="mt-1 text-sm text-muted-foreground">
                {employee.position || "Должность не указана"}
              </div>
            </div>
            <StatusBadge status={employee.status} />
          </div>
          <div className="mt-3">
            <EmployeeTags employee={employee} />
          </div>
        </div>
      </div>

      <div className="grid gap-4">
        <Label className="grid gap-2">
          <span>Имя</span>
          <div className="flex items-center gap-2">
            <Input disabled title="Управляется iiko" value={employee.full_name} />
            <span
              className="inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-md border text-primary"
              title="Управляется iiko"
            >
              <DatabaseZap size={16} aria-hidden="true" />
            </span>
          </div>
        </Label>

        <Label className="grid gap-2">
          <span>Должность</span>
          <Input
            onChange={(event) => {
              const position = event.target.value || null;
              setDraft({
                ...draft,
                position,
                default_cooking_station: isCookPosition(position)
                  ? draft.default_cooking_station
                  : null,
              });
            }}
            placeholder="Например, повар"
            value={draft.position ?? ""}
          />
        </Label>

        <Label className="grid gap-2">
          <span>Категория</span>
          <Select
            disabled={mutation.isPending}
            onValueChange={(value) => autosaveField("category", value as EmployeeCategory)}
            value={draft.category ?? undefined}
          >
            <SelectTrigger>
              <SelectValue placeholder="Выберите категорию" />
            </SelectTrigger>
            <SelectContent>
              {categoryOptions.map((value) => (
                <SelectItem value={value} key={value}>
                  {EMPLOYEE_CATEGORY_LABELS[value]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Label>

        {isCook ? (
          <Label className="grid gap-2">
            <span>Цех</span>
            <Select
              disabled={mutation.isPending}
              onValueChange={(value) =>
                autosaveField("default_cooking_station", value as CookingStation)
              }
              value={draft.default_cooking_station ?? undefined}
            >
              <SelectTrigger
                className={cn(!draft.default_cooking_station && "border-amber-300")}
              >
                <SelectValue placeholder="Выберите цех" />
              </SelectTrigger>
              <SelectContent>
                {cookingStationOptions.map((value) => (
                  <SelectItem value={value} key={value}>
                    {COOKING_STATION_LABELS[value]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {!draft.default_cooking_station ? (
              <span className="text-sm text-amber-700">Выберите цех</span>
            ) : null}
          </Label>
        ) : null}

        <Label className="grid gap-2">
          <span>Статус</span>
          <div
            className="flex h-10 items-center rounded-md border border-input bg-muted/30 px-3"
            title="Рассчитывается автоматически"
          >
            <StatusBadge status={employee.status} />
          </div>
        </Label>

        <div className="grid gap-3 rounded-lg border bg-card p-4">
          <div className="text-sm font-medium">Надбавки</div>
          <label className="flex items-center justify-between gap-3 rounded-md border bg-background px-3 py-2 text-sm">
            <span>Старший</span>
            <input
              checked={draft.is_senior}
              onChange={(event) => setDraft({ ...draft, is_senior: event.target.checked })}
              type="checkbox"
            />
          </label>
          <label className="flex items-center justify-between gap-3 rounded-md border bg-background px-3 py-2 text-sm">
            <span>Заместитель старшего</span>
            <input
              checked={draft.is_deputy_senior}
              onChange={(event) => setDraft({ ...draft, is_deputy_senior: event.target.checked })}
              type="checkbox"
            />
          </label>
        </div>
      </div>

      <div className="grid gap-2 rounded-lg border bg-muted/30 p-4 text-sm">
        <InfoRow label="Синхронизация" value={formatDateTime(employee.iiko_sync_at)} />
        <InfoRow label="Создан" value={formatDateTime(employee.created_at)} />
        <InfoRow label="Обновлён" value={formatDateTime(employee.updated_at)} />
      </div>

      <Button
        className="w-full"
        disabled={!dirty || mutation.isPending}
        onClick={() => mutation.mutate(draft)}
      >
        {mutation.isPending ? (
          <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
        ) : (
          <Save size={16} aria-hidden="true" />
        )}
        Сохранить изменения
      </Button>
    </div>
  );
}

function EmployeeAvatar({ employee }: { employee: Employee }) {
  return (
    <Avatar className="h-11 w-11 border">
      <AvatarFallback className="bg-primary/10 text-sm font-semibold text-primary">
        {initials(employee.full_name)}
      </AvatarFallback>
    </Avatar>
  );
}

function EmployeeTags({ employee, compact = false }: { employee: Employee; compact?: boolean }) {
  const tags = [
    employee.is_senior ? "Старший" : null,
    employee.is_deputy_senior ? "Зам" : null,
    employee.category ? categoryLabel(employee.category) : null,
    isCookPosition(employee.position) && employee.default_cooking_station
      ? stationLabel(employee.default_cooking_station)
      : null,
  ].filter((tag): tag is string => Boolean(tag));

  if (tags.length === 0) {
    return <span className="text-sm text-muted-foreground">Без надбавок</span>;
  }

  return (
    <div className={cn("flex flex-wrap gap-2", compact ? "max-w-[240px]" : undefined)}>
      {tags.map((tag) => (
        <Badge className="rounded-md border-border bg-background text-foreground shadow-none" key={tag}>
          {tag}
        </Badge>
      ))}
    </div>
  );
}

function StaffMetric({
  label,
  value,
  tone = "default",
}: {
  label: string;
  value: number;
  tone?: "default" | "success" | "warning";
}) {
  return (
    <Card className="shadow-none">
      <CardContent className="flex items-center justify-between gap-3 p-4">
        <div>
          <div className="text-sm text-muted-foreground">{label}</div>
          <div className="mt-1 text-2xl font-semibold tabular-nums">{value}</div>
        </div>
        <span
          className={cn(
            "inline-flex h-10 w-10 items-center justify-center rounded-md",
            tone === "success"
              ? "bg-emerald-50 text-emerald-700"
              : tone === "warning"
                ? "bg-amber-50 text-amber-700"
                : "bg-accent text-accent-foreground",
          )}
        >
          {tone === "success" ? (
            <CheckCircle2 size={18} aria-hidden="true" />
          ) : tone === "warning" ? (
            <ShieldAlert size={18} aria-hidden="true" />
          ) : (
            <UserPlus size={18} aria-hidden="true" />
          )}
        </span>
      </CardContent>
    </Card>
  );
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-right font-medium">{value}</span>
    </div>
  );
}

const cookPositions = new Set(
  [
    "Повар",
    "Повара",
    "Сушист",
    "Сушисты",
    "Пиццист",
    "Пиццисты",
    "Пиццерист",
    "Пиццеристы",
    "Шаурмист",
    "Шаурмисты",
    "Заготовщик",
    "Заготовщики",
    "Шеф-повар",
    "Шеф повар",
    "Шеф-повара",
  ].map(normalizePosition),
);

function isCookPosition(position: string | null) {
  return cookPositions.has(normalizePosition(position));
}

function normalizePosition(position: string | null) {
  return (position ?? "")
    .replace(/\u00a0/g, " ")
    .trim()
    .replace(/Ё/g, "Е")
    .replace(/ё/g, "е")
    .toLowerCase()
    .replace(/\s*[-–—]\s*/g, "-")
    .replace(/\s+/g, " ");
}

function categoryLabel(category: EmployeeCategory | null) {
  return category ? EMPLOYEE_CATEGORY_LABELS[category] : "Не задана";
}

function stationLabel(station: CookingStation | null) {
  return station ? COOKING_STATION_LABELS[station] : "Не задан";
}

function toDraft(employee: Employee): Draft {
  return {
    position: employee.position,
    category: employee.category,
    default_cooking_station: employee.default_cooking_station,
    is_senior: employee.is_senior,
    is_deputy_senior: employee.is_deputy_senior,
  };
}

function initials(name: string) {
  const parts = name.trim().split(/\s+/).slice(0, 2);
  return parts.map((part) => part[0]?.toUpperCase() ?? "").join("") || "С";
}

function formatDateTime(value: string | null) {
  if (!value) {
    return "Не было";
  }

  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "short",
    timeStyle: "short",
    timeZone: "Europe/Moscow",
  }).format(new Date(value));
}
