import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  DatabaseZap,
  Grid2X2,
  KeyRound,
  List,
  LoaderCircle,
  Plus,
  Save,
  Search,
  ShieldAlert,
  RotateCcw,
  X,
  UserPlus,
  UserMinus,
} from "lucide-react";
import { type FormEvent, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
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
  type EmployeeCreatePayload,
  type EmployeeCategory,
  type IikoEmployeeRole,
  type EmployeePatch,
  type EmployeeRoleAssignment,
  type EmployeeStatus,
  type PayrollRoleCategoryOption,
  type PayrollRole,
  apiErrorMessage,
  changeEmployeePin,
  createEmployee,
  createEmployeeAssignment,
  deleteEmployeeAssignment,
  dismissEmployee,
  getEmployeeAssignments,
  getEmployees,
  getIikoEmployeeRoles,
  getPayrollRoleCategories,
  patchEmployeeAssignment,
  patchEmployee,
  reinstateEmployee,
  syncEmployees,
} from "@/lib/api";
import { getAuthSnapshot, subscribeAuth } from "@/lib/auth";
import {
  COOKING_STATION_LABELS,
  EMPLOYEE_CATEGORY_LABELS,
  EMPLOYEE_STATUS_LABELS,
  PAYROLL_ROLE_LABELS,
} from "@/lib/i18n/employee";
import { cn } from "@/lib/utils";

type StaffStatusFilter = EmployeeStatus | "current" | "all";

const statusOptions: StaffStatusFilter[] = [
  "current",
  "active",
  "requires_setup",
  "inactive",
  "all",
];
const deprecatedCategoryOptions = new Set<EmployeeCategory>(["freelancer"]);
const categoryOptions = (Object.keys(EMPLOYEE_CATEGORY_LABELS) as EmployeeCategory[]).filter(
  (category) => !deprecatedCategoryOptions.has(category),
);
const cookingStationOptions = Object.keys(COOKING_STATION_LABELS) as CookingStation[];
const payrollRoleOptions = Object.keys(PAYROLL_ROLE_LABELS) as PayrollRole[];
const cookPayrollRoles: PayrollRole[] = ["sushi", "pizza", "shawarma", "prep"];
const cashierPayrollRoles: PayrollRole[] = ["administrator"];

type CanonicalPosition =
  | "Кассир"
  | "Повар"
  | "Управляющий"
  | "Системный администратор"
  | "Курьер"
  | "Менеджер";

const canonicalPositions: CanonicalPosition[] = [
  "Кассир",
  "Повар",
  "Управляющий",
  "Системный администратор",
  "Курьер",
  "Менеджер",
];
const createPositions = new Set<CanonicalPosition>([
  "Кассир",
  "Менеджер",
  "Повар",
  "Управляющий",
  "Курьер",
]);
const positionPayrollRoles: Record<CanonicalPosition, PayrollRole[]> = {
  Кассир: cashierPayrollRoles,
  Повар: cookPayrollRoles,
  Управляющий: [],
  "Системный администратор": [],
  Курьер: [],
  Менеджер: [],
};
const premiumApplicability: Record<
  CanonicalPosition,
  { is_senior: boolean; is_deputy_senior: boolean }
> = {
  Кассир: { is_senior: true, is_deputy_senior: true },
  Повар: { is_senior: true, is_deputy_senior: true },
  Курьер: { is_senior: true, is_deputy_senior: false },
  Управляющий: { is_senior: false, is_deputy_senior: false },
  "Системный администратор": { is_senior: false, is_deputy_senior: false },
  Менеджер: { is_senior: false, is_deputy_senior: false },
};

type Draft = Pick<Employee, "position" | "is_senior" | "is_deputy_senior">;
type ViewMode = "grid" | "table";
type StaffGroupFilter = "all" | "cook" | "staff";

export function StaffRoute() {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<StaffStatusFilter>("current");
  const [category, setCategory] = useState<EmployeeCategory | "all">("all");
  const [group, setGroup] = useState<StaffGroupFilter>("all");
  const [cookingStation, setCookingStation] = useState<CookingStation | "all">("all");
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [viewMode, setViewMode] = useState<ViewMode>("grid");
  const [selectedEmployeeId, setSelectedEmployeeId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);

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
        status: status === "current" ? "all" : status,
        category: category === "all" ? undefined : category,
      }),
  });

  const allEmployeesQuery = useQuery({
    queryKey: ["employees", "staff-filter-options"],
    queryFn: () => getEmployees({ status: "all" }),
  });

  const iikoRolesQuery = useQuery({
    queryKey: ["employees", "iiko-roles"],
    queryFn: getIikoEmployeeRoles,
    enabled: createOpen,
  });

  const roleCategoriesQuery = useQuery({
    queryKey: ["payroll", "role-categories"],
    queryFn: getPayrollRoleCategories,
    enabled: createOpen,
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
      toast.error(apiErrorMessage(error));
    },
  });

  const createMutation = useMutation({
    mutationFn: createEmployee,
    onSuccess: (employee) => {
      toast.success("Сотрудник создан");
      if (!isTargetPosition(employee.position)) {
        toast.warning("Роль iiko не относится к целевым позициям Штата");
      }
      setCreateOpen(false);
      setSelectedEmployeeId(employee.id);
      void queryClient.invalidateQueries({ queryKey: ["employees"] });
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось создать сотрудника"));
    },
  });

  const optionEmployees = useMemo(
    () => allEmployeesQuery.data ?? employeesQuery.data ?? [],
    [allEmployeesQuery.data, employeesQuery.data],
  );

  const employees = useMemo(() => {
    const needle = debouncedSearch.trim().toLowerCase();
    return (employeesQuery.data ?? []).filter((employee) => {
      const matchesStatus = status === "current" ? employee.status !== "inactive" : true;
      const matchesSearch = needle ? employee.full_name.toLowerCase().includes(needle) : true;
      const employeeIsCook = isCookPosition(employee.position);
      const matchesGroup =
        group === "all" ? true : group === "cook" ? employeeIsCook : !employeeIsCook;
      const matchesStation =
        group === "cook" && cookingStation !== "all"
          ? activeAssignments(employee).some(
              (assignment) => assignment.payroll_role === cookingStation,
            )
          : true;
      return matchesStatus && matchesSearch && matchesGroup && matchesStation;
    });
  }, [cookingStation, debouncedSearch, employeesQuery.data, group, status]);

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
        cell: (employee) =>
          categoryLabel(primaryAssignment(employee)?.category ?? employee.category),
      },
      {
        key: "default_cooking_station",
        header: "Роль",
        cell: (employee) =>
          payrollRoleLabel(primaryAssignment(employee)?.payroll_role) ||
          stationLabel(employee.default_cooking_station),
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
          <div className="flex flex-wrap items-center gap-2">
            <Button onClick={() => setCreateOpen(true)} variant="outline">
              <UserPlus size={16} aria-hidden="true" />
              Создать сотрудника
            </Button>
            <Button onClick={() => syncMutation.mutate()} disabled={syncMutation.isPending}>
              {syncMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <DatabaseZap size={16} aria-hidden="true" />
              )}
              Загрузить из iiko
            </Button>
          </div>
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
          <Select onValueChange={(value) => setStatus(value as StaffStatusFilter)} value={status}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {statusOptions.map((option) => (
                <SelectItem value={option} key={option}>
                  {statusFilterLabel(option)}
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
              <SelectItem value="staff">Не повара</SelectItem>
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
          {apiErrorMessage(employeesQuery.error, "Не удалось загрузить сотрудников")}
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
          {selectedEmployee ? (
            <StaffEditor employee={selectedEmployee} onClose={() => setSelectedEmployeeId(null)} />
          ) : null}
        </SheetContent>
      </Sheet>

      <CreateEmployeeDialog
        isPending={createMutation.isPending}
        onCreate={(payload) => createMutation.mutate(payload)}
        onOpenChange={setCreateOpen}
        open={createOpen}
        roles={iikoRolesQuery.data ?? []}
        rolesError={iikoRolesQuery.error}
        rolesLoading={iikoRolesQuery.isLoading || iikoRolesQuery.isFetching}
        roleCategories={roleCategoriesQuery.data ?? {}}
        roleCategoriesError={roleCategoriesQuery.error}
        roleCategoriesLoading={
          roleCategoriesQuery.isLoading || roleCategoriesQuery.isFetching
        }
      />
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

type CreateEmployeeRoleRow = {
  id: string;
  payroll_role: PayrollRole | "";
  category: EmployeeCategory | "";
  is_primary: boolean;
};

let createEmployeeRoleRowCounter = 0;

function createRoleRow(isPrimary = false): CreateEmployeeRoleRow {
  createEmployeeRoleRowCounter += 1;
  return {
    id: `create-role-${createEmployeeRoleRowCounter}`,
    payroll_role: "",
    category: "",
    is_primary: isPrimary,
  };
}

function CreateEmployeeDialog({
  isPending,
  onCreate,
  onOpenChange,
  open,
  roles,
  rolesError,
  rolesLoading,
  roleCategories,
  roleCategoriesError,
  roleCategoriesLoading,
}: {
  isPending: boolean;
  onCreate: (payload: EmployeeCreatePayload) => void;
  onOpenChange: (open: boolean) => void;
  open: boolean;
  roles: IikoEmployeeRole[];
  rolesError: unknown;
  rolesLoading: boolean;
  roleCategories: Partial<Record<PayrollRole, PayrollRoleCategoryOption[]>>;
  roleCategoriesError: unknown;
  roleCategoriesLoading: boolean;
}) {
  const [fullName, setFullName] = useState("");
  const [pinCode, setPinCode] = useState("");
  const [iikoRoleId, setIikoRoleId] = useState("");
  const [roleRows, setRoleRows] = useState<CreateEmployeeRoleRow[]>(() => [createRoleRow(true)]);
  const [isSenior, setIsSenior] = useState(false);
  const [isDeputySenior, setIsDeputySenior] = useState(false);

  const filteredRoles = useMemo(
	    () =>
	      roles.filter((role) => {
	        if (role.deleted) {
	          return false;
	        }
	        const position = canonicalPosition(role.name);
	        return Boolean(position && createPositions.has(position));
	      }),
	    [roles],
	  );
  const selectedIikoRole = useMemo(
	    () => filteredRoles.find((role) => role.id === iikoRoleId) ?? null,
	    [filteredRoles, iikoRoleId],
	  );
  const selectedPosition = canonicalPosition(selectedIikoRole?.name ?? null);
  const createPayrollRoleOptions = selectedPosition ? positionPayrollRoles[selectedPosition] : [];
  const premiumOptions = selectedPosition ? premiumApplicability[selectedPosition] : null;
  const showRoleSection = createPayrollRoleOptions.length > 0;
  const selectedRoleIds = useMemo(
	    () => new Set(roleRows.map((row) => row.payroll_role).filter(Boolean)),
	    [roleRows],
	  );
  useEffect(() => {
    if (!open) {
      setFullName("");
      setPinCode("");
      setIikoRoleId("");
      setRoleRows([createRoleRow(true)]);
      setIsSenior(false);
      setIsDeputySenior(false);
    }
  }, [open]);

  useEffect(() => {
    if (!selectedPosition) {
      setRoleRows([createRoleRow(true)]);
      setIsSenior(false);
      setIsDeputySenior(false);
      return;
    }
    const allowedRoles = positionPayrollRoles[selectedPosition];
    if (allowedRoles.length === 0) {
      setRoleRows([]);
    } else {
      setRoleRows((rows) => {
        const keptRows = rows
          .filter((row) => row.payroll_role === "" || allowedRoles.includes(row.payroll_role))
          .map((row, index) => ({ ...row, is_primary: index === 0 ? true : row.is_primary }));
        const nextRows = keptRows.length > 0 ? keptRows : [createRoleRow(true)];
        const hasPrimary = nextRows.some((row) => row.is_primary);
        return hasPrimary
          ? nextRows
          : nextRows.map((row, index) => ({ ...row, is_primary: index === 0 }));
      });
    }
    const applicability = premiumApplicability[selectedPosition];
    if (!applicability.is_senior) {
      setIsSenior(false);
    }
    if (!applicability.is_deputy_senior) {
      setIsDeputySenior(false);
    }
  }, [selectedPosition]);
  const hasUnusedCreateRole = createPayrollRoleOptions.some((role) => !selectedRoleIds.has(role));
  const trimmedName = fullName.trim();
  const nameIsValid = trimmedName.split(/\s+/).filter(Boolean).length >= 2;
  const pinIsValid = /^\d{4}$/.test(pinCode);
  const primaryCount = roleRows.filter((row) => row.is_primary).length;
	  const rolesAreValid =
	    !showRoleSection ||
	    (roleRows.length > 0 &&
	      primaryCount === 1 &&
	      roleRows.every((row) => {
	        if (!row.payroll_role || !row.category) {
	          return false;
	        }
	        return categoriesForPayrollRole(row.payroll_role).some(
	          (category) => category.code === row.category,
	        );
	      }));
  const canSubmit =
    nameIsValid &&
    pinIsValid &&
    Boolean(iikoRoleId) &&
    rolesAreValid &&
    !isPending &&
    !rolesLoading &&
    !roleCategoriesLoading;

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmit) {
      return;
    }
    onCreate({
      full_name: trimmedName,
      pin_code: pinCode,
      iiko_role_id: iikoRoleId,
	      roles: showRoleSection
	        ? roleRows.map((row) => ({
	            payroll_role: row.payroll_role as PayrollRole,
	            category: row.category as EmployeeCategory,
	            is_primary: row.is_primary,
	          }))
	        : [],
      is_senior: isSenior,
      is_deputy_senior: isDeputySenior,
    });
  }

  function categoriesForPayrollRole(payrollRole: PayrollRole | "") {
    return payrollRole ? (roleCategories[payrollRole] ?? []) : [];
  }

  function roleOptionsForRow(row: CreateEmployeeRoleRow) {
    return createPayrollRoleOptions.filter(
      (role) => role === row.payroll_role || !selectedRoleIds.has(role),
    );
  }

  function updateRoleRow(rowId: string, patch: Partial<CreateEmployeeRoleRow>) {
    setRoleRows((rows) => rows.map((row) => (row.id === rowId ? { ...row, ...patch } : row)));
  }

  function selectRole(row: CreateEmployeeRoleRow, payrollRole: PayrollRole) {
    const categories = categoriesForPayrollRole(payrollRole);
    updateRoleRow(row.id, {
      payroll_role: payrollRole,
      category: categories.length === 1 ? categories[0].code : "",
    });
  }

  function setPrimaryRole(rowId: string) {
    setRoleRows((rows) => rows.map((row) => ({ ...row, is_primary: row.id === rowId })));
  }

  function addRoleRow() {
    const hasUnusedRole = createPayrollRoleOptions.some((role) => !selectedRoleIds.has(role));
    if (!hasUnusedRole) {
      toast.error("Все доступные роли уже выбраны");
      return;
    }
    setRoleRows((rows) => [...rows, createRoleRow(false)]);
  }

  function removeRoleRow(rowId: string) {
    setRoleRows((rows) => rows.filter((row) => row.id !== rowId));
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-3xl">
        <form className="grid gap-4" onSubmit={submit}>
          <DialogHeader>
            <DialogTitle>Создать сотрудника</DialogTitle>
            <DialogDescription>Карточка будет заведена в iiko и добавлена в Штат.</DialogDescription>
          </DialogHeader>

          {rolesError ? (
            <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {apiErrorMessage(rolesError, "Не удалось загрузить роли iiko")}
            </div>
          ) : null}

          {roleCategoriesError ? (
            <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {apiErrorMessage(roleCategoriesError, "Не удалось загрузить категории ролей")}
            </div>
          ) : null}

          <Label className="grid gap-2">
            <span>ФИО</span>
            <Input
              autoComplete="off"
              onChange={(event) => setFullName(event.target.value)}
              placeholder="Иванов Иван Иванович"
              value={fullName}
            />
            {trimmedName && !nameIsValid ? (
              <span className="text-xs text-destructive">Укажите минимум фамилию и имя</span>
            ) : null}
          </Label>

          <Label className="grid gap-2">
            <span>ПИН-код для открытия смены</span>
            <Input
              autoComplete="off"
              inputMode="numeric"
              maxLength={4}
              onChange={(event) => setPinCode(event.target.value.replace(/\D/g, "").slice(0, 4))}
              placeholder="0000"
              value={pinCode}
            />
            {pinCode && !pinIsValid ? (
              <span className="text-xs text-destructive">ПИН-код должен состоять из 4 цифр</span>
            ) : null}
          </Label>

          <Label className="grid gap-2">
            <span>Должность iiko</span>
            <Select
              disabled={rolesLoading || isPending || filteredRoles.length === 0}
              onValueChange={setIikoRoleId}
              value={iikoRoleId}
            >
              <SelectTrigger>
                <SelectValue
                  placeholder={rolesLoading ? "Загрузка должностей..." : "Выберите должность"}
                />
              </SelectTrigger>
              <SelectContent>
                {filteredRoles.map((role) => (
                  <SelectItem value={role.id} key={role.id}>
                    {role.name}
                    {role.code ? ` · ${role.code}` : ""}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Label>

	          {showRoleSection ? (
	            <div className="grid gap-3 rounded-lg border bg-card p-4">
	              <div className="flex items-center justify-between gap-3">
	                <div className="text-sm font-medium">Роли и категории</div>
	                <Button
	                  disabled={isPending || roleCategoriesLoading || !hasUnusedCreateRole}
	                  onClick={addRoleRow}
	                  size="sm"
	                  type="button"
	                  variant="outline"
	                >
	                  <Plus size={15} aria-hidden="true" />
	                  Добавить роль
	                </Button>
	              </div>

	              <div className="grid gap-2">
	                {roleRows.map((row) => {
	                  const rowCategories = categoriesForPayrollRole(row.payroll_role);
	                  return (
	                    <div
	                      className="grid gap-3 rounded-md border bg-background p-3 lg:grid-cols-[120px_1fr_1fr_auto] lg:items-end"
	                      key={row.id}
	                    >
	                      <label className="flex h-10 items-center gap-2 text-sm">
	                        <input
	                          checked={row.is_primary}
	                          disabled={isPending}
	                          name="create-primary-role"
	                          onChange={() => setPrimaryRole(row.id)}
	                          type="radio"
	                        />
	                        <span>Основная</span>
	                      </label>

	                      <Label className="grid gap-1">
	                        <span className="text-xs font-medium uppercase text-muted-foreground">
	                          Роль
	                        </span>
	                        <Select
	                          disabled={
	                            roleCategoriesLoading ||
	                            isPending ||
	                            roleOptionsForRow(row).length === 0
	                          }
	                          onValueChange={(value) => selectRole(row, value as PayrollRole)}
	                          value={row.payroll_role}
	                        >
	                          <SelectTrigger>
	                            <SelectValue placeholder="Выберите роль" />
	                          </SelectTrigger>
	                          <SelectContent>
	                            {roleOptionsForRow(row).map((role) => (
	                              <SelectItem value={role} key={role}>
	                                {PAYROLL_ROLE_LABELS[role]}
	                              </SelectItem>
	                            ))}
	                          </SelectContent>
	                        </Select>
	                      </Label>

	                      <Label className="grid gap-1">
	                        <span className="text-xs font-medium uppercase text-muted-foreground">
	                          Категория
	                        </span>
	                        <Select
	                          disabled={
	                            roleCategoriesLoading ||
	                            isPending ||
	                            !row.payroll_role ||
	                            rowCategories.length === 0
	                          }
	                          onValueChange={(value) =>
	                            updateRoleRow(row.id, { category: value as EmployeeCategory })
	                          }
	                          value={row.category}
	                        >
	                          <SelectTrigger>
	                            <SelectValue
	                              placeholder={
	                                roleCategoriesLoading
	                                  ? "Загрузка категорий..."
	                                  : "Выберите категорию"
	                              }
	                            />
	                          </SelectTrigger>
	                          <SelectContent>
	                            {rowCategories.map((category) => (
	                              <SelectItem value={category.code} key={category.code}>
	                                {category.name}
	                              </SelectItem>
	                            ))}
	                          </SelectContent>
	                        </Select>
	                      </Label>

	                      {row.is_primary ? (
	                        <div className="hidden h-10 lg:block" />
	                      ) : (
	                        <Button
	                          disabled={isPending}
	                          onClick={() => removeRoleRow(row.id)}
	                          size="icon"
	                          title="Удалить роль"
	                          type="button"
	                          variant="ghost"
	                        >
	                          <X size={16} aria-hidden="true" />
	                        </Button>
	                      )}
	                    </div>
	                  );
	                })}
	              </div>
	            </div>
	          ) : null}

	          {premiumOptions?.is_senior || premiumOptions?.is_deputy_senior ? (
	            <div className="grid gap-3 rounded-lg border bg-card p-4">
	              <div className="text-sm font-medium">Надбавки</div>
	              {premiumOptions.is_senior ? (
	                <label className="flex items-center justify-between gap-3 rounded-md border bg-background px-3 py-2 text-sm">
	                  <span>Старший</span>
	                  <input
	                    checked={isSenior}
	                    onChange={(event) => setIsSenior(event.target.checked)}
	                    type="checkbox"
	                  />
	                </label>
	              ) : null}
	              {premiumOptions.is_deputy_senior ? (
	                <label className="flex items-center justify-between gap-3 rounded-md border bg-background px-3 py-2 text-sm">
	                  <span>Зам старшего</span>
	                  <input
	                    checked={isDeputySenior}
	                    onChange={(event) => setIsDeputySenior(event.target.checked)}
	                    type="checkbox"
	                  />
	                </label>
	              ) : null}
	            </div>
	          ) : null}

          <DialogFooter>
            <Button
              disabled={isPending}
              onClick={() => onOpenChange(false)}
              type="button"
              variant="outline"
            >
              Отмена
            </Button>
            <Button disabled={!canSubmit} type="submit">
              {isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <UserPlus size={16} aria-hidden="true" />
              )}
              Создать
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function StaffEditor({ employee, onClose }: { employee: Employee; onClose: () => void }) {
  const queryClient = useQueryClient();
  const auth = useAuthSnapshot();
  const [draft, setDraft] = useState<Draft>(() => toDraft(employee));
  const [dismissOpen, setDismissOpen] = useState(false);
  const [dismissFireDate, setDismissFireDate] = useState(() => todayDateInputValue());
  const [dismissReason, setDismissReason] = useState("");
  const [pinOpen, setPinOpen] = useState(false);
  const [pinCode, setPinCode] = useState("");

  useEffect(() => {
    setDraft(toDraft(employee));
    setDismissFireDate(employee.fire_date ?? todayDateInputValue());
    setDismissReason("");
    setPinOpen(false);
    setPinCode("");
  }, [employee]);

  const mutation = useMutation({
    mutationFn: (patch: EmployeePatch) => patchEmployee(employee.id, patch),
    onSuccess: (updatedEmployee) => {
      setDraft(toDraft(updatedEmployee));
      toast.success("Карточка сотрудника обновлена");
      void queryClient.invalidateQueries({ queryKey: ["employees"] });
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось обновить карточку сотрудника"));
    },
  });
	  const assignmentsQuery = useQuery({
	    queryKey: ["employees", employee.id, "assignments"],
	    queryFn: () => getEmployeeAssignments(employee.id),
	    initialData: activeAssignments(employee),
	  });
  const roleCategoriesQuery = useQuery({
    queryKey: ["payroll", "role-categories"],
    queryFn: getPayrollRoleCategories,
  });
  const assignmentMutation = useMutation({
    mutationFn: ({
      assignmentId,
      patch,
    }: {
      assignmentId: string;
      patch: Partial<Pick<EmployeeRoleAssignment, "payroll_role" | "category" | "is_primary">>;
    }) => patchEmployeeAssignment(employee.id, assignmentId, patch),
    onSuccess: () => {
      toast.success("Роль сотрудника обновлена");
      void queryClient.invalidateQueries({ queryKey: ["employees"] });
      void queryClient.invalidateQueries({ queryKey: ["employees", employee.id, "assignments"] });
    },
    onError: (error, variables) => {
      toast.error(assignmentErrorMessage(error, variables.patch.category));
    },
  });
  const createAssignmentMutation = useMutation({
    mutationFn: (payload: { payroll_role: PayrollRole; category: EmployeeCategory }) =>
      createEmployeeAssignment(employee.id, payload),
    onSuccess: () => {
      toast.success("Роль добавлена");
      void queryClient.invalidateQueries({ queryKey: ["employees"] });
      void queryClient.invalidateQueries({ queryKey: ["employees", employee.id, "assignments"] });
    },
    onError: (error, variables) => {
      toast.error(assignmentErrorMessage(error, variables.category));
    },
  });
	  const deleteAssignmentMutation = useMutation({
    mutationFn: (assignmentId: string) => deleteEmployeeAssignment(employee.id, assignmentId),
    onSuccess: () => {
      toast.success("Роль удалена");
      void queryClient.invalidateQueries({ queryKey: ["employees"] });
      void queryClient.invalidateQueries({ queryKey: ["employees", employee.id, "assignments"] });
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось удалить роль сотрудника"));
    },
	  });
  const pinMutation = useMutation({
    mutationFn: () => changeEmployeePin(employee.id, { pin_code: pinCode }),
    onSuccess: (updatedEmployee) => {
      setDraft(toDraft(updatedEmployee));
      setPinOpen(false);
      setPinCode("");
      toast.success("ПИН изменён");
      void queryClient.invalidateQueries({ queryKey: ["employees"] });
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось сменить ПИН"));
    },
  });
  const dismissMutation = useMutation({
    mutationFn: () =>
      dismissEmployee(employee.id, {
        fire_date: dismissFireDate,
        reason: dismissReason.trim() || undefined,
      }),
    onSuccess: () => {
      toast.success("Сотрудник уволен");
      setDismissOpen(false);
      onClose();
      void queryClient.invalidateQueries({ queryKey: ["employees"] });
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось уволить сотрудника"));
    },
  });
  const reinstateMutation = useMutation({
    mutationFn: () => reinstateEmployee(employee.id),
    onSuccess: () => {
      toast.success("Сотрудник восстановлен");
      void queryClient.invalidateQueries({ queryKey: ["employees"] });
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось восстановить сотрудника"));
    },
  });

  const dirty =
    draft.position !== employee.position ||
    draft.is_senior !== employee.is_senior ||
    draft.is_deputy_senior !== employee.is_deputy_senior;
	  const assignments = assignmentsQuery.data ?? [];
	  const activeRoleIds = new Set(assignments.map((assignment) => assignment.payroll_role));
	  const roleOptions = payrollRolesForPosition(draft.position);
  const editorPosition = canonicalPosition(draft.position);
  const editorPremiumOptions = editorPosition ? premiumApplicability[editorPosition] : null;
  const showEditorRoles = roleOptions.length > 0;
  const editorRoleCategories = roleCategoriesQuery.data ?? {};
  const pinIsValid = /^\d{4}$/.test(pinCode);
	  const isAssignmentPending =
	    assignmentsQuery.isFetching ||
    roleCategoriesQuery.isFetching ||
	    assignmentMutation.isPending ||
	    createAssignmentMutation.isPending ||
	    deleteAssignmentMutation.isPending;
  const canDismiss =
    !auth.user || hasAnyRole(auth.user.roles, ["finance_manager", "owner", "admin"]);
  const canReinstate = hasAnyRole(auth.user?.roles, ["owner"]);
	  const canDismissStatus = employee.status === "active" || employee.status === "requires_setup";

  function setDraftPosition(position: CanonicalPosition) {
    const applicability = premiumApplicability[position];
    setDraft((current) => ({
      ...current,
      position,
      is_senior: applicability.is_senior ? current.is_senior : false,
      is_deputy_senior: applicability.is_deputy_senior ? current.is_deputy_senior : false,
    }));
  }

  function categoriesForEditorRole(payrollRole: PayrollRole) {
    return editorRoleCategories[payrollRole] ?? [];
  }

	  function addRole() {
	    const payrollRole = roleOptions.find((role) => !activeRoleIds.has(role));
    if (!payrollRole) {
      toast.error("Все подходящие роли уже добавлены");
      return;
	    }
    const categories = categoriesForEditorRole(payrollRole);
    const category = categories[0]?.code;
    if (!category) {
      toast.error("Для роли нет доступных категорий");
      return;
    }
	    createAssignmentMutation.mutate({
	      payroll_role: payrollRole,
	      category,
	    });
	  }

  return (
    <div className="space-y-5">
      <SheetHeader>
        <SheetTitle className="pr-8">Карточка сотрудника</SheetTitle>
        <SheetDescription>
          Поля реестра, которые используются графиком и зарплатой.
        </SheetDescription>
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
	          <Select
	            onValueChange={(value) => setDraftPosition(value as CanonicalPosition)}
	            value={editorPosition ?? undefined}
	          >
	            <SelectTrigger>
	              <SelectValue placeholder="Выберите должность" />
	            </SelectTrigger>
	            <SelectContent>
	              {canonicalPositions.map((position) => (
	                <SelectItem value={position} key={position}>
	                  {position}
	                </SelectItem>
	              ))}
	            </SelectContent>
	          </Select>
	        </Label>

	        {showEditorRoles ? (
	          <div className="grid gap-3 rounded-lg border bg-card p-4">
	            <div className="flex items-center justify-between gap-3">
	              <div className="text-sm font-medium">Роли и категории</div>
	              <Button
	                disabled={isAssignmentPending || roleOptions.every((role) => activeRoleIds.has(role))}
	                onClick={addRole}
	                size="sm"
	                type="button"
	                variant="outline"
	              >
	                <Plus size={15} aria-hidden="true" />
	                Добавить роль
	              </Button>
	            </div>

	            {assignments.length === 0 ? (
	              <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
	                Добавьте хотя бы одну роль
	              </div>
	            ) : (
	              <div className="grid gap-2">
	                {assignments.map((assignment) => {
	                  const roleOptionsForAssignment = roleOptions.filter(
	                    (role) => role === assignment.payroll_role || !activeRoleIds.has(role),
	                  );
	                  const rowCategories = categoriesForEditorRole(assignment.payroll_role);
	                  return (
	                    <div
	                      className="grid gap-2 rounded-md border bg-background p-3 sm:grid-cols-[120px_1fr_1fr_auto] sm:items-center"
	                      key={assignment.id}
	                    >
	                      <label className="flex h-10 items-center gap-2 text-sm">
	                        <input
	                          checked={assignment.is_primary}
	                          disabled={assignment.is_primary || isAssignmentPending}
	                          name="edit-primary-role"
	                          onChange={() =>
	                            assignmentMutation.mutate({
	                              assignmentId: assignment.id,
	                              patch: { is_primary: true },
	                            })
	                          }
	                          type="radio"
	                        />
	                        <span>Основная</span>
	                      </label>

	                      <Select
	                        disabled={isAssignmentPending}
	                        onValueChange={(value) => {
	                          const payrollRole = value as PayrollRole;
	                          const nextCategories = categoriesForEditorRole(payrollRole);
	                          const category = nextCategories.some(
	                            (option) => option.code === assignment.category,
	                          )
	                            ? assignment.category
	                            : nextCategories[0]?.code;
	                          if (!category) {
	                            toast.error("Для роли нет доступных категорий");
	                            return;
	                          }
	                          assignmentMutation.mutate({
	                            assignmentId: assignment.id,
	                            patch: { payroll_role: payrollRole, category },
	                          });
	                        }}
	                        value={assignment.payroll_role}
	                      >
	                        <SelectTrigger>
	                          <SelectValue />
	                        </SelectTrigger>
	                        <SelectContent>
	                          {roleOptionsForAssignment.map((value) => (
	                            <SelectItem value={value} key={value}>
	                              {PAYROLL_ROLE_LABELS[value]}
	                            </SelectItem>
	                          ))}
	                        </SelectContent>
	                      </Select>

	                      <Select
	                        disabled={isAssignmentPending || rowCategories.length === 0}
	                        onValueChange={(value) =>
	                          assignmentMutation.mutate({
	                            assignmentId: assignment.id,
	                            patch: { category: value as EmployeeCategory },
	                          })
	                        }
	                        value={assignment.category}
	                      >
	                        <SelectTrigger>
	                          <SelectValue />
	                        </SelectTrigger>
	                        <SelectContent>
	                          {rowCategories.map((value) => (
	                            <SelectItem value={value.code} key={value.code}>
	                              {value.name}
	                            </SelectItem>
	                          ))}
	                        </SelectContent>
	                      </Select>

	                      <Button
	                        disabled={assignment.is_primary || isAssignmentPending}
	                        onClick={() => deleteAssignmentMutation.mutate(assignment.id)}
	                        size="icon"
	                        title="Удалить роль"
	                        type="button"
	                        variant="ghost"
	                      >
	                        <X size={16} aria-hidden="true" />
	                      </Button>
	                    </div>
	                  );
	                })}
	              </div>
	            )}
	          </div>
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

	        {editorPremiumOptions?.is_senior || editorPremiumOptions?.is_deputy_senior ? (
	          <div className="grid gap-3 rounded-lg border bg-card p-4">
	            <div className="text-sm font-medium">Надбавки</div>
	            {editorPremiumOptions.is_senior ? (
	              <label className="flex items-center justify-between gap-3 rounded-md border bg-background px-3 py-2 text-sm">
	                <span>Старший</span>
	                <input
	                  checked={draft.is_senior}
	                  onChange={(event) => setDraft({ ...draft, is_senior: event.target.checked })}
	                  type="checkbox"
	                />
	              </label>
	            ) : null}
	            {editorPremiumOptions.is_deputy_senior ? (
	              <label className="flex items-center justify-between gap-3 rounded-md border bg-background px-3 py-2 text-sm">
	                <span>Зам старшего</span>
	                <input
	                  checked={draft.is_deputy_senior}
	                  onChange={(event) =>
	                    setDraft({ ...draft, is_deputy_senior: event.target.checked })
	                  }
	                  type="checkbox"
	                />
	              </label>
	            ) : null}
	          </div>
	        ) : null}
      </div>

      <div className="grid gap-2 rounded-lg border bg-muted/30 p-4 text-sm">
        {employee.status === "inactive" ? (
          <InfoRow label="Дата увольнения" value={formatDate(employee.fire_date)} />
        ) : null}
	        {employee.status === "inactive" && employee.fire_reason ? (
	          <InfoRow label="Причина" value={employee.fire_reason} />
	        ) : null}
	        <InfoRow label="ПИН изменён" value={formatDateTime(employee.pin_set_at)} />
	        <InfoRow label="Синхронизация" value={formatDateTime(employee.iiko_sync_at)} />
        <InfoRow label="Создан" value={formatDateTime(employee.created_at)} />
        <InfoRow label="Обновлён" value={formatDateTime(employee.updated_at)} />
      </div>

      <div className="grid gap-2">
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

	        <Button
	          className="w-full"
	          disabled={pinMutation.isPending}
	          onClick={() => {
	            setPinCode("");
	            setPinOpen(true);
	          }}
	          type="button"
	          variant="outline"
	        >
	          <KeyRound size={16} aria-hidden="true" />
	          Сменить ПИН
	        </Button>

	        {canDismiss && canDismissStatus ? (
          <Button
            className="w-full"
            disabled={dismissMutation.isPending}
            onClick={() => {
              setDismissFireDate(todayDateInputValue());
              setDismissReason("");
              setDismissOpen(true);
            }}
            type="button"
            variant="destructive"
          >
            <UserMinus size={16} aria-hidden="true" />
            Уволить
          </Button>
        ) : null}

        {employee.status === "inactive" && canReinstate ? (
          <Button
            className="w-full"
            disabled={reinstateMutation.isPending}
            onClick={() => reinstateMutation.mutate()}
            type="button"
            variant="outline"
          >
            {reinstateMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : (
              <RotateCcw size={16} aria-hidden="true" />
            )}
            Восстановить
          </Button>
        ) : null}
      </div>

	      <Dialog open={dismissOpen} onOpenChange={setDismissOpen}>
	        <DialogContent>
          <DialogHeader>
            <DialogTitle>Уволить {employee.full_name}?</DialogTitle>
            <DialogDescription>
              Укажите дату увольнения и причину, если её нужно сохранить в карточке.
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-4">
            <Label className="grid gap-2">
              <span>Дата увольнения</span>
              <Input
                onChange={(event) => setDismissFireDate(event.target.value)}
                type="date"
                value={dismissFireDate}
              />
            </Label>

            <Label className="grid gap-2">
              <span>Причина</span>
              <textarea
                className="min-h-24 rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                onChange={(event) => setDismissReason(event.target.value)}
                placeholder="Опционально"
                value={dismissReason}
              />
            </Label>
          </div>

          <DialogFooter>
            <Button
              disabled={dismissMutation.isPending}
              onClick={() => setDismissOpen(false)}
              type="button"
              variant="outline"
            >
              Отмена
            </Button>
            <Button
              disabled={!dismissFireDate || dismissMutation.isPending}
              onClick={() => dismissMutation.mutate()}
              type="button"
              variant="destructive"
            >
              {dismissMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <UserMinus size={16} aria-hidden="true" />
              )}
              Уволить
            </Button>
          </DialogFooter>
	        </DialogContent>
	      </Dialog>

	      <Dialog open={pinOpen} onOpenChange={setPinOpen}>
	        <DialogContent>
	          <DialogHeader>
	            <DialogTitle>Сменить ПИН</DialogTitle>
	            <DialogDescription>
	              Новый ПИН будет использоваться для открытия смены.
	            </DialogDescription>
	          </DialogHeader>

	          <Label className="grid gap-2">
	            <span>ПИН-код</span>
	            <Input
	              autoComplete="off"
	              inputMode="numeric"
	              maxLength={4}
	              onChange={(event) => setPinCode(event.target.value.replace(/\D/g, "").slice(0, 4))}
	              placeholder="0000"
	              value={pinCode}
	            />
	            {pinCode && !pinIsValid ? (
	              <span className="text-xs text-destructive">ПИН-код должен состоять из 4 цифр</span>
	            ) : null}
	          </Label>

	          <DialogFooter>
	            <Button
	              disabled={pinMutation.isPending}
	              onClick={() => setPinOpen(false)}
	              type="button"
	              variant="outline"
	            >
	              Отмена
	            </Button>
	            <Button
	              disabled={!pinIsValid || pinMutation.isPending}
	              onClick={() => pinMutation.mutate()}
	              type="button"
	            >
	              {pinMutation.isPending ? (
	                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
	              ) : (
	                <KeyRound size={16} aria-hidden="true" />
	              )}
	              Сохранить
	            </Button>
	          </DialogFooter>
	        </DialogContent>
	      </Dialog>
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
  const primary = primaryAssignment(employee);
  const additionalRoles = Math.max(activeAssignments(employee).length - (primary ? 1 : 0), 0);
  const tags = [
    employee.is_senior ? "Старший" : null,
    employee.is_deputy_senior ? "Зам" : null,
    primary
      ? `${payrollRoleLabel(primary.payroll_role)} · ${categoryLabel(primary.category)}`
      : employee.category
        ? categoryLabel(employee.category)
        : null,
    additionalRoles > 0 ? `+${additionalRoles} ролей` : null,
  ].filter((tag): tag is string => Boolean(tag));

  if (tags.length === 0) {
    return <span className="text-sm text-muted-foreground">Без надбавок</span>;
  }

  return (
    <div className={cn("flex flex-wrap gap-2", compact ? "max-w-[240px]" : undefined)}>
      {tags.map((tag) => (
        <Badge
          className="rounded-md border-border bg-background text-foreground shadow-none"
          key={tag}
        >
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

const canonicalPositionByName = new Map(
  canonicalPositions.map((position) => [normalizePosition(position), position]),
);

function isCookPosition(position: string | null) {
  return canonicalPosition(position) === "Повар";
}

function isStaffPosition(position: string | null) {
  const canonical = canonicalPosition(position);
  return Boolean(canonical && canonical !== "Повар");
}

function isTargetPosition(position: string | null) {
  return canonicalPosition(position) !== null;
}

function canonicalPosition(position: string | null): CanonicalPosition | null {
  return canonicalPositionByName.get(normalizePosition(position)) ?? null;
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

function payrollRoleLabel(role: PayrollRole | null | undefined) {
  return role ? PAYROLL_ROLE_LABELS[role] : null;
}

function assignmentErrorMessage(error: unknown, category?: EmployeeCategory) {
  if (category === "category_4") {
    return "Категория 4-я доступна только для Шаурмиста";
  }
  return apiErrorMessage(error, "Не удалось обновить роль сотрудника");
}

function statusFilterLabel(status: StaffStatusFilter) {
  if (status === "current") {
    return "Работают";
  }
  if (status === "all") {
    return "Все";
  }
  return EMPLOYEE_STATUS_LABELS[status];
}

function hasAnyRole(roles: string[] | undefined, allowedRoles: readonly string[]) {
  return Boolean(roles?.some((role) => allowedRoles.includes(role)));
}

function activeAssignments(employee: Employee) {
  const today = new Date().toISOString().slice(0, 10);
  return (employee.assignments ?? []).filter(
    (assignment) =>
      assignment.effective_from <= today &&
      (!assignment.effective_to || assignment.effective_to > today),
  );
}

function primaryAssignment(employee: Employee) {
  const assignments = activeAssignments(employee);
  return assignments.find((assignment) => assignment.is_primary) ?? assignments[0] ?? null;
}

function payrollRolesForPosition(position: string | null): PayrollRole[] {
  const canonical = canonicalPosition(position);
  return canonical ? positionPayrollRoles[canonical] : payrollRoleOptions;
}

function toDraft(employee: Employee): Draft {
  return {
    position: employee.position,
    is_senior: employee.is_senior,
    is_deputy_senior: employee.is_deputy_senior,
  };
}

function initials(name: string) {
  const parts = name.trim().split(/\s+/).slice(0, 2);
  return parts.map((part) => part[0]?.toUpperCase() ?? "").join("") || "С";
}

function todayDateInputValue() {
  const parts = new Intl.DateTimeFormat("en-CA", {
    day: "2-digit",
    month: "2-digit",
    timeZone: "Europe/Moscow",
    year: "numeric",
  }).formatToParts(new Date());
  const value = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${value.year}-${value.month}-${value.day}`;
}

function formatDate(value: string | null) {
  if (!value) {
    return "Не указана";
  }

  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "short",
    timeZone: "Europe/Moscow",
  }).format(new Date(`${value}T00:00:00+03:00`));
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

function useAuthSnapshot() {
  const [auth, setAuth] = useState(getAuthSnapshot);

  useEffect(() => {
    const unsubscribe = subscribeAuth(setAuth);
    return () => {
      unsubscribe();
    };
  }, []);

  return auth;
}
