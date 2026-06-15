import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import {
  Banknote,
  BellOff,
  BellPlus,
  CalendarPlus,
  CheckCircle2,
  CircleAlert,
  CircleMinus,
  DatabaseZap,
  Grid2X2,
  History,
  Info,
  KeyRound,
  List,
  LoaderCircle,
  MoreHorizontal,
  Pencil,
  Plus,
  Save,
  Search,
  ShieldAlert,
  RotateCcw,
  Trash2,
  X,
  UserPlus,
  UserMinus,
} from "lucide-react";
import {
  type Dispatch,
  type FormEvent,
  type SetStateAction,
  useEffect,
  useMemo,
  useState,
} from "react";
import { toast } from "sonner";

import { EmployeeDepositSection } from "@/components/deposits/EmployeeDepositSection";
import {
  extractDepositSettings,
  formatMoney as formatDepositMoney,
  isDepositTargetPosition,
} from "@/components/deposits/deposit-utils";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
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
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import { EmptyState } from "@/components/ui-app/EmptyState";
import { PageHeader } from "@/components/ui-app/PageHeader";
import { StatusBadge } from "@/components/ui-app/StatusBadge";
import {
  type CookingStation,
  type AccumulationFundAccount,
  type AccumulationFundEmployeeDetail,
  type AccumulationFundTransaction,
  type Employee,
  type EmployeeChangeEvent,
  type EmployeeChangeSource,
  type EmployeeChangeStatus,
  type EmployeeCreatePayload,
  type EmployeeCategory,
  type EmployeeDismissPayload,
  type EmployeeDismissalReason,
  type DepositDismissAction,
  type IikoEmployeeRole,
  type EmployeePatch,
  type EmployeePositionAssignment,
  type EmployeePositionAssignmentDeletePayload,
  type EmployeePositionAssignmentPatch,
  type EmployeePositionChangePayload,
  type EmployeeRoleAssignment,
  type EmployeeStatus,
  type PayrollImpactWarning,
  type PayrollRole,
  type PayrollAdjustment,
  apiErrorDetail,
  apiErrorMessage,
  apiErrorStatus,
  cancelEmployeeNotice,
  changeEmployeePosition,
  createEmployeeAssignment,
  createEmployee,
  dismissEmployee,
  getEmployeeAccumulationFund,
  getDeposits,
  getEmployeeChanges,
  getEmployeeDismissalReasons,
  getEmployeePositionHistory,
  getEmployees,
  getIikoEmployeeRoles,
  getPayrollAdjustments,
  getSettings,
  getSubstitutePairs,
  patchEmployee,
  patchEmployeeAssignment,
  patchEmployeePositionAssignment,
  recordEmployeeNotice,
  reinstateEmployee,
  setEmployeeHireDate,
  syncEmployees,
  deleteEmployeeAssignment,
  deleteEmployeePositionAssignment,
  type SubstitutePair,
} from "@/lib/api";
import {
  COOKING_STATION_LABELS,
  EMPLOYEE_CHANGE_FIELD_LABELS,
  EMPLOYEE_CHANGE_SOURCE_LABELS,
  EMPLOYEE_CHANGE_STATUS_LABELS,
  EMPLOYEE_CHANGE_TYPE_LABELS,
  EMPLOYEE_CATEGORY_LABELS,
  EMPLOYEE_PIN_BADGE_LABELS,
  EMPLOYEE_PIN_BUTTON_LABELS,
  EMPLOYEE_STATUS_LABELS,
  type EmployeePinState,
  PAYROLL_ROLE_LABELS,
} from "@/lib/i18n/employee";
import { PERMISSION_GROUPS, usePermissions } from "@/lib/permissions";
import { roleColorClasses } from "@/lib/role-colors";
import { cn } from "@/lib/utils";

type StaffStatusFilter = EmployeeStatus | "current" | "all";
type StaffPositionFilter =
  | "all"
  | "cashiers"
  | "cooks"
  | "administration"
  | "couriers"
  | "auxiliary";
type StaffSecondaryFilter = "all" | PayrollRole | EmployeeCategory;

type StaffSecondaryFilterOption = {
  value: StaffSecondaryFilter;
  label: string;
};

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
const cookPayrollRoles: PayrollRole[] = ["sushi", "pizza", "shawarma", "prep"];
const cashierPayrollRoles: PayrollRole[] = ["administrator"];
const cashierSecondaryFilterOptions: StaffSecondaryFilterOption[] = [
  { value: "all", label: "Все категории" },
  { value: "category_2", label: "2" },
  { value: "category_3", label: "3" },
  { value: "category_4", label: "4" },
  { value: "intern", label: "Стажёр" },
];
const cookSecondaryFilterOptions: StaffSecondaryFilterOption[] = [
  { value: "all", label: "Все роли" },
  ...cookPayrollRoles.map((role) => ({
    value: role,
    label: PAYROLL_ROLE_LABELS[role],
  })),
];
const payrollRoleCategories: Record<PayrollRole, EmployeeCategory[]> = {
  administrator: ["category_2", "category_3", "category_4", "intern"],
  sushi: ["category_1", "category_2", "category_3", "intern"],
  pizza: ["category_1", "category_2", "category_3", "intern"],
  shawarma: ["category_3", "category_4", "intern"],
  prep: ["category_3", "intern"],
};

type CanonicalPosition =
  | "Кассир"
  | "Повар"
  | "Управляющий"
  | "Системный администратор"
  | "Курьер"
  | "Старший курьер"
  | "Менеджер"
  | "Уборщица"
  | "Посудомойка";

const canonicalPositions: CanonicalPosition[] = [
  "Кассир",
  "Повар",
  "Управляющий",
  "Системный администратор",
  "Курьер",
  "Старший курьер",
  "Менеджер",
  "Уборщица",
  "Посудомойка",
];
const positionPayrollRoles: Record<CanonicalPosition, PayrollRole[]> = {
  Кассир: cashierPayrollRoles,
  Повар: cookPayrollRoles,
  Управляющий: [],
  "Системный администратор": [],
  Курьер: [],
  "Старший курьер": [],
  Менеджер: [],
  Уборщица: [],
  Посудомойка: [],
};
const premiumApplicability: Record<
  CanonicalPosition,
  { is_senior: boolean; is_deputy_senior: boolean }
> = {
  Кассир: { is_senior: true, is_deputy_senior: true },
  Повар: { is_senior: true, is_deputy_senior: true },
  Курьер: { is_senior: false, is_deputy_senior: false },
  "Старший курьер": { is_senior: false, is_deputy_senior: false },
  Управляющий: { is_senior: false, is_deputy_senior: false },
  "Системный администратор": { is_senior: false, is_deputy_senior: false },
  Менеджер: { is_senior: false, is_deputy_senior: false },
  Уборщица: { is_senior: false, is_deputy_senior: false },
  Посудомойка: { is_senior: false, is_deputy_senior: false },
};
const auxiliaryPositions = new Set<CanonicalPosition>(["Уборщица", "Посудомойка"]);
const positionFilterOptions: Array<{
  value: StaffPositionFilter;
  label: string;
  positions: CanonicalPosition[] | null;
}> = [
  { value: "all", label: "Все", positions: null },
  { value: "cashiers", label: "Кассиры", positions: ["Кассир"] },
  { value: "cooks", label: "Повара", positions: ["Повар"] },
  {
    value: "administration",
    label: "Администрация",
    positions: ["Управляющий", "Менеджер", "Системный администратор"],
  },
  { value: "couriers", label: "Курьеры", positions: ["Курьер", "Старший курьер"] },
  {
    value: "auxiliary",
    label: "Вспомогательный персонал",
    positions: ["Уборщица", "Посудомойка"],
  },
];

type Draft = Pick<Employee, "full_name" | "position" | "is_senior" | "is_deputy_senior">;
type AssignmentDraft = Pick<EmployeeRoleAssignment, "payroll_role" | "category" | "is_primary"> & {
  id?: string;
  draft_id: string;
};
type StaffEditorDraft = Draft & {
  assignments: AssignmentDraft[];
  pin_code: string;
  pin_confirmation: string;
};
type PremiumTransferConflict = {
  patch: EmployeePatch;
  message: string;
  existingFullName: string;
};
type CategoryEffectiveChange = {
  assignmentId: string;
  payrollRole: PayrollRole;
  currentCategory: EmployeeCategory;
  nextCategory: EmployeeCategory;
};
type PositionAssignmentEditState = {
  assignment: EmployeePositionAssignment;
  position: CanonicalPosition | "";
  effectiveFrom: string;
  comment: string;
} | null;
type PositionAssignmentDeleteState = {
  assignment: EmployeePositionAssignment;
  comment: string;
} | null;
type ClosedPayrollPeriod = {
  id: string;
  start_date: string;
  end_date: string;
  label?: string;
};
type PendingClosedPeriodAction =
  | {
      type: "change-position";
      payload: EmployeePositionChangePayload;
      periods: ClosedPayrollPeriod[];
    }
  | {
      type: "patch-assignment";
      assignmentId: string;
      payload: EmployeePositionAssignmentPatch;
      periods: ClosedPayrollPeriod[];
    }
  | {
      type: "delete-assignment";
      assignmentId: string;
      payload: EmployeePositionAssignmentDeletePayload;
      periods: ClosedPayrollPeriod[];
    };
type PendingAssignmentTarget = {
  employee: Employee;
  assignment: EmployeeRoleAssignment;
};
type SubstituteRoleDialogState = {
  assignment?: EmployeeRoleAssignment;
  targetPosition?: "Повар" | "Кассир";
} | null;
type ViewMode = "grid" | "table";
type StaffGroupFilter = "all" | "cook" | "staff";
type StaffTab = "employees" | "changes";

type EmployeeChangeFiltersState = {
  employeeId: string;
  changedFrom: string;
  changedTo: string;
  actionDate: string;
  changeType: string;
  source: EmployeeChangeSource | "all";
  actor: string;
  status: EmployeeChangeStatus | "all";
  onlyErrors: boolean;
  onlyRequiresReview: boolean;
  onlyRetroactive: boolean;
  includeSystemMigrations: boolean;
};

type DismissalReasonOption = {
  key: string;
  id?: string;
  code: string;
  label: string;
  requires_comment: boolean;
};

const changeSourceOptions: EmployeeChangeSource[] = ["app", "iiko_sync", "system_migration"];
const changeStatusOptions: EmployeeChangeStatus[] = [
  "success",
  "error",
  "requires_review",
  "skipped",
];
const changeTypeOptions = [
  "create_employee",
  "update_full_name",
  "update_position",
  "assign_role",
  "change_role",
  "close_role",
  "change_category",
  "set_senior",
  "unset_senior",
  "set_deputy_senior",
  "unset_deputy_senior",
  "change_pin",
  "notice_given",
  "notice_cancelled",
  "dismiss",
  "reinstate",
  "iiko_sync_create",
  "iiko_sync_update",
  "iiko_sync_deactivate",
  "iiko_sync_skipped",
  "iiko_sync_error",
];
const defaultChangeFilters: EmployeeChangeFiltersState = {
  employeeId: "all",
  changedFrom: "",
  changedTo: "",
  actionDate: "",
  changeType: "all",
  source: "all",
  actor: "",
  status: "all",
  onlyErrors: false,
  onlyRequiresReview: false,
  onlyRetroactive: false,
  includeSystemMigrations: false,
};
const dismissalReasonDefinitions: Array<Omit<DismissalReasonOption, "key" | "id">> = [
  { code: "voluntary", label: "По собственному желанию", requires_comment: false },
  { code: "no_show", label: "Не вышел на смену", requires_comment: false },
  { code: "discipline", label: "Нарушение дисциплины", requires_comment: false },
  { code: "failed_trial", label: "Не прошёл стажировку", requires_comment: false },
  { code: "layoff_no_shifts", label: "Сокращение/нет смен", requires_comment: false },
  { code: "transfer", label: "Перевод", requires_comment: false },
  { code: "other", label: "Другое", requires_comment: true },
];

export function StaffRoute({ onNavigate }: { onNavigate?: (path: string) => void }) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const canViewChanges = permissions.hasAnyPermission(PERMISSION_GROUPS.staffHistoryRead);
  const canCreateStaff = permissions.hasAnyPermission(PERMISSION_GROUPS.staffCreate);
  const canImportStaff = permissions.canPerformAction("staff.import");
  const [activeTab, setActiveTab] = useState<StaffTab>("employees");
  const [status, setStatus] = useState<StaffStatusFilter>("current");
  const [category, setCategory] = useState<EmployeeCategory | "all">("all");
  const [group, setGroup] = useState<StaffGroupFilter>("all");
  const [cookingStation, setCookingStation] = useState<CookingStation | "all">("all");
  const [positionFilter, setPositionFilter] = useState<StaffPositionFilter>("all");
  const [secondaryFilter, setSecondaryFilter] = useState<StaffSecondaryFilter>("all");
  const [onlyWithoutHireDate, setOnlyWithoutHireDate] = useState(false);
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [viewMode, setViewMode] = useState<ViewMode>("grid");
  const [selectedEmployeeId, setSelectedEmployeeId] = useState<string | null>(null);
  const [editorDirty, setEditorDirty] = useState(false);
  const [discardEditorOpen, setDiscardEditorOpen] = useState(false);
  const [selectedChangeId, setSelectedChangeId] = useState<string | null>(null);
  const [changeFilters, setChangeFilters] =
    useState<EmployeeChangeFiltersState>(defaultChangeFilters);
  const [createOpen, setCreateOpen] = useState(false);
  const [noticeTarget, setNoticeTarget] = useState<Employee | null>(null);
  const [noticeCancelTarget, setNoticeCancelTarget] = useState<Employee | null>(null);
  const [pendingAssignmentTarget, setPendingAssignmentTarget] =
    useState<PendingAssignmentTarget | null>(null);

  useEffect(() => {
    const timeoutId = window.setTimeout(() => setDebouncedSearch(search), 250);
    return () => window.clearTimeout(timeoutId);
  }, [search]);

  useEffect(() => {
    if (group !== "cook") {
      setCookingStation("all");
    }
  }, [group]);

  useEffect(() => {
    if (!canViewChanges && activeTab === "changes") {
      setActiveTab("employees");
    }
  }, [activeTab, canViewChanges]);

  useEffect(() => {
    if (!selectedEmployeeId) {
      setEditorDirty(false);
      setDiscardEditorOpen(false);
    }
  }, [selectedEmployeeId]);

  const employeesQuery = useQuery({
    queryKey: ["employees", status, category],
    queryFn: () =>
      getEmployees({
        status: status === "current" ? "all" : status,
        category: category === "all" ? undefined : category,
        includePending: true,
      }),
  });

  const allEmployeesQuery = useQuery({
    queryKey: ["employees", "staff-filter-options"],
    queryFn: () => getEmployees({ status: "all", includePending: true }),
  });

  const changesQuery = useQuery({
    queryKey: ["employees", "changes", changeFilters],
    queryFn: () =>
      getEmployeeChanges({
        employeeId: changeFilters.employeeId === "all" ? undefined : changeFilters.employeeId,
        changedFrom: dateTimeFilterStart(changeFilters.changedFrom),
        changedTo: dateTimeFilterEnd(changeFilters.changedTo),
        changeType: changeFilters.changeType === "all" ? undefined : changeFilters.changeType,
        source: changeFilters.source === "all" ? undefined : changeFilters.source,
        actor: changeFilters.actor.trim() || undefined,
        status: changeFilters.status === "all" ? undefined : changeFilters.status,
        onlyErrors: changeFilters.onlyErrors,
        onlyRequiresReview: changeFilters.onlyRequiresReview,
        includeSystemMigrations:
          changeFilters.includeSystemMigrations || changeFilters.source === "system_migration",
      }),
    enabled: canViewChanges && activeTab === "changes",
  });

  const iikoRolesQuery = useQuery({
    queryKey: ["employees", "iiko-roles"],
    queryFn: getIikoEmployeeRoles,
    enabled: createOpen,
  });

  const syncMutation = useMutation({
    mutationFn: syncEmployees,
    onSuccess: (result) => {
      toast.success(
        `Создано ${result.created}, обновлено ${result.updated}, деактивировано ${result.deactivated}`,
      );
      void queryClient.invalidateQueries({ queryKey: ["employees"] });
      void queryClient.invalidateQueries({ queryKey: ["employees-roster"] });
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
      void queryClient.invalidateQueries({ queryKey: ["employees-roster"] });
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось создать сотрудника"));
    },
  });

  const noticeMutation = useMutation({
    mutationFn: ({
      comment,
      employee,
      noticeDate,
    }: {
      employee: Employee;
      noticeDate: string;
      comment: string;
    }) =>
      recordEmployeeNotice(employee.id, {
        notice_date: noticeDate,
        comment: comment.trim() || undefined,
      }),
    onSuccess: (result, variables) => {
      toast.success(`Уведомление зафиксировано на ${formatDate(result.effective_from)}`);
      setNoticeTarget(null);
      invalidateEmployeeQueries(variables.employee.id);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось зафиксировать уведомление"));
    },
  });

  const cancelNoticeMutation = useMutation({
    mutationFn: ({ comment, employee }: { employee: Employee; comment: string }) =>
      cancelEmployeeNotice(employee.id, { comment: comment.trim() || undefined }),
    onSuccess: (_result, variables) => {
      toast.success("Уведомление отменено");
      setNoticeCancelTarget(null);
      invalidateEmployeeQueries(variables.employee.id);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось отменить уведомление"));
    },
  });

  function invalidateEmployeeQueries(employeeId?: string) {
    void queryClient.invalidateQueries({ queryKey: ["employees"] });
    void queryClient.invalidateQueries({ queryKey: ["employees-roster"] });
    void queryClient.invalidateQueries({ queryKey: ["employees", "changes"] });
    if (employeeId) {
      void queryClient.invalidateQueries({ queryKey: ["employees", employeeId, "changes"] });
    }
  }

  const optionEmployees = useMemo(
    () => allEmployeesQuery.data ?? employeesQuery.data ?? [],
    [allEmployeesQuery.data, employeesQuery.data],
  );
  const employeeById = useMemo(
    () => new Map(optionEmployees.map((employee) => [employee.id, employee])),
    [optionEmployees],
  );

  const employeesBeforePositionFilter = useMemo(() => {
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

  const secondaryFilterOptions = useMemo(
    () => secondaryFilterOptionsForPositionFilter(positionFilter),
    [positionFilter],
  );
  const visiblePositionFilterOptions = useMemo(
    () =>
      positionFilterOptions.filter(
        (option) =>
          option.value === "all" ||
          option.positions?.some((position) => canReadStaffPosition(position, permissions)),
      ),
    [permissions],
  );
  const allowedCreatePositions = useMemo(
    () => new Set(canonicalPositions.filter((position) => canCreateStaffPosition(position, permissions))),
    [permissions],
  );

  useEffect(() => {
    if (!visiblePositionFilterOptions.some((option) => option.value === positionFilter)) {
      setPositionFilter("all");
      setSecondaryFilter("all");
    }
  }, [positionFilter, visiblePositionFilterOptions]);

  const positionFilterCounts = useMemo(
    () =>
      Object.fromEntries(
        visiblePositionFilterOptions.map((option) => [
          option.value,
          employeesBeforePositionFilter.filter((employee) =>
            employeeMatchesPositionFilter(employee, option.value),
          ).length,
        ]),
      ) as Record<StaffPositionFilter, number>,
    [employeesBeforePositionFilter, visiblePositionFilterOptions],
  );

  const employeesBeforeSecondaryFilter = useMemo(
    () =>
      employeesBeforePositionFilter.filter((employee) =>
        employeeMatchesPositionFilter(employee, positionFilter),
      ),
    [employeesBeforePositionFilter, positionFilter],
  );

  const secondaryFilterCounts = useMemo(
    () =>
      new Map(
        secondaryFilterOptions.map((option) => [
          option.value,
          option.value === "all"
            ? employeesBeforeSecondaryFilter.length
            : employeesBeforeSecondaryFilter.filter((employee) =>
                employeeMatchesSecondaryFilter(employee, positionFilter, option.value),
              ).length,
        ]),
      ),
    [employeesBeforeSecondaryFilter, positionFilter, secondaryFilterOptions],
  );

  const employeesBeforeHireDateFilter = useMemo(
    () =>
      employeesBeforeSecondaryFilter.filter((employee) =>
        employeeMatchesSecondaryFilter(employee, positionFilter, secondaryFilter),
      ),
    [employeesBeforeSecondaryFilter, positionFilter, secondaryFilter],
  );

  const employees = useMemo(
    () =>
      onlyWithoutHireDate
        ? employeesBeforeHireDateFilter.filter((employee) => employee.hire_date === null)
        : employeesBeforeHireDateFilter,
    [employeesBeforeHireDateFilter, onlyWithoutHireDate],
  );

  function handlePositionFilterChange(value: string) {
    setPositionFilter(value as StaffPositionFilter);
    setSecondaryFilter("all");
  }

  const selectedEmployee = useMemo(
    () => optionEmployees.find((employee) => employee.id === selectedEmployeeId) ?? null,
    [optionEmployees, selectedEmployeeId],
  );
  const changes = useMemo(() => {
    const rows = changesQuery.data ?? [];
    if (!changeFilters.actionDate && !changeFilters.onlyRetroactive) {
      return rows;
    }
    return rows.filter((change) => {
      const matchesActionDate = changeFilters.actionDate
        ? changeMatchesActionDate(change, changeFilters.actionDate)
        : true;
      const matchesRetroactive = changeFilters.onlyRetroactive ? isRetroactiveChange(change) : true;
      return matchesActionDate && matchesRetroactive;
    });
  }, [changeFilters.actionDate, changeFilters.onlyRetroactive, changesQuery.data]);
  const selectedChange = useMemo(
    () => changes.find((change) => change.id === selectedChangeId) ?? null,
    [changes, selectedChangeId],
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
    positionFilter === "all" &&
    secondaryFilter === "all" &&
    !onlyWithoutHireDate &&
    cookingStation === "all" &&
    debouncedSearch.trim() === "";
  const emptyEmployeesTitle = onlyWithoutHireDate
    ? "Все сотрудники имеют дату приёма"
    : "Сотрудники не найдены";
  const emptyEmployeesDescription = onlyWithoutHireDate
    ? "В выбранном списке нет сотрудников без даты приёма."
    : "Измените поиск или фильтры.";
  const emptyEmployeesTableMessage = onlyWithoutHireDate
    ? "Все сотрудники имеют дату приёма"
    : "Сотрудники по выбранным фильтрам не найдены";

  const tableColumns = useMemo<Array<DataTableColumn<Employee>>>(
    () => [
      {
        key: "employee",
        header: "Сотрудник",
        cell: (employee) => (
          <div className="flex min-w-[220px] items-center gap-3">
            <EmployeeAvatar employee={employee} />
            <div className="min-w-0">
              <div className="flex min-w-0 items-center gap-2">
                <div className="truncate font-medium">{employee.full_name}</div>
                <RoleReviewMarker employee={employee} />
                {!employee.hire_date ? (
                  <span
                    aria-label="Дата приёма не указана"
                    className="h-2.5 w-2.5 shrink-0 rounded-full bg-amber-500"
                    title="Дата приёма не указана"
                  />
                ) : null}
              </div>
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
        cell: (employee) => (
          <CategoryCell
            employee={employee}
            onPendingClick={(assignment) =>
              setPendingAssignmentTarget({ employee, assignment })
            }
          />
        ),
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
        cell: (employee) => (
          <EmployeeTags
            employee={employee}
            compact
            onPendingClick={(assignment) =>
              setPendingAssignmentTarget({ employee, assignment })
            }
          />
        ),
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
          <div className="flex justify-end gap-2">
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
            {canEditStaffEmployee(employee, permissions) ? (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    aria-label="Действия сотрудника"
                    onClick={(event) => event.stopPropagation()}
                    size="icon"
                    variant="ghost"
                  >
                    <MoreHorizontal size={16} aria-hidden="true" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" onClick={(event) => event.stopPropagation()}>
                  {employee.active_notice ? (
                    <DropdownMenuItem onSelect={() => setNoticeCancelTarget(employee)}>
                      <BellOff size={16} aria-hidden="true" />
                      Отменить уведомление
                    </DropdownMenuItem>
                  ) : (
                    <DropdownMenuItem onSelect={() => setNoticeTarget(employee)}>
                      <BellPlus size={16} aria-hidden="true" />
                      Зафиксировать уведомление об уходе
                    </DropdownMenuItem>
                  )}
                </DropdownMenuContent>
              </DropdownMenu>
            ) : null}
          </div>
        ),
      },
    ],
    [permissions],
  );

  const changeColumns = useMemo<Array<DataTableColumn<EmployeeChangeEvent>>>(
    () => [
      {
        key: "changed_at",
        header: "Дата/время изменения",
        cell: (change) => (
          <span className="whitespace-nowrap text-sm">{formatDateTime(change.changed_at)}</span>
        ),
      },
      {
        key: "effective",
        header: "Дата действия",
        cell: (change) => (
          <span className="whitespace-nowrap text-sm">{formatEffectivePeriod(change)}</span>
        ),
      },
      {
        key: "employee",
        header: "Сотрудник",
        cell: (change) => (
          <span className="block min-w-[180px] max-w-[240px] truncate font-medium">
            {employeeNameForChange(change, employeeById)}
          </span>
        ),
      },
      {
        key: "change_type",
        header: "Тип изменения",
        cell: (change) => (
          <span className="block min-w-[150px]">{changeTypeLabel(change.change_type)}</span>
        ),
      },
      {
        key: "source",
        header: "Источник",
        cell: (change) => (
          <span className="whitespace-nowrap">{EMPLOYEE_CHANGE_SOURCE_LABELS[change.source]}</span>
        ),
      },
      {
        key: "actor",
        header: "Кто изменил",
        cell: (change) => (
          <span className="block max-w-[180px] truncate text-muted-foreground">
            {change.actor_label || "Система"}
          </span>
        ),
      },
      {
        key: "summary",
        header: "Краткое описание",
        cell: (change) => <span className="block min-w-[220px]">{change.summary}</span>,
      },
      {
        key: "status",
        header: "Статус",
        cell: (change) => <EmployeeChangeStatusBadge status={change.status} />,
      },
    ],
    [employeeById],
  );

  return (
    <div className="space-y-5">
      <PageHeader
        title="Штат"
        description="Реестр сотрудников для графика и зарплаты."
        action={
          activeTab === "employees" ? (
            <div className="flex flex-wrap items-center gap-2">
              {canCreateStaff ? (
                <Button onClick={() => setCreateOpen(true)} variant="outline">
                  <UserPlus size={16} aria-hidden="true" />
                  Создать сотрудника
                </Button>
              ) : null}
              {canImportStaff ? (
                <Button onClick={() => syncMutation.mutate()} disabled={syncMutation.isPending}>
                  {syncMutation.isPending ? (
                    <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                  ) : (
                    <DatabaseZap size={16} aria-hidden="true" />
                  )}
                  Загрузить из iiko
                </Button>
              ) : null}
            </div>
          ) : undefined
        }
      />

      <Tabs
        className="space-y-5"
        onValueChange={(value) => setActiveTab(value as StaffTab)}
        value={activeTab}
      >
        <TabsList>
          <TabsTrigger value="employees">Сотрудники</TabsTrigger>
          {canViewChanges ? (
            <TabsTrigger className="gap-2" value="changes">
              <History size={15} aria-hidden="true" />
              Изменения
            </TabsTrigger>
          ) : null}
        </TabsList>

        <TabsContent className="mt-0 space-y-5" value="employees">
          <section className="grid gap-3 md:grid-cols-3">
            <StaffMetric label="Всего в реестре" value={stats.total} />
            <StaffMetric label="Активны" value={stats.active} tone="success" />
            <StaffMetric label="Требуют проверки" value={stats.needsSetup} tone="warning" />
          </section>

          <div className="space-y-2">
            <Tabs onValueChange={handlePositionFilterChange} value={positionFilter}>
              <div className="-mx-1 overflow-x-auto px-1">
                <TabsList className="inline-flex h-10 min-w-max justify-start">
                  {visiblePositionFilterOptions.map((option) => (
                    <TabsTrigger
                      className="whitespace-nowrap"
                      value={option.value}
                      key={option.value}
                    >
                      {option.label} ({positionFilterCounts[option.value]})
                    </TabsTrigger>
                  ))}
                </TabsList>
              </div>
            </Tabs>

            {secondaryFilterOptions.length > 0 ? (
              <Tabs
                onValueChange={(value) => setSecondaryFilter(value as StaffSecondaryFilter)}
                value={secondaryFilter}
              >
                <div className="overflow-x-auto pl-3">
                  <TabsList className="inline-flex h-9 min-w-max justify-start">
                    {secondaryFilterOptions.map((option) => (
                      <TabsTrigger
                        className="whitespace-nowrap"
                        value={option.value}
                        key={option.value}
                      >
                        {option.label} ({secondaryFilterCounts.get(option.value) ?? 0})
                      </TabsTrigger>
                    ))}
                  </TabsList>
                </div>
              </Tabs>
            ) : null}
          </div>

          <section className="grid gap-3 rounded-lg border bg-card p-3 lg:grid-cols-[minmax(220px,1fr)_160px_160px_160px_160px_190px_auto] lg:items-end">
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
                onValueChange={(value) => setStatus(value as StaffStatusFilter)}
                value={status}
              >
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

            <label className="flex h-10 items-center gap-2 rounded-md border border-input bg-background px-3 text-sm">
              <input
                checked={onlyWithoutHireDate}
                className="h-4 w-4"
                onChange={(event) => setOnlyWithoutHireDate(event.target.checked)}
                type="checkbox"
              />
              <span>Только без даты приёма</span>
            </label>

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
                canImportStaff ? (
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
                ) : undefined
              }
            />
          ) : viewMode === "grid" ? (
            <StaffGrid
              employees={employees}
              emptyDescription={emptyEmployeesDescription}
              emptyTitle={emptyEmployeesTitle}
              isLoading={employeesQuery.isLoading}
              onPendingClick={(employee, assignment) =>
                setPendingAssignmentTarget({ employee, assignment })
              }
              onSelect={(employee) => setSelectedEmployeeId(employee.id)}
            />
          ) : (
            <DataTable
              columns={tableColumns}
              rows={employees}
              isLoading={employeesQuery.isLoading}
              getRowKey={(employee) => employee.id}
              onRowClick={(employee) => setSelectedEmployeeId(employee.id)}
              emptyMessage={emptyEmployeesTableMessage}
            />
          )}
        </TabsContent>

        {canViewChanges ? (
          <TabsContent className="mt-0 space-y-4" value="changes">
            <EmployeeChangesPanel
              changes={changes}
              columns={changeColumns}
              employees={optionEmployees}
              filters={changeFilters}
              isError={changesQuery.isError}
              error={changesQuery.error}
              isLoading={changesQuery.isLoading || changesQuery.isFetching}
              onFiltersChange={setChangeFilters}
              onSelectChange={(change) => setSelectedChangeId(change.id)}
            />
          </TabsContent>
        ) : null}
      </Tabs>

      <Sheet
        open={Boolean(selectedEmployee)}
        onOpenChange={(open) => {
          if (!open) {
            if (editorDirty) {
              setDiscardEditorOpen(true);
              return;
            }
            setSelectedEmployeeId(null);
          }
        }}
      >
        <SheetContent className="w-full overflow-y-auto sm:max-w-xl" side="right">
          {selectedEmployee ? (
            <StaffEditor
              employee={selectedEmployee}
              onNavigate={onNavigate}
              onPendingClick={(assignment) =>
                setPendingAssignmentTarget({ employee: selectedEmployee, assignment })
              }
              onClose={() => {
                setEditorDirty(false);
                setSelectedEmployeeId(null);
              }}
              onDirtyChange={setEditorDirty}
              onShowChanges={(employeeId) => {
                setChangeFilters((current) => ({
                  ...current,
                  employeeId,
                }));
                setActiveTab("changes");
                setEditorDirty(false);
                setSelectedEmployeeId(null);
              }}
            />
          ) : null}
        </SheetContent>
      </Sheet>

      <AlertDialog open={discardEditorOpen} onOpenChange={setDiscardEditorOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Закрыть без сохранения?</AlertDialogTitle>
            <AlertDialogDescription>Изменения будут потеряны.</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => setDiscardEditorOpen(false)}>
              Отмена
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                setDiscardEditorOpen(false);
                setEditorDirty(false);
                setSelectedEmployeeId(null);
              }}
            >
              Закрыть
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <Sheet
        open={Boolean(selectedChange)}
        onOpenChange={(open) => {
          if (!open) {
            setSelectedChangeId(null);
          }
        }}
      >
        <SheetContent className="w-full overflow-y-auto sm:max-w-2xl" side="right">
          {selectedChange ? (
            <EmployeeChangeDetails
              change={selectedChange}
              employee={
                selectedChange.employee_id ? employeeById.get(selectedChange.employee_id) : null
              }
            />
          ) : null}
        </SheetContent>
      </Sheet>

      <CreateEmployeeDialog
        allowedPositions={allowedCreatePositions}
        isPending={createMutation.isPending}
        onCreate={(payload) => createMutation.mutate(payload)}
        onOpenChange={setCreateOpen}
        open={createOpen}
        roles={iikoRolesQuery.data ?? []}
        rolesError={iikoRolesQuery.error}
        rolesLoading={iikoRolesQuery.isLoading || iikoRolesQuery.isFetching}
      />

      <EmployeeNoticeDialog
        employee={noticeTarget}
        isPending={noticeMutation.isPending}
        onOpenChange={(open) => {
          if (!open) {
            setNoticeTarget(null);
          }
        }}
        onSubmit={(employee, noticeDate, comment) =>
          noticeMutation.mutate({ employee, noticeDate, comment })
        }
      />

      <EmployeeNoticeCancelDialog
        employee={noticeCancelTarget}
        isPending={cancelNoticeMutation.isPending}
        onOpenChange={(open) => {
          if (!open) {
            setNoticeCancelTarget(null);
          }
        }}
        onSubmit={(employee, comment) => cancelNoticeMutation.mutate({ employee, comment })}
      />

      <PendingAssignmentDialog
        target={pendingAssignmentTarget}
        onClose={() => setPendingAssignmentTarget(null)}
      />
    </div>
  );
}

function EmployeeNoticeDialog({
  employee,
  isPending,
  onOpenChange,
  onSubmit,
}: {
  employee: Employee | null;
  isPending: boolean;
  onOpenChange: (open: boolean) => void;
  onSubmit: (employee: Employee, noticeDate: string, comment: string) => void;
}) {
  const [noticeDate, setNoticeDate] = useState(todayDateInputValue);
  const [comment, setComment] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);

  useEffect(() => {
    if (!employee) {
      return;
    }
    setNoticeDate(todayDateInputValue());
    setComment("");
    setConfirmOpen(false);
  }, [employee?.id]);

  return (
    <>
      <Dialog open={Boolean(employee)} onOpenChange={onOpenChange}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Уведомление об уходе</DialogTitle>
            <DialogDescription>{employee?.full_name}</DialogDescription>
          </DialogHeader>

          <div className="grid gap-4">
            <Label className="grid gap-2">
              <span>Дата уведомления</span>
              <Input
                onChange={(event) => setNoticeDate(event.target.value)}
                type="date"
                value={noticeDate}
              />
            </Label>
            <Label className="grid gap-2">
              <span>Комментарий (опционально)</span>
              <textarea
                className="min-h-24 rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                onChange={(event) => setComment(event.target.value)}
                value={comment}
              />
            </Label>
          </div>

          <DialogFooter>
            <Button disabled={isPending} onClick={() => onOpenChange(false)} variant="outline">
              Отмена
            </Button>
            <Button
              disabled={!employee || !noticeDate || isPending}
              onClick={() => setConfirmOpen(true)}
              type="button"
            >
              <BellPlus size={16} aria-hidden="true" />
              Зафиксировать
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Зафиксировать уведомление?</AlertDialogTitle>
            <AlertDialogDescription>
              {employee?.full_name}, дата уведомления: {formatDate(noticeDate)}.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isPending}>Отмена</AlertDialogCancel>
            <AlertDialogAction
              disabled={!employee || isPending}
              onClick={() => {
                if (employee) {
                  onSubmit(employee, noticeDate, comment);
                }
              }}
            >
              Подтвердить
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

function EmployeeNoticeCancelDialog({
  employee,
  isPending,
  onOpenChange,
  onSubmit,
}: {
  employee: Employee | null;
  isPending: boolean;
  onOpenChange: (open: boolean) => void;
  onSubmit: (employee: Employee, comment: string) => void;
}) {
  const [comment, setComment] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);

  useEffect(() => {
    if (!employee) {
      return;
    }
    setComment("");
    setConfirmOpen(false);
  }, [employee?.id]);

  return (
    <>
      <Dialog open={Boolean(employee)} onOpenChange={onOpenChange}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Отменить уведомление</DialogTitle>
            <DialogDescription>{employee?.full_name}</DialogDescription>
          </DialogHeader>

          <Label className="grid gap-2">
            <span>Комментарий (опционально)</span>
            <textarea
              className="min-h-24 rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
              onChange={(event) => setComment(event.target.value)}
              value={comment}
            />
          </Label>

          <DialogFooter>
            <Button disabled={isPending} onClick={() => onOpenChange(false)} variant="outline">
              Отмена
            </Button>
            <Button
              disabled={!employee || isPending}
              onClick={() => setConfirmOpen(true)}
              type="button"
              variant="destructive"
            >
              <BellOff size={16} aria-hidden="true" />
              Отменить уведомление
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Отменить уведомление?</AlertDialogTitle>
            <AlertDialogDescription>
              {employee?.full_name}. Запись останется в истории изменений.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isPending}>Назад</AlertDialogCancel>
            <AlertDialogAction
              disabled={!employee || isPending}
              onClick={() => {
                if (employee) {
                  onSubmit(employee, comment);
                }
              }}
            >
              Отменить уведомление
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

function PendingAssignmentDialog({
  onClose,
  target,
}: {
  onClose: () => void;
  target: PendingAssignmentTarget | null;
}) {
  const queryClient = useQueryClient();
  const [effectiveDate, setEffectiveDate] = useState("");
  const [comment, setComment] = useState("");
  const [retroConfirmOpen, setRetroConfirmOpen] = useState(false);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const assignment = target?.assignment ?? null;
  const employee = target?.employee ?? null;
  const today = todayDateInputValue();
  const isPastDate = Boolean(effectiveDate) && effectiveDate < today;
  const canSave =
    Boolean(assignment) &&
    Boolean(effectiveDate) &&
    (!isPastDate || comment.trim().length > 0);

  useEffect(() => {
    setEffectiveDate(assignment?.effective_from ?? "");
    setComment("");
    setRetroConfirmOpen(false);
    setDeleteConfirmOpen(false);
  }, [assignment?.id]);

  const changesQuery = useQuery({
    queryKey: ["employees", employee?.id, "changes", "pending-assignment", assignment?.id],
    queryFn: () => getEmployeeChanges({ employeeId: employee?.id }),
    enabled: Boolean(employee && assignment),
  });
  const auditEvent = useMemo(() => {
    if (!assignment) {
      return null;
    }
    return (changesQuery.data ?? []).find(
      (change) =>
        change.related_entity_id === assignment.id ||
        String((change.after_value ?? {})["id"] ?? "") === assignment.id,
    );
  }, [assignment, changesQuery.data]);

  const patchMutation = useMutation({
    mutationFn: () => {
      if (!employee || !assignment) {
        throw new Error("Не выбрано запланированное изменение");
      }
      return patchEmployeeAssignment(employee.id, assignment.id, {
        effective_from: effectiveDate,
        comment: comment.trim() || undefined,
      });
    },
    onSuccess: () => {
      toast.success(
        effectiveDate < today
          ? `Изменено задним числом с ${formatDate(effectiveDate)}`
          : `Запланировано: с ${formatDate(effectiveDate)}`,
      );
      onClose();
      void queryClient.invalidateQueries({ queryKey: ["employees"] });
      if (employee) {
        void queryClient.invalidateQueries({ queryKey: ["employees", employee.id, "assignments"] });
        void queryClient.invalidateQueries({ queryKey: ["employees", employee.id, "changes"] });
      }
      void queryClient.invalidateQueries({ queryKey: ["employees", "changes"] });
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось сохранить запланированное изменение"));
    },
  });
  const deleteMutation = useMutation({
    mutationFn: () => {
      if (!employee || !assignment) {
        throw new Error("Не выбрано запланированное изменение");
      }
      return deleteEmployeeAssignment(employee.id, assignment.id);
    },
    onSuccess: () => {
      setDeleteConfirmOpen(false);
      toast.success("Запланированное изменение удалено");
      onClose();
      void queryClient.invalidateQueries({ queryKey: ["employees"] });
      if (employee) {
        void queryClient.invalidateQueries({ queryKey: ["employees", employee.id, "assignments"] });
        void queryClient.invalidateQueries({ queryKey: ["employees", employee.id, "changes"] });
      }
      void queryClient.invalidateQueries({ queryKey: ["employees", "changes"] });
    },
    onError: (error) => {
      setDeleteConfirmOpen(false);
      toast.error(apiErrorMessage(error, "Не удалось удалить запланированное изменение"));
    },
  });

  const current = employee && assignment ? currentAssignmentForPending(employee, assignment) : null;
  const isBusy = patchMutation.isPending || deleteMutation.isPending;

  return (
    <>
      <Dialog
        open={Boolean(target)}
        onOpenChange={(open) => {
          if (!open && !isBusy) {
            onClose();
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Запланированное изменение</DialogTitle>
            <DialogDescription>
              {employee?.full_name}
              {assignment ? `, ${payrollRoleLabel(assignment.payroll_role)}` : ""}
            </DialogDescription>
          </DialogHeader>

          {assignment ? (
            <div className="grid gap-4">
              <div className="grid gap-2 rounded-md border bg-muted/30 p-3 text-sm">
                <InfoRow
                  label={`С ${formatDate(assignment.effective_from)}`}
                  value={`${categoryLabel(assignment.category)}${
                    current ? ` вместо ${categoryLabel(current.category)}` : ""
                  }`}
                />
                <InfoRow label="Создано" value={formatDateTime(assignment.created_at)} />
                <InfoRow
                  label="Автор"
                  value={auditEvent?.actor_label || "Не указан"}
                />
                <InfoRow
                  label="Комментарий"
                  value={auditEvent?.comment || "Не указан"}
                />
              </div>

              <Label className="grid gap-2">
                <span>Изменить дату</span>
                <Input
                  disabled={isBusy}
                  onChange={(event) => setEffectiveDate(event.target.value)}
                  type="date"
                  value={effectiveDate}
                />
              </Label>

              {isPastDate ? (
                <Label className="grid gap-2">
                  <span>Комментарий</span>
                  <Textarea
                    disabled={isBusy}
                    maxLength={1000}
                    onChange={(event) => setComment(event.target.value)}
                    placeholder="Обязателен для изменения задним числом"
                    value={comment}
                  />
                </Label>
              ) : null}
            </div>
          ) : null}

          <DialogFooter className="gap-2 sm:justify-between">
            <Button
              disabled={!assignment || isBusy}
              onClick={() => setDeleteConfirmOpen(true)}
              type="button"
              variant="destructive"
            >
              <Trash2 size={16} aria-hidden="true" />
              Удалить запланированное
            </Button>
            <div className="flex justify-end gap-2">
              <Button disabled={isBusy} onClick={onClose} type="button" variant="outline">
                Отмена
              </Button>
              <Button
                disabled={!canSave || isBusy}
                onClick={() => {
                  if (isPastDate) {
                    setRetroConfirmOpen(true);
                    return;
                  }
                  patchMutation.mutate();
                }}
                type="button"
              >
                {patchMutation.isPending ? (
                  <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                ) : (
                  <Save size={16} aria-hidden="true" />
                )}
                Сохранить
              </Button>
            </div>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog open={retroConfirmOpen} onOpenChange={setRetroConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Применить изменение задним числом?</AlertDialogTitle>
            <AlertDialogDescription>
              Это перепишет историю с {formatDate(effectiveDate)}. Комментарий обязателен.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={patchMutation.isPending}>Отмена</AlertDialogCancel>
            <AlertDialogAction
              disabled={!canSave || patchMutation.isPending}
              onClick={(event) => {
                event.preventDefault();
                patchMutation.mutate();
              }}
            >
              Применить
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={deleteConfirmOpen} onOpenChange={setDeleteConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Удалить запланированное изменение?</AlertDialogTitle>
            <AlertDialogDescription>
              Текущая категория останется без изменений.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteMutation.isPending}>Отмена</AlertDialogCancel>
            <AlertDialogAction
              disabled={!assignment || deleteMutation.isPending}
              onClick={() => {
                deleteMutation.mutate();
              }}
            >
              {deleteMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : null}
              Удалить
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

function StaffGrid({
  emptyDescription = "Измените поиск или фильтры.",
  emptyTitle = "Сотрудники не найдены",
  employees,
  isLoading,
  onPendingClick,
  onSelect,
}: {
  emptyDescription?: string;
  emptyTitle?: string;
  employees: Employee[];
  isLoading: boolean;
  onPendingClick: (employee: Employee, assignment: EmployeeRoleAssignment) => void;
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
        title={emptyTitle}
        description={emptyDescription}
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
                <div className="flex min-w-0 items-center gap-2">
                  <div className="truncate text-lg font-semibold leading-6">
                    {employee.full_name}
                  </div>
                  <RoleReviewMarker employee={employee} />
                </div>
                <div className="mt-1 truncate text-sm text-muted-foreground">
                  {employee.position || "Должность не указана"}
                </div>
              </div>
              <EmployeeTags
                employee={employee}
                onPendingClick={(assignment) => onPendingClick(employee, assignment)}
              />
            </CardContent>
          </Card>
        </button>
      ))}
    </div>
  );
}

function EmployeeChangesPanel({
  changes,
  columns,
  employees,
  error,
  filters,
  isError,
  isLoading,
  onFiltersChange,
  onSelectChange,
}: {
  changes: EmployeeChangeEvent[];
  columns: Array<DataTableColumn<EmployeeChangeEvent>>;
  employees: Employee[];
  error: unknown;
  filters: EmployeeChangeFiltersState;
  isError: boolean;
  isLoading: boolean;
  onFiltersChange: Dispatch<SetStateAction<EmployeeChangeFiltersState>>;
  onSelectChange: (change: EmployeeChangeEvent) => void;
}) {
  function updateFilters(patch: Partial<EmployeeChangeFiltersState>) {
    onFiltersChange((current) => ({ ...current, ...patch }));
  }

  const filtersAreDefault = JSON.stringify(filters) === JSON.stringify(defaultChangeFilters);

  return (
    <>
      <section className="grid gap-3 rounded-lg border bg-card p-3 xl:grid-cols-[minmax(220px,1.4fr)_repeat(4,minmax(150px,1fr))]">
        <Label className="grid gap-1 text-sm">
          <span className="text-xs font-medium uppercase text-muted-foreground">Сотрудник</span>
          <Select
            onValueChange={(value) => updateFilters({ employeeId: value })}
            value={filters.employeeId}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Все</SelectItem>
              {employees.map((employee) => (
                <SelectItem value={employee.id} key={employee.id}>
                  {employee.full_name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Label>

        <Label className="grid gap-1 text-sm">
          <span className="text-xs font-medium uppercase text-muted-foreground">Изменено с</span>
          <Input
            onChange={(event) => updateFilters({ changedFrom: event.target.value })}
            type="date"
            value={filters.changedFrom}
          />
        </Label>

        <Label className="grid gap-1 text-sm">
          <span className="text-xs font-medium uppercase text-muted-foreground">Изменено по</span>
          <Input
            onChange={(event) => updateFilters({ changedTo: event.target.value })}
            type="date"
            value={filters.changedTo}
          />
        </Label>

        <Label className="grid gap-1 text-sm">
          <span className="text-xs font-medium uppercase text-muted-foreground">Дата действия</span>
          <Input
            onChange={(event) => updateFilters({ actionDate: event.target.value })}
            type="date"
            value={filters.actionDate}
          />
        </Label>

        <Label className="grid gap-1 text-sm">
          <span className="text-xs font-medium uppercase text-muted-foreground">Тип</span>
          <Select
            onValueChange={(value) => updateFilters({ changeType: value })}
            value={filters.changeType}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Все</SelectItem>
              {changeTypeOptions.map((type) => (
                <SelectItem value={type} key={type}>
                  {changeTypeLabel(type)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Label>

        <Label className="grid gap-1 text-sm">
          <span className="text-xs font-medium uppercase text-muted-foreground">Источник</span>
          <Select
            onValueChange={(value) =>
              updateFilters({ source: value as EmployeeChangeSource | "all" })
            }
            value={filters.source}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Все</SelectItem>
              {changeSourceOptions.map((source) => (
                <SelectItem value={source} key={source}>
                  {EMPLOYEE_CHANGE_SOURCE_LABELS[source]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Label>

        <Label className="grid gap-1 text-sm">
          <span className="text-xs font-medium uppercase text-muted-foreground">Автор</span>
          <Input
            onChange={(event) => updateFilters({ actor: event.target.value })}
            placeholder="Имя, роль или ID"
            value={filters.actor}
          />
        </Label>

        <Label className="grid gap-1 text-sm">
          <span className="text-xs font-medium uppercase text-muted-foreground">Статус</span>
          <Select
            onValueChange={(value) =>
              updateFilters({ status: value as EmployeeChangeStatus | "all" })
            }
            value={filters.status}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Все</SelectItem>
              {changeStatusOptions.map((status) => (
                <SelectItem value={status} key={status}>
                  {EMPLOYEE_CHANGE_STATUS_LABELS[status]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Label>

        <div className="grid gap-2 xl:col-span-3">
          <span className="text-xs font-medium uppercase text-muted-foreground">
            Быстрые переключатели
          </span>
          <div className="flex flex-wrap gap-2">
            <Button
              onClick={() => updateFilters({ onlyErrors: !filters.onlyErrors })}
              size="sm"
              type="button"
              variant={filters.onlyErrors ? "secondary" : "outline"}
            >
              Только ошибки
            </Button>
            <Button
              onClick={() => updateFilters({ onlyRequiresReview: !filters.onlyRequiresReview })}
              size="sm"
              type="button"
              variant={filters.onlyRequiresReview ? "secondary" : "outline"}
            >
              Только требует проверки
            </Button>
            <Button
              onClick={() => updateFilters({ onlyRetroactive: !filters.onlyRetroactive })}
              size="sm"
              type="button"
              variant={filters.onlyRetroactive ? "secondary" : "outline"}
            >
              Только задним числом
            </Button>
          </div>
        </div>

        <div className="flex flex-wrap items-end gap-3 xl:col-span-2">
          <label className="flex h-10 items-center gap-2 rounded-md border bg-background px-3 text-sm">
            <input
              checked={filters.includeSystemMigrations || filters.source === "system_migration"}
              disabled={filters.source === "system_migration"}
              onChange={(event) => updateFilters({ includeSystemMigrations: event.target.checked })}
              type="checkbox"
            />
            <span>Include system migrations</span>
          </label>
          <Button
            disabled={filtersAreDefault}
            onClick={() => onFiltersChange(defaultChangeFilters)}
            type="button"
            variant="outline"
          >
            <RotateCcw size={16} aria-hidden="true" />
            Сбросить
          </Button>
        </div>
      </section>

      {isError ? (
        <div className="flex items-center gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          <ShieldAlert size={16} aria-hidden="true" />
          {apiErrorMessage(error, "Не удалось загрузить изменения штата")}
        </div>
      ) : null}

      <DataTable
        columns={columns}
        rows={changes}
        isLoading={isLoading}
        getRowKey={(change) => change.id}
        onRowClick={onSelectChange}
        emptyMessage="Изменения по выбранным фильтрам не найдены"
      />
    </>
  );
}

function EmployeeChangeDetails({
  change,
  employee,
}: {
  change: EmployeeChangeEvent;
  employee: Employee | null | undefined;
}) {
  const diffRows = employeeChangeDiffRows(change);
  const payrollWarnings = payrollImpactWarnings(change);

  return (
    <div className="space-y-5">
      <SheetHeader>
        <SheetTitle className="pr-8">{changeTypeLabel(change.change_type)}</SheetTitle>
        <SheetDescription>{change.summary}</SheetDescription>
      </SheetHeader>

      <div className="grid gap-2 rounded-lg border bg-card p-4 text-sm">
        <InfoRow label="Сотрудник" value={employee?.full_name ?? employeeNameForChange(change)} />
        <InfoRow label="Источник" value={EMPLOYEE_CHANGE_SOURCE_LABELS[change.source]} />
        <InfoRow label="Кто изменил" value={change.actor_label || "Система"} />
        <InfoRow label="Дата/время изменения" value={formatDateTime(change.changed_at)} />
        <InfoRow label="Дата действия" value={formatEffectivePeriod(change)} />
        <div className="flex items-center justify-between gap-3">
          <span className="text-muted-foreground">Статус</span>
          <EmployeeChangeStatusBadge status={change.status} />
        </div>
      </div>

      <section className="grid gap-3 rounded-lg border bg-card p-4">
        <div className="text-sm font-medium">Изменения</div>
        {diffRows.length > 0 ? (
          <div className="grid gap-2">
            {diffRows.map((row) => (
              <div
                className="grid gap-2 rounded-md border bg-background p-3 sm:grid-cols-[160px_1fr]"
                key={row.label}
              >
                <div className="text-sm font-medium">{row.label}</div>
                {row.note ? (
                  <div className="text-sm">{row.note}</div>
                ) : (
                  <div className="grid gap-2 text-sm sm:grid-cols-2">
                    <DiffValue label="Было" value={row.before} />
                    <DiffValue label="Стало" value={row.after} />
                  </div>
                )}
              </div>
            ))}
          </div>
        ) : (
          <div className="rounded-md border bg-background px-3 py-2 text-sm text-muted-foreground">
            Детали изменения не переданы.
          </div>
        )}
      </section>

      <section className="grid gap-2 rounded-lg border bg-card p-4 text-sm">
        <div className="flex items-center justify-between gap-3">
          <span className="text-muted-foreground">Влияет на зарплату</span>
          <span className="font-medium">{change.payroll_impact ? "Да" : "Нет"}</span>
        </div>
        {payrollWarnings.map((warning) => (
          <div
            className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-amber-800"
            key={warning}
          >
            {warning}
          </div>
        ))}
      </section>

      {change.change_type === "dismiss" || change.reason || change.comment ? (
        <section className="grid gap-2 rounded-lg border bg-card p-4 text-sm">
          <div className="text-sm font-medium">Увольнение</div>
          <InfoRow label="Причина" value={change.reason_label || change.reason || "Не указана"} />
          <InfoRow label="Комментарий" value={change.comment || "Не указан"} />
        </section>
      ) : null}
    </div>
  );
}

function DiffValue({ label, value }: { label: string; value: string | undefined }) {
  return (
    <div className="min-w-0 rounded-md bg-muted/40 px-3 py-2">
      <div className="text-xs font-medium uppercase text-muted-foreground">{label}</div>
      <div className="mt-1 break-words font-medium">{value ?? "Не задано"}</div>
    </div>
  );
}

function EmployeeChangeHistoryPreview({
  changes,
  isError,
  isLoading,
  onShowAll,
}: {
  changes: EmployeeChangeEvent[];
  isError: boolean;
  isLoading: boolean;
  onShowAll: () => void;
}) {
  return (
    <section className="grid gap-3 rounded-lg border bg-card p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2 text-sm font-medium">
          <History size={16} aria-hidden="true" />
          <span>История изменений</span>
        </div>
        <Button onClick={onShowAll} size="sm" type="button" variant="outline">
          Показать все
        </Button>
      </div>

      {isLoading ? (
        <div className="grid gap-2">
          {Array.from({ length: 3 }).map((_, index) => (
            <div className="h-12 animate-pulse rounded-md bg-muted" key={index} />
          ))}
        </div>
      ) : isError ? (
        <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          Не удалось загрузить историю изменений
        </div>
      ) : changes.length === 0 ? (
        <div className="rounded-md border bg-background px-3 py-2 text-sm text-muted-foreground">
          История изменений пуста
        </div>
      ) : (
        <div className="grid gap-2">
          {changes.map((change) => (
            <div className="rounded-md border bg-background px-3 py-2" key={change.id}>
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="truncate text-sm font-medium">{change.summary}</div>
                  <div className="mt-1 text-xs text-muted-foreground">
                    {formatDateTime(change.changed_at)} · {changeTypeLabel(change.change_type)}
                  </div>
                </div>
                <EmployeeChangeStatusBadge status={change.status} />
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function EmployeeFundSection({ employee }: { employee: Employee }) {
  const [open, setOpen] = useState(false);
  const fundQuery = useQuery({
    queryKey: ["payroll-fund", "employee", employee.id],
    queryFn: () => getEmployeeAccumulationFund(employee.id),
    enabled: open,
  });
  const fund = fundQuery.data;
  const accounts = fund?.accounts ?? [];
  const transactionsByAccount = groupFundTransactions(fund?.transactions ?? []);

  return (
    <section className="rounded-lg border bg-card">
      <Accordion type="single" collapsible>
        <AccordionItem
          className="border-0 bg-transparent"
          open={open}
          onToggle={(event) => setOpen(event.currentTarget.open)}
          value="fund"
        >
          <AccordionTrigger className="px-4">
            <div className="flex min-w-0 items-center gap-2">
              <Banknote size={16} aria-hidden="true" />
              <span>Накопительный фонд</span>
              {fund?.employee?.fund_exclusion?.is_currently_excluded ? (
                <Badge variant="outline" className="border-amber-400 bg-amber-50 text-amber-800">
                  Исключён из фонда
                </Badge>
              ) : null}
            </div>
            {fund?.employee ? (
              <span className="shrink-0 text-xs text-muted-foreground">
                {fund.employee.tenure_months} мес. ·{" "}
                {formatFundPercent(fund.employee.current_rate_percent)}
              </span>
            ) : null}
          </AccordionTrigger>
          <AccordionContent className="grid gap-4">
            {fundQuery.isLoading ? (
              <div className="rounded-md border bg-background px-3 py-2 text-sm text-muted-foreground">
                Загрузка фонда
              </div>
            ) : fundQuery.isError ? (
              <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                Не удалось загрузить накопительный фонд
              </div>
            ) : fund?.employee ? (
              <>
                {fund.employee.fund_exclusion?.is_currently_excluded ? (
                  <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
                    <div className="font-medium">Сотрудник исключён из накопительного фонда</div>
                    {fund.employee.fund_exclusion.fund_excluded_until ? (
                      <div className="text-xs">
                        До {formatDate(fund.employee.fund_exclusion.fund_excluded_until)}
                      </div>
                    ) : null}
                    {fund.employee.fund_exclusion.fund_excluded_reason ? (
                      <div className="text-xs">
                        Причина: {fund.employee.fund_exclusion.fund_excluded_reason}
                      </div>
                    ) : null}
                  </div>
                ) : null}
                <div className="grid gap-3 md:grid-cols-3">
                  <EmployeeFundMetric
                    label="Стаж"
                    value={`${fund.employee.tenure_months} мес. (${formatDate(
                      fund.employee.tenure_started_at,
                    )})`}
                  />
                  <EmployeeFundMetric
                    label="Текущая ставка"
                    value={formatFundPercent(fund.employee.current_rate_percent)}
                  />
                  <EmployeeFundMetric
                    label="Следующий порог"
                    value={nextFundThresholdLabel(fund)}
                  />
                </div>

                {accounts.length === 0 ? (
                  <div className="rounded-md border bg-background px-3 py-2 text-sm text-muted-foreground">
                    Начислений ещё нет
                  </div>
                ) : (
                  <div className="grid gap-3">
                    {accounts.map((account) => (
                      <EmployeeFundAccountRow
                        account={account}
                        key={account.id}
                        transactions={transactionsByAccount.get(account.id) ?? []}
                      />
                    ))}
                  </div>
                )}
              </>
            ) : (
              <div className="rounded-md border bg-background px-3 py-2 text-sm text-muted-foreground">
                Начислений ещё нет
              </div>
            )}
          </AccordionContent>
        </AccordionItem>
      </Accordion>
    </section>
  );
}

function EmployeeFundMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid min-w-0 gap-1 rounded-md border bg-background px-3 py-2">
      <span className="text-sm text-muted-foreground">{label}</span>
      <span className="break-words text-sm font-semibold leading-5 text-foreground">{value}</span>
    </div>
  );
}

function EmployeeFundAccountRow({
  account,
  transactions,
}: {
  account: AccumulationFundAccount;
  transactions: AccumulationFundTransaction[];
}) {
  return (
    <div className="rounded-md border bg-background p-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2 text-sm font-medium">
            <span>{account.year} год</span>
            <FundStatusBadge status={account.status} />
          </div>
          <div className="mt-2 grid gap-1 text-sm text-muted-foreground">
            <span>Накоплено: {formatDepositMoney(account.accumulated)}</span>
            {account.status === "paid_out" ? (
              <span>
                Выплачено {formatDateOnlyFromDateTime(account.paid_out_at)}:{" "}
                {formatDepositMoney(account.paid_out)}
              </span>
            ) : account.status === "forfeited" ? (
              <span>{forfeitedFundLabel(account)}</span>
            ) : (
              <span>Выплата планируется {formatDate(account.planned_payout_date)}</span>
            )}
          </div>
        </div>
        <div className="text-right text-sm">
          <div className="text-muted-foreground">Остаток</div>
          <div className="font-medium tabular-nums">{formatDepositMoney(account.outstanding)}</div>
        </div>
      </div>
      <details className="mt-3">
        <summary className="cursor-pointer list-none text-sm font-medium text-primary">
          Транзакции
        </summary>
        <div className="mt-2 grid gap-2">
          {transactions.length === 0 ? (
            <div className="rounded-md border bg-muted/30 px-3 py-2 text-sm text-muted-foreground">
              Транзакций нет
            </div>
          ) : (
            transactions.map((transaction) => (
              <div
                className="grid gap-1 rounded-md border bg-muted/20 px-3 py-2 text-sm sm:grid-cols-[90px_1fr_auto] sm:items-center"
                key={transaction.id}
              >
                <span className="text-muted-foreground">
                  {formatDateOnlyFromDateTime(transaction.created_at)}
                </span>
                <span>{employeeFundTransactionLabel(transaction)}</span>
                <span className="font-medium tabular-nums">
                  {formatDepositMoney(transaction.amount)}
                </span>
              </div>
            ))
          )}
        </div>
      </details>
    </div>
  );
}

function groupFundTransactions(transactions: AccumulationFundTransaction[]) {
  const grouped = new Map<string, AccumulationFundTransaction[]>();
  for (const transaction of transactions) {
    const rows = grouped.get(transaction.account_id) ?? [];
    rows.push(transaction);
    grouped.set(transaction.account_id, rows);
  }
  return grouped;
}

function EmployeeAdjustmentsPreview({
  adjustments,
  employeeId,
  isError,
  isLoading,
  onNavigate,
}: {
  adjustments: PayrollAdjustment[];
  employeeId: string;
  isError: boolean;
  isLoading: boolean;
  onNavigate?: (path: string) => void;
}) {
  return (
    <section className="grid gap-3 rounded-lg border bg-card p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2 text-sm font-medium">
          <Banknote size={16} aria-hidden="true" />
          <span>Премии и штрафы за последние 60 дней</span>
        </div>
        <Button
          disabled={!onNavigate}
          onClick={() => onNavigate?.(`/payroll/adjustments?employee=${employeeId}`)}
          size="sm"
          type="button"
          variant="outline"
        >
          Все
        </Button>
      </div>

      {isLoading ? (
        <div className="grid gap-2">
          {Array.from({ length: 3 }).map((_, index) => (
            <div className="h-11 animate-pulse rounded-md bg-muted" key={index} />
          ))}
        </div>
      ) : isError ? (
        <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          Не удалось загрузить премии и штрафы
        </div>
      ) : adjustments.length === 0 ? (
        <div className="rounded-md border bg-background px-3 py-2 text-sm text-muted-foreground">
          За последние 60 дней корректировок нет
        </div>
      ) : (
        <div className="grid gap-2">
          {adjustments.map((adjustment) => (
            <div
              className="grid gap-2 rounded-md border bg-background px-3 py-2 text-sm sm:grid-cols-[88px_1fr_auto] sm:items-center"
              key={adjustment.id}
            >
              <span className="text-muted-foreground">{formatDate(adjustment.work_date)}</span>
              <span className="min-w-0 truncate">
                {adjustment.category_display_name || adjustment.custom_label || "Корректировка"}
              </span>
              <span className="inline-flex items-center justify-end gap-1 font-medium tabular-nums">
                {adjustment.type === "bonus" ? (
                  <Banknote size={14} aria-hidden="true" />
                ) : (
                  <CircleMinus size={14} aria-hidden="true" />
                )}
                {formatDepositMoney(numericAmount(adjustment.amount))}
              </span>
            </div>
          ))}
        </div>
      )}
    </section>
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
  allowedPositions,
  isPending,
  onCreate,
  onOpenChange,
  open,
  roles,
  rolesError,
  rolesLoading,
}: {
  allowedPositions: ReadonlySet<CanonicalPosition>;
  isPending: boolean;
  onCreate: (payload: EmployeeCreatePayload) => void;
  onOpenChange: (open: boolean) => void;
  open: boolean;
  roles: IikoEmployeeRole[];
  rolesError: unknown;
  rolesLoading: boolean;
}) {
  const [fullName, setFullName] = useState("");
  const [pinCode, setPinCode] = useState("");
  const [iikoRoleId, setIikoRoleId] = useState("");
  const [roleRows, setRoleRows] = useState<CreateEmployeeRoleRow[]>(() => [createRoleRow(true)]);
  const [cashierCategory, setCashierCategory] = useState<EmployeeCategory | "">("");
  const [isSenior, setIsSenior] = useState(false);
  const [isDeputySenior, setIsDeputySenior] = useState(false);

  const filteredRoles = useMemo(
    // Список приходит из реестра должностей и уже отфильтрован сервером по праву
    // создания (доступные актору активные должности) — доверяем ему как есть.
    () => roles.filter((role) => !role.deleted),
    [roles],
  );
  const selectedIikoRole = useMemo(
    () => filteredRoles.find((role) => role.id === iikoRoleId) ?? null,
    [filteredRoles, iikoRoleId],
  );
  const selectedPosition = canonicalPosition(selectedIikoRole?.name ?? null);
  const createPayrollRoleOptions = selectedPosition ? positionPayrollRoles[selectedPosition] : [];
  const premiumOptions = selectedPosition ? premiumApplicability[selectedPosition] : null;
  const showCashierCategory = selectedPosition === "Кассир";
  const showCookRoleSection = selectedPosition === "Повар";
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
      setCashierCategory("");
      setIsSenior(false);
      setIsDeputySenior(false);
    }
  }, [open]);

  useEffect(() => {
    if (!selectedPosition) {
      setRoleRows([createRoleRow(true)]);
      setCashierCategory("");
      setIsSenior(false);
      setIsDeputySenior(false);
      return;
    }
    const allowedRoles = positionPayrollRoles[selectedPosition];
    if (selectedPosition === "Кассир") {
      setRoleRows([]);
      setCashierCategory((current) =>
        isCategoryAllowedForRole("administrator", current) ? current : "",
      );
    } else if (allowedRoles.length === 0) {
      setRoleRows([]);
      setCashierCategory("");
    } else {
      setCashierCategory("");
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
    (showCashierCategory ? isCategoryAllowedForRole("administrator", cashierCategory) : true) &&
    (!showCookRoleSection ||
      (roleRows.length > 0 &&
        primaryCount === 1 &&
        roleRows.every((row) => {
          if (!row.payroll_role || !row.category) {
            return false;
          }
          return isCategoryAllowedForRole(row.payroll_role, row.category);
        })));
  const canSubmit =
    nameIsValid &&
    pinIsValid &&
    Boolean(iikoRoleId) &&
    rolesAreValid &&
    !isPending &&
    !rolesLoading;

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmit) {
      return;
    }
    onCreate({
      full_name: trimmedName,
      pin_code: pinCode,
      position: iikoRoleId,
      roles: showCashierCategory
        ? [
            {
              payroll_role: "administrator",
              category: cashierCategory as EmployeeCategory,
              is_primary: true,
            },
          ]
        : showCookRoleSection
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
      category: categories.length === 1 ? categories[0] : "",
    });
  }

  function setPrimaryRole(rowId: string) {
    setRoleRows((rows) => rows.map((row) => ({ ...row, is_primary: row.id === rowId })));
  }

  function addRoleRow() {
    const unusedRoles = createPayrollRoleOptions.filter((role) => !selectedRoleIds.has(role));
    if (unusedRoles.length === 0) {
      toast.error("Все доступные роли уже выбраны");
      return;
    }
    const nextRow = createRoleRow(false);
    if (unusedRoles.length === 1) {
      const payrollRole = unusedRoles[0];
      const categories = categoriesForPayrollRole(payrollRole);
      nextRow.payroll_role = payrollRole;
      nextRow.category = categories.length === 1 ? categories[0] : "";
    }
    setRoleRows((rows) => [...rows, nextRow]);
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
            <DialogDescription>
              Карточка будет заведена в iiko и добавлена в Штат.
            </DialogDescription>
          </DialogHeader>

          {rolesError ? (
            <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {apiErrorMessage(rolesError, "Не удалось загрузить роли iiko")}
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

          {showCashierCategory ? (
            <div className="grid gap-3 rounded-lg border bg-card p-4 sm:grid-cols-2">
              <StaticField label="Роль" value={PAYROLL_ROLE_LABELS.administrator} />

              <Label className="grid gap-1">
                <span className="text-xs font-medium uppercase text-muted-foreground">
                  Категория
                </span>
                <Select
                  disabled={isPending}
                  onValueChange={(value) => setCashierCategory(value as EmployeeCategory)}
                  value={cashierCategory}
                >
                  <SelectTrigger>
                    <SelectValue placeholder="Выберите категорию" />
                  </SelectTrigger>
                  <SelectContent>
                    {categoriesForPayrollRole("administrator").map((category) => (
                      <SelectItem value={category} key={category}>
                        {EMPLOYEE_CATEGORY_LABELS[category]}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Label>
            </div>
          ) : null}

          {showCookRoleSection ? (
            <div className="grid gap-3 rounded-lg border bg-card p-4">
              <div className="flex items-center justify-between gap-3">
                <div className="text-sm font-medium">Роли и категории</div>
                <Button
                  disabled={isPending || !hasUnusedCreateRole}
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
                  const rowRoleOptions = roleOptionsForRow(row);
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

                      {row.payroll_role && rowRoleOptions.length === 1 ? (
                        <StaticField label="Роль" value={PAYROLL_ROLE_LABELS[row.payroll_role]} />
                      ) : (
                        <Label className="grid gap-1">
                          <span className="text-xs font-medium uppercase text-muted-foreground">
                            Роль
                          </span>
                          <Select
                            disabled={isPending || rowRoleOptions.length === 0}
                            onValueChange={(value) => selectRole(row, value as PayrollRole)}
                            value={row.payroll_role}
                          >
                            <SelectTrigger>
                              <SelectValue placeholder="Выберите роль" />
                            </SelectTrigger>
                            <SelectContent>
                              {rowRoleOptions.map((role) => (
                                <SelectItem value={role} key={role}>
                                  {PAYROLL_ROLE_LABELS[role]}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </Label>
                      )}

                      {row.payroll_role && rowCategories.length === 1 ? (
                        <StaticField
                          label="Категория"
                          value={EMPLOYEE_CATEGORY_LABELS[rowCategories[0]]}
                        />
                      ) : (
                        <Label className="grid gap-1">
                          <span className="text-xs font-medium uppercase text-muted-foreground">
                            Категория
                          </span>
                          <Select
                            disabled={isPending || !row.payroll_role || rowCategories.length === 0}
                            onValueChange={(value) =>
                              updateRoleRow(row.id, { category: value as EmployeeCategory })
                            }
                            value={row.category}
                          >
                            <SelectTrigger>
                              <SelectValue placeholder="Выберите категорию" />
                            </SelectTrigger>
                            <SelectContent>
                              {rowCategories.map((category) => (
                                <SelectItem value={category} key={category}>
                                  {EMPLOYEE_CATEGORY_LABELS[category]}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </Label>
                      )}

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

function ChangeEmployeePositionDialog({
  currentPosition,
  employeeName,
  isPending,
  onOpenChange,
  onSubmit,
  open,
}: {
  currentPosition: string | null;
  employeeName: string;
  isPending: boolean;
  onOpenChange: (open: boolean) => void;
  onSubmit: (payload: {
    position: CanonicalPosition;
    effective_from: string;
    comment?: string;
  }) => void;
  open: boolean;
}) {
  const today = todayDateInputValue();
  const currentCanonical = canonicalPosition(currentPosition);
  const [position, setPosition] = useState<CanonicalPosition | "">("");
  const [effectiveFrom, setEffectiveFrom] = useState(today);
  const [comment, setComment] = useState("");
  // Перевести можно на любую должность реестра (включая вспомогательные и сисадмина).
  const positionOptions = canonicalPositions;
  const commentRequired = Boolean(effectiveFrom) && effectiveFrom < today;
  const canSubmit =
    Boolean(position) &&
    Boolean(effectiveFrom) &&
    (!commentRequired || comment.trim().length > 0) &&
    !isPending;

  useEffect(() => {
    if (!open) {
      return;
    }
    setPosition(currentCanonical ?? "");
    setEffectiveFrom(todayDateInputValue());
    setComment("");
  }, [currentCanonical, open]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmit || !position) {
      return;
    }
    onSubmit({
      position,
      effective_from: effectiveFrom,
      comment: comment.trim() || undefined,
    });
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Изменить должность</DialogTitle>
          <DialogDescription>{employeeName}</DialogDescription>
        </DialogHeader>

        <form className="grid gap-4" onSubmit={submit}>
          <div className="grid gap-2 rounded-md border bg-muted/30 p-3 text-sm">
            <InfoRow label="Сейчас" value={currentPosition || "Должность не указана"} />
          </div>

          <Label className="grid gap-2">
            <span>Новая должность</span>
            <Select
              disabled={isPending}
              onValueChange={(value) => setPosition(value as CanonicalPosition)}
              value={position || undefined}
            >
              <SelectTrigger>
                <SelectValue placeholder="Выберите должность" />
              </SelectTrigger>
              <SelectContent>
                {positionOptions.map((item) => (
                  <SelectItem value={item} key={item}>
                    {item}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Label>

          <Label className="grid gap-2">
            <span>Дата изменения</span>
            <Input
              disabled={isPending}
              onChange={(event) => setEffectiveFrom(event.target.value)}
              type="date"
              value={effectiveFrom}
            />
          </Label>

          <Label className="grid gap-2">
            <span>{commentRequired ? "Комментарий" : "Комментарий (опционально)"}</span>
            <Textarea
              disabled={isPending}
              maxLength={500}
              onChange={(event) => setComment(event.target.value)}
              placeholder={commentRequired ? "Обязателен для задней даты" : ""}
              value={comment}
            />
          </Label>

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
                <Save size={16} aria-hidden="true" />
              )}
              Применить
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function PositionHistorySection({
  assignments,
  canEdit,
  isError,
  isLoading,
  onDelete,
  onEdit,
}: {
  assignments: EmployeePositionAssignment[];
  canEdit: boolean;
  isError: boolean;
  isLoading: boolean;
  onDelete: (assignment: EmployeePositionAssignment) => void;
  onEdit: (assignment: EmployeePositionAssignment) => void;
}) {
  return (
    <div className="grid gap-3 rounded-lg border bg-card p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="text-sm font-medium">История должностей</div>
        {isLoading ? (
          <LoaderCircle className="animate-spin text-muted-foreground" size={16} aria-hidden="true" />
        ) : null}
      </div>
      {isError ? (
        <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          Не удалось загрузить историю должностей
        </div>
      ) : null}
      {!isLoading && assignments.length === 0 && !isError ? (
        <div className="rounded-md border bg-muted/30 px-3 py-2 text-sm text-muted-foreground">
          История должностей пуста
        </div>
      ) : null}
      {assignments.length > 0 ? (
        <div className="grid gap-2">
          {assignments.map((assignment) => (
            <div
              className="grid gap-2 rounded-md border bg-background p-3 sm:grid-cols-[1fr_auto] sm:items-center"
              key={assignment.id}
            >
              <div className="min-w-0 text-sm">
                <div className="font-medium">{assignment.position}</div>
                <div className="mt-1 text-muted-foreground">
                  {formatDate(assignment.effective_from)} -{" "}
                  {assignment.effective_to ? formatDate(assignment.effective_to) : "сейчас"}
                </div>
                {assignment.comment ? (
                  <div className="mt-1 line-clamp-2 text-xs text-muted-foreground">
                    {assignment.comment}
                  </div>
                ) : null}
              </div>
              {canEdit ? (
                <div className="flex gap-1">
                  <Button
                    onClick={() => onEdit(assignment)}
                    size="icon"
                    title="Исправить интервал"
                    type="button"
                    variant="ghost"
                  >
                    <Pencil size={15} aria-hidden="true" />
                  </Button>
                  <Button
                    onClick={() => onDelete(assignment)}
                    size="icon"
                    title="Удалить интервал"
                    type="button"
                    variant="ghost"
                  >
                    <Trash2 size={15} aria-hidden="true" />
                  </Button>
                </div>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function CashierRoleEditor({
  category,
  disabled,
  onCategoryChange,
}: {
  category: EmployeeCategory | "";
  disabled: boolean;
  onCategoryChange: (category: EmployeeCategory) => void;
}) {
  return (
    <div className="grid gap-3 rounded-lg border bg-card p-4">
      <div className="text-sm font-medium">Роль и категория</div>
      <div className="grid gap-3 sm:grid-cols-2">
        <StaticField label="Роль" value={PAYROLL_ROLE_LABELS.administrator} />
        <Label className="grid gap-1">
          <span className="text-xs font-medium uppercase text-muted-foreground">Категория</span>
          <Select
            disabled={disabled}
            onValueChange={(value) => onCategoryChange(value as EmployeeCategory)}
            value={category}
          >
            <SelectTrigger>
              <SelectValue placeholder="Выберите категорию" />
            </SelectTrigger>
            <SelectContent>
              {categoriesForPayrollRole("administrator").map((value) => (
                <SelectItem value={value} key={value}>
                  {EMPLOYEE_CATEGORY_LABELS[value]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Label>
      </div>
    </div>
  );
}

function StaffEditor({
  employee,
  onClose,
  onDirtyChange,
  onNavigate,
  onPendingClick,
  onShowChanges,
}: {
  employee: Employee;
  onClose: () => void;
  onDirtyChange: (dirty: boolean) => void;
  onNavigate?: (path: string) => void;
  onPendingClick: (assignment: EmployeeRoleAssignment) => void;
  onShowChanges: (employeeId: string) => void;
}) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const canEditEmployee = canEditStaffEmployee(employee, permissions);
  const canViewChangeHistory = canReadStaffEmployeeHistory(employee, permissions);
  const [initialDraft, setInitialDraft] = useState<StaffEditorDraft>(() => toEditorDraft(employee));
  const [draft, setDraft] = useState<StaffEditorDraft>(() => toEditorDraft(employee));
  const [dismissOpen, setDismissOpen] = useState(false);
  const [dismissFireDate, setDismissFireDate] = useState(() => todayDateInputValue());
  const [dismissReasonKey, setDismissReasonKey] = useState("");
  const [dismissComment, setDismissComment] = useState("");
  const [dismissDepositAction, setDismissDepositAction] = useState<DepositDismissAction>("none");
  const [dismissDepositAmount, setDismissDepositAmount] = useState("");
  const [dismissDepositComment, setDismissDepositComment] = useState("");
  const [dismissConfirmOpen, setDismissConfirmOpen] = useState(false);
  const [pinOpen, setPinOpen] = useState(false);
  const [hireDateOpen, setHireDateOpen] = useState(false);
  const [hireDateValue, setHireDateValue] = useState("");
  const [hireDateComment, setHireDateComment] = useState("");
  const [hireDateConfirmOpen, setHireDateConfirmOpen] = useState(false);
  const [positionDialogOpen, setPositionDialogOpen] = useState(false);
  const [positionAssignmentEdit, setPositionAssignmentEdit] =
    useState<PositionAssignmentEditState>(null);
  const [positionAssignmentDelete, setPositionAssignmentDelete] =
    useState<PositionAssignmentDeleteState>(null);
  const [closedPeriodAction, setClosedPeriodAction] =
    useState<PendingClosedPeriodAction | null>(null);
  const [closedPeriodAcknowledged, setClosedPeriodAcknowledged] = useState(false);
  const [premiumConfirmOpen, setPremiumConfirmOpen] = useState(false);
  const [premiumConfirmActions, setPremiumConfirmActions] = useState<string[]>([]);
  const [pendingDraftPatch, setPendingDraftPatch] = useState<EmployeePatch | null>(null);
  const [premiumEffectiveDate, setPremiumEffectiveDate] = useState(todayDateInputValue);
  const [premiumComment, setPremiumComment] = useState("");
  const [categoryChange, setCategoryChange] = useState<CategoryEffectiveChange | null>(null);
  const [categoryEffectiveDate, setCategoryEffectiveDate] = useState(todayDateInputValue);
  const [categoryComment, setCategoryComment] = useState("");
  const [premiumTransferConflict, setPremiumTransferConflict] =
    useState<PremiumTransferConflict | null>(null);
  const [substituteDialog, setSubstituteDialog] =
    useState<SubstituteRoleDialogState>(null);
  const [substituteDeleteTarget, setSubstituteDeleteTarget] =
    useState<EmployeeRoleAssignment | null>(null);

  useEffect(() => {
    const nextDraft = toEditorDraft(employee);
    setInitialDraft(nextDraft);
    setDraft(nextDraft);
    setDismissFireDate(employee.fire_date ?? todayDateInputValue());
    setDismissReasonKey("");
    setDismissComment("");
    setDismissDepositAction("none");
    setDismissDepositAmount("");
    setDismissDepositComment("");
    setDismissConfirmOpen(false);
    setPinOpen(false);
    setHireDateOpen(false);
    setHireDateValue("");
    setHireDateComment("");
    setHireDateConfirmOpen(false);
    setPositionDialogOpen(false);
    setPositionAssignmentEdit(null);
    setPositionAssignmentDelete(null);
    setClosedPeriodAction(null);
    setClosedPeriodAcknowledged(false);
    setPremiumConfirmOpen(false);
    setPendingDraftPatch(null);
    setPremiumEffectiveDate(todayDateInputValue());
    setPremiumComment("");
    setCategoryChange(null);
    setCategoryEffectiveDate(todayDateInputValue());
    setCategoryComment("");
    setPremiumConfirmActions([]);
    setPremiumTransferConflict(null);
    setSubstituteDialog(null);
    setSubstituteDeleteTarget(null);
  }, [employee.id]);

  const mutation = useMutation({
    mutationFn: (patch: EmployeePatch) => patchEmployee(employee.id, patch),
    onSuccess: (updatedEmployee, patch) => {
      const nextDraft = toEditorDraft(updatedEmployee);
      setInitialDraft(nextDraft);
      setDraft(nextDraft);
      setPinOpen(false);
      setPremiumTransferConflict(null);
      toast.success(patch.transfer_from_existing ? "Надбавка перенесена" : "Изменения сохранены");
      void queryClient.invalidateQueries({ queryKey: ["employees"] });
      void queryClient.invalidateQueries({ queryKey: ["employees-roster"] });
      void queryClient.invalidateQueries({
        queryKey: ["employees", updatedEmployee.id, "assignments"],
      });
      void queryClient.invalidateQueries({ queryKey: ["employees", "changes"] });
      void queryClient.invalidateQueries({
        queryKey: ["employees", updatedEmployee.id, "changes"],
      });
    },
    onError: (error, patch) => {
      const transferConflict = premiumTransferConflictFromError(error, patch);
      if (transferConflict) {
        setPremiumTransferConflict(transferConflict);
        return;
      }
      toast.error(apiErrorMessage(error, "Не удалось обновить карточку сотрудника"));
    },
  });
  const categoryMutation = useMutation({
    mutationFn: ({
      change,
      comment,
      effectiveFrom,
    }: {
      change: CategoryEffectiveChange;
      comment: string;
      effectiveFrom: string;
    }) =>
      patchEmployeeAssignment(employee.id, change.assignmentId, {
        category: change.nextCategory,
        effective_from: effectiveFrom,
        comment: comment.trim() || undefined,
      }),
    onSuccess: (_assignment, variables) => {
      const today = todayDateInputValue();
      if (variables.effectiveFrom > today) {
        toast.success(
          `Запланировано: с ${formatDate(variables.effectiveFrom)} категория станет ${categoryLabel(
            variables.change.nextCategory,
          )}`,
        );
        setDraft(initialDraft);
      } else if (variables.effectiveFrom < today) {
        toast.success(`Изменено задним числом с ${formatDate(variables.effectiveFrom)}`);
        const nextDraft = applyCategoryChangeToDraft(initialDraft, variables.change);
        setInitialDraft(nextDraft);
        setDraft(nextDraft);
      } else {
        toast.success("Категория изменена");
        const nextDraft = applyCategoryChangeToDraft(initialDraft, variables.change);
        setInitialDraft(nextDraft);
        setDraft(nextDraft);
      }
      setCategoryChange(null);
      setCategoryEffectiveDate(todayDateInputValue());
      setCategoryComment("");
      void queryClient.invalidateQueries({ queryKey: ["employees"] });
      void queryClient.invalidateQueries({ queryKey: ["employees-roster"] });
      void queryClient.invalidateQueries({
        queryKey: ["employees", employee.id, "assignments"],
      });
      void queryClient.invalidateQueries({ queryKey: ["employees", "changes"] });
      void queryClient.invalidateQueries({ queryKey: ["employees", employee.id, "changes"] });
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось изменить категорию"));
    },
  });
  const positionMutation = useMutation({
    mutationFn: (payload: EmployeePositionChangePayload) => changeEmployeePosition(employee.id, payload),
    onSuccess: (assignment) => {
      toast.success("Должность изменена");
      showPayrollWarnings(assignment.warnings);
      setPositionDialogOpen(false);
      setClosedPeriodAction(null);
      setClosedPeriodAcknowledged(false);
      invalidateStaffEmployeeQueries(queryClient, employee.id);
      void queryClient.invalidateQueries({ queryKey: ["payroll-fund"] });
    },
    onError: (error, payload) => {
      const conflict = closedPayrollConflictFromError(error);
      if (conflict && !payload.acknowledge_closed_period) {
        setClosedPeriodAction({ type: "change-position", payload, periods: conflict.periods });
        setClosedPeriodAcknowledged(false);
        return;
      }
      toast.error(apiErrorMessage(error, "Не удалось изменить должность"));
    },
  });
  const positionAssignmentPatchMutation = useMutation({
    mutationFn: ({
      assignmentId,
      payload,
    }: {
      assignmentId: string;
      payload: EmployeePositionAssignmentPatch;
    }) => patchEmployeePositionAssignment(employee.id, assignmentId, payload),
    onSuccess: (assignment) => {
      toast.success("Интервал должности изменён");
      showPayrollWarnings(assignment.warnings);
      setPositionAssignmentEdit(null);
      setClosedPeriodAction(null);
      setClosedPeriodAcknowledged(false);
      invalidateStaffEmployeeQueries(queryClient, employee.id);
      void queryClient.invalidateQueries({ queryKey: ["payroll-fund"] });
    },
    onError: (error, variables) => {
      const conflict = closedPayrollConflictFromError(error);
      if (conflict && !variables.payload.acknowledge_closed_period) {
        setClosedPeriodAction({
          type: "patch-assignment",
          assignmentId: variables.assignmentId,
          payload: variables.payload,
          periods: conflict.periods,
        });
        setClosedPeriodAcknowledged(false);
        return;
      }
      toast.error(apiErrorMessage(error, "Не удалось изменить интервал должности"));
    },
  });
  const positionAssignmentDeleteMutation = useMutation({
    mutationFn: ({
      assignmentId,
      payload,
    }: {
      assignmentId: string;
      payload: EmployeePositionAssignmentDeletePayload;
    }) => deleteEmployeePositionAssignment(employee.id, assignmentId, payload),
    onSuccess: (result) => {
      toast.success("Интервал должности удалён");
      showPayrollWarnings(result.warnings);
      setPositionAssignmentDelete(null);
      setClosedPeriodAction(null);
      setClosedPeriodAcknowledged(false);
      invalidateStaffEmployeeQueries(queryClient, employee.id);
      void queryClient.invalidateQueries({ queryKey: ["payroll-fund"] });
    },
    onError: (error, variables) => {
      const conflict = closedPayrollConflictFromError(error);
      if (conflict && !variables.payload.acknowledge_closed_period) {
        setClosedPeriodAction({
          type: "delete-assignment",
          assignmentId: variables.assignmentId,
          payload: variables.payload,
          periods: conflict.periods,
        });
        setClosedPeriodAcknowledged(false);
        return;
      }
      toast.error(apiErrorMessage(error, "Не удалось удалить интервал должности"));
    },
  });
  const dismissalReasonsQuery = useQuery({
    queryKey: ["employees", "dismissal-reasons"],
    queryFn: () => getEmployeeDismissalReasons(),
    enabled: dismissOpen,
  });
  const employeeChangesQuery = useQuery({
    queryKey: ["employees", employee.id, "changes", "latest"],
    queryFn: () => getEmployeeChanges({ employeeId: employee.id }),
    enabled: canViewChangeHistory,
  });
  const positionHistoryQuery = useQuery({
    queryKey: ["employees", employee.id, "position-history"],
    queryFn: () => getEmployeePositionHistory(employee.id),
    enabled: canViewChangeHistory || canEditEmployee,
  });
  const substitutePairsQuery = useQuery({
    queryKey: ["substitute-pairs"],
    queryFn: getSubstitutePairs,
  });
  const depositsQuery = useQuery({
    queryKey: ["deposits"],
    queryFn: getDeposits,
    enabled: isDepositTargetPosition(employee.position),
  });
  const depositSettingsQuery = useQuery({
    queryKey: ["settings", "deposits"],
    queryFn: () => getSettings(),
    enabled: isDepositTargetPosition(employee.position),
  });
  const recentAdjustmentsQuery = useQuery({
    queryKey: ["payroll-adjustments", employee.id, "recent"],
    queryFn: () =>
      getPayrollAdjustments({
        employeeId: employee.id,
        dateFrom: dateInputDaysAgo(60),
        dateTo: todayDateInputValue(),
      }),
    enabled: isDepositTargetPosition(employee.position),
  });
  const depositSettings = useMemo(
    () => extractDepositSettings(depositSettingsQuery.data),
    [depositSettingsQuery.data],
  );
  const employeeDeposit = useMemo(
    () => (depositsQuery.data ?? []).find((deposit) => deposit.id === employee.id) ?? null,
    [depositsQuery.data, employee.id],
  );
  const dismissDepositBalance = numericAmount(employeeDeposit?.balance);
  const dismissDepositHasBalance = dismissDepositBalance > 0;
  const noticeDaysToFire =
    employee.active_notice && dismissFireDate
      ? daysBetweenDateStrings(employee.active_notice.notice_date, dismissFireDate)
      : null;
  const noticeTriggersFullPayout = noticeDaysToFire !== null && noticeDaysToFire >= 14;
  const dismissDepositDecision = depositDismissDecision(
    dismissDepositAction,
    dismissDepositBalance,
    dismissDepositAmount,
  );
  const dismissDepositValid =
    !dismissDepositHasBalance ||
    (dismissDepositDecision.isValid && dismissDepositAction !== "none");
  const dismissalReasons = dismissalReasonOptions(dismissalReasonsQuery.data ?? []);
  const selectedDismissalReason =
    dismissalReasons.find((reason) => reason.key === dismissReasonKey) ?? null;
  const dismissCommentRequired = Boolean(selectedDismissalReason?.requires_comment);
  const dismissMutation = useMutation({
    mutationFn: () => {
      const dismissalReason = selectedDismissalReason;
      if (!dismissalReason) {
        throw new Error("Выберите причину увольнения");
      }
      const payload: EmployeeDismissPayload = {
        fire_date: dismissFireDate,
        reason_code: dismissalReason.code,
        comment: dismissComment.trim() || undefined,
        deposit_action: dismissDepositHasBalance ? dismissDepositAction : "none",
        deposit_comment: dismissDepositComment.trim() || undefined,
      };
      if (dismissDepositHasBalance && dismissDepositAction === "payout_partial") {
        payload.deposit_payout_amount = decimalPayload(dismissDepositAmount);
      }
      if (dismissalReason.id) {
        payload.reason_id = dismissalReason.id;
      }
      return dismissEmployee(employee.id, payload);
    },
    onSuccess: () => {
      toast.success("Сотрудник уволен");
      setDismissOpen(false);
      setDismissConfirmOpen(false);
      onClose();
      void queryClient.invalidateQueries({ queryKey: ["employees"] });
      void queryClient.invalidateQueries({ queryKey: ["employees", "changes"] });
      void queryClient.invalidateQueries({ queryKey: ["employees", employee.id, "changes"] });
      void queryClient.invalidateQueries({ queryKey: ["deposits"] });
      void queryClient.invalidateQueries({ queryKey: ["payroll-fund"] });
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
      void queryClient.invalidateQueries({ queryKey: ["employees", "changes"] });
      void queryClient.invalidateQueries({ queryKey: ["employees", employee.id, "changes"] });
      void queryClient.invalidateQueries({ queryKey: ["payroll-fund"] });
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось восстановить сотрудника"));
    },
  });
  const hireDateMutation = useMutation({
    mutationFn: () =>
      setEmployeeHireDate(employee.id, {
        hire_date: hireDateValue,
        comment: hireDateComment.trim() || undefined,
      }),
    onSuccess: (updatedEmployee) => {
      const nextDraft = toEditorDraft(updatedEmployee);
      toast.success("Дата приёма установлена");
      setInitialDraft(nextDraft);
      setDraft(nextDraft);
      setHireDateOpen(false);
      setHireDateConfirmOpen(false);
      setHireDateValue("");
      setHireDateComment("");
      void queryClient.invalidateQueries({ queryKey: ["employees"] });
      void queryClient.invalidateQueries({ queryKey: ["employee", updatedEmployee.id] });
      void queryClient.invalidateQueries({ queryKey: ["employees", "changes"] });
      void queryClient.invalidateQueries({
        queryKey: ["employees", updatedEmployee.id, "changes"],
      });
      void queryClient.invalidateQueries({ queryKey: ["payroll-fund"] });
    },
    onError: (error) => {
      setHireDateConfirmOpen(false);
      if (apiErrorStatus(error) === 409) {
        toast.error("Дата приёма уже недоступна для изменения");
        setHireDateOpen(false);
        setHireDateValue("");
        setHireDateComment("");
        void queryClient.invalidateQueries({ queryKey: ["employees"] });
        void queryClient.invalidateQueries({ queryKey: ["employee", employee.id] });
        return;
      }
      toast.error(apiErrorMessage(error, "Не удалось установить дату приёма"));
    },
  });
  const substituteSaveMutation = useMutation({
    mutationFn: ({
      assignmentId,
      category,
      comment,
      effectiveFrom,
      payrollRole,
    }: {
      assignmentId?: string;
      payrollRole: PayrollRole;
      category: EmployeeCategory;
      effectiveFrom: string;
      comment: string;
    }) => {
      const reason = comment.trim() ? comment.trim() : undefined;
      if (assignmentId) {
        return patchEmployeeAssignment(employee.id, assignmentId, {
          payroll_role: payrollRole,
          category,
          is_primary: false,
          is_substitute: true,
          effective_from: effectiveFrom,
          comment: reason,
        });
      }
      return createEmployeeAssignment(employee.id, {
        payroll_role: payrollRole,
        category,
        is_primary: false,
        is_substitute: true,
        effective_from: effectiveFrom,
        comment: reason,
      });
    },
    onSuccess: () => {
      toast.success("Подменная роль сохранена");
      setSubstituteDialog(null);
      invalidateStaffEmployeeQueries(queryClient, employee.id);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось сохранить подменную роль"));
    },
  });
  const substituteDeleteMutation = useMutation({
    mutationFn: (assignmentId: string) => deleteEmployeeAssignment(employee.id, assignmentId),
    onSuccess: () => {
      toast.success("Подменная роль удалена");
      setSubstituteDeleteTarget(null);
      invalidateStaffEmployeeQueries(queryClient, employee.id);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось удалить подменную роль"));
    },
  });
  const roleReviewIgnoreMutation = useMutation({
    mutationFn: () => patchEmployee(employee.id, { requires_role_review: false }),
    onSuccess: () => {
      toast.success("Уточнение скрыто");
      invalidateStaffEmployeeQueries(queryClient, employee.id);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось скрыть уточнение"));
    },
  });

  const trimmedDraftName = draft.full_name.trim();
  const nameIsValid = trimmedDraftName.length > 0;
  const dirty =
    JSON.stringify(editorDraftSnapshot(draft)) !==
    JSON.stringify(editorDraftSnapshot(initialDraft));
  const assignments = draft.assignments;
  const cashierAssignment = assignments.find(
    (assignment) => assignment.payroll_role === "administrator",
  );
  const cashierCategoryValue = cashierAssignment?.category ?? "";
  const activeRoleIds = new Set(assignments.map((assignment) => assignment.payroll_role));
  const roleOptions = payrollRolesForPosition(draft.position);
  const editorPosition = canonicalPosition(draft.position);
  const positionUnsaved =
    canonicalPosition(draft.position) !== canonicalPosition(initialDraft.position);
  const substituteAddDisabledReason = positionUnsaved ? "Сначала сохраните должность" : null;
  const editorPremiumOptions = editorPosition ? premiumApplicability[editorPosition] : null;
  const editorRequiresPin = positionRequiresPin(draft.position);
  const showEditorCashierRole = editorPosition === "Кассир";
  const showEditorCookRoles = editorPosition === "Повар";
  const substitutePairs = substitutePairsQuery.data?.pairs ?? [];
  const employeeSubstitutePairs = substitutePairs.filter(
    (pair) => canonicalPosition(pair.from_position) === editorPosition,
  );
  const employeeSubstitutes = visibleSubstituteAssignments(employee);
  const showSubstituteSection =
    employeeSubstitutePairs.length > 0 ||
    employeeSubstitutes.length > 0 ||
    employee.requires_role_review;
  const roleValidation = validateEditorRoles(draft);
  const pinTouched = pinDraftTouched(draft);
  const pinState = employeePinState(employee);
  const pinFormatValid = /^\d{4}$/.test(draft.pin_code);
  const pinMatches = draft.pin_code === draft.pin_confirmation;
  const pinIsValid = !pinTouched || (pinFormatValid && pinMatches);
  const canSaveDraft =
    canEditEmployee &&
    dirty &&
    nameIsValid &&
    !roleValidation &&
    pinIsValid &&
    !mutation.isPending &&
    !categoryMutation.isPending;
  const canSubmitDismiss =
    Boolean(dismissFireDate) &&
    Boolean(selectedDismissalReason) &&
    (!dismissCommentRequired || dismissComment.trim().length > 0) &&
    dismissDepositValid &&
    (!isDepositTargetPosition(employee.position) || !depositsQuery.isLoading) &&
    !dismissalReasonsQuery.isLoading &&
    !dismissMutation.isPending;
  const canDismiss = canDismissStaffEmployee(employee, permissions);
  const canReinstate = canReinstateStaffEmployee(employee, permissions);
  const canDismissStatus = employee.status === "active" || employee.status === "requires_setup";
  const canManageHireDate = canEditEmployee && employee.status !== "inactive";
  const hireDateMin = dateInputYearsAgo(10);
  const hireDateMax = todayDateInputValue();
  const hireDateIsValid =
    Boolean(hireDateValue) && hireDateValue >= hireDateMin && hireDateValue <= hireDateMax;
  const hireDateCanSubmit = hireDateIsValid && hireDateValue !== (employee.hire_date ?? "");
  const selectedCategoryEffectiveDate = categoryEffectiveDate;
  const categoryCommentRequired =
    Boolean(selectedCategoryEffectiveDate) &&
    selectedCategoryEffectiveDate < todayDateInputValue();
  const canApplyCategoryChange =
    Boolean(categoryChange) &&
    Boolean(selectedCategoryEffectiveDate) &&
    (!categoryCommentRequired || categoryComment.trim().length > 0) &&
    !categoryMutation.isPending;
  const positionAssignmentEditEffectiveFrom = positionAssignmentEdit?.effectiveFrom ?? "";
  const positionAssignmentEditCommentRequired =
    Boolean(positionAssignmentEditEffectiveFrom) &&
    positionAssignmentEditEffectiveFrom < todayDateInputValue();
  const canApplyPositionAssignmentEdit =
    Boolean(positionAssignmentEdit?.position) &&
    Boolean(positionAssignmentEditEffectiveFrom) &&
    (!positionAssignmentEditCommentRequired ||
      Boolean(positionAssignmentEdit?.comment.trim())) &&
    !positionAssignmentPatchMutation.isPending;
  const canDeletePositionAssignment =
    Boolean(positionAssignmentDelete?.comment.trim()) &&
    !positionAssignmentDeleteMutation.isPending;
  const closedPeriodActionPending =
    positionMutation.isPending ||
    positionAssignmentPatchMutation.isPending ||
    positionAssignmentDeleteMutation.isPending;
  const premiumCommentRequired =
    Boolean(premiumEffectiveDate) && premiumEffectiveDate < todayDateInputValue();
  const canApplyPremiumChange =
    Boolean(pendingDraftPatch) &&
    Boolean(premiumEffectiveDate) &&
    (!premiumCommentRequired || premiumComment.trim().length > 0) &&
    !mutation.isPending;

  useEffect(() => {
    onDirtyChange(dirty);
  }, [dirty, onDirtyChange]);

  useEffect(() => () => onDirtyChange(false), [onDirtyChange]);

  useEffect(() => {
    if (!dismissOpen) {
      return;
    }
    if (!dismissDepositHasBalance) {
      setDismissDepositAction("none");
      setDismissDepositAmount("");
      return;
    }
    setDismissDepositAction(noticeTriggersFullPayout ? "payout_full" : "write_off");
    setDismissDepositAmount("");
  }, [dismissOpen, dismissDepositHasBalance, employee.id, noticeTriggersFullPayout]);

  function addRole() {
    const payrollRole = roleOptions.find((role) => !activeRoleIds.has(role));
    if (!payrollRole) {
      toast.error("Все подходящие роли уже добавлены");
      return;
    }
    const categories = categoriesForPayrollRole(payrollRole);
    const category = categories[0];
    if (!category) {
      toast.error("Для роли нет доступных категорий");
      return;
    }
    setDraft((current) => ({
      ...current,
      assignments: ensurePrimaryAssignment([
        ...current.assignments,
        newAssignmentDraft(payrollRole, category, current.assignments.length === 0),
      ]),
    }));
  }

  function saveDraft() {
    if (!canSaveDraft) {
      return;
    }
    const patch = buildEmployeePatch(initialDraft, draft);
    if (Object.keys(patch).length === 0) {
      return;
    }
    const effectiveCategoryChange = categoryEffectiveChangeForDraft(initialDraft, draft, patch);
    if (hasExistingCategoryChange(initialDraft, draft) && !effectiveCategoryChange) {
      toast.error("Сохраните изменение категории отдельно от других изменений");
      return;
    }
    if (effectiveCategoryChange) {
      setCategoryChange(effectiveCategoryChange);
      setCategoryEffectiveDate(todayDateInputValue());
      setCategoryComment("");
      return;
    }
    const premiumActions = premiumChangeConfirmations(initialDraft, draft);
    if (premiumActions.length > 0) {
      setPremiumConfirmActions(premiumActions);
      setPendingDraftPatch(patch);
      setPremiumEffectiveDate(todayDateInputValue());
      setPremiumComment("");
      setPremiumConfirmOpen(true);
      return;
    }
    mutation.mutate(patch);
  }

  function requestHireDateConfirmation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!hireDateIsValid) {
      toast.error("Выберите дату приёма в допустимом диапазоне");
      return;
    }
    if (hireDateValue === (employee.hire_date ?? "")) {
      toast.error("Выберите новую дату приёма");
      return;
    }
    setHireDateConfirmOpen(true);
  }

  function openHireDateDialog() {
    setHireDateValue(employee.hire_date ?? "");
    setHireDateComment("");
    setHireDateConfirmOpen(false);
    setHireDateOpen(true);
  }

  function openPositionAssignmentEdit(assignment: EmployeePositionAssignment) {
    setPositionAssignmentEdit({
      assignment,
      position: canonicalPosition(assignment.position) ?? "",
      effectiveFrom: assignment.effective_from,
      comment: "",
    });
  }

  function submitPositionAssignmentEdit() {
    if (!positionAssignmentEdit || !canApplyPositionAssignmentEdit) {
      return;
    }
    positionAssignmentPatchMutation.mutate({
      assignmentId: positionAssignmentEdit.assignment.id,
      payload: {
        position: positionAssignmentEdit.position || undefined,
        effective_from: positionAssignmentEdit.effectiveFrom,
        comment: positionAssignmentEdit.comment.trim() || undefined,
      },
    });
  }

  function submitPositionAssignmentDelete() {
    if (!positionAssignmentDelete || !canDeletePositionAssignment) {
      return;
    }
    positionAssignmentDeleteMutation.mutate({
      assignmentId: positionAssignmentDelete.assignment.id,
      payload: {
        comment: positionAssignmentDelete.comment.trim(),
      },
    });
  }

  function confirmClosedPeriodAction() {
    if (!closedPeriodAction || !closedPeriodAcknowledged) {
      return;
    }
    if (closedPeriodAction.type === "change-position") {
      positionMutation.mutate({
        ...closedPeriodAction.payload,
        acknowledge_closed_period: true,
      });
      return;
    }
    if (closedPeriodAction.type === "patch-assignment") {
      positionAssignmentPatchMutation.mutate({
        assignmentId: closedPeriodAction.assignmentId,
        payload: {
          ...closedPeriodAction.payload,
          acknowledge_closed_period: true,
        },
      });
      return;
    }
    positionAssignmentDeleteMutation.mutate({
      assignmentId: closedPeriodAction.assignmentId,
      payload: {
        ...closedPeriodAction.payload,
        acknowledge_closed_period: true,
      },
    });
  }

  return (
    <div className="space-y-5">
      <SheetHeader>
        <div className="flex items-start justify-between gap-3 pr-8">
          <div>
            <SheetTitle>Карточка сотрудника</SheetTitle>
            <SheetDescription>
              Поля реестра, которые используются графиком и зарплатой.
            </SheetDescription>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Button disabled={mutation.isPending} onClick={onClose} type="button" variant="outline">
              Отмена
            </Button>
            {canEditEmployee ? (
            <Button disabled={!canSaveDraft} onClick={saveDraft} type="button">
              {mutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <Save size={16} aria-hidden="true" />
              )}
              Сохранить
            </Button>
            ) : null}
          </div>
        </div>
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
            <EmployeeTags employee={employee} onPendingClick={onPendingClick} />
          </div>
        </div>
      </div>

      {employee.requires_role_review ? (
        <RoleReviewBanner
          addDisabledReason={substituteAddDisabledReason}
          canEdit={canEditEmployee}
          employee={employee}
          onAddSubstitute={(targetPosition) => setSubstituteDialog({ targetPosition })}
          onIgnore={() => roleReviewIgnoreMutation.mutate()}
          onOpenSettings={() => onNavigate?.("/settings")}
          pairs={employeeSubstitutePairs}
          isIgnoring={roleReviewIgnoreMutation.isPending}
        />
      ) : null}

      {employee.requires_position_review ? (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-950">
          <div className="flex min-w-0 items-center gap-2">
            <CircleAlert size={16} aria-hidden="true" />
            <span>iiko предлагает другую должность для этого сотрудника.</span>
          </div>
          <Button
            className="border-amber-300 bg-background text-amber-950 hover:bg-amber-100"
            onClick={() => setPositionDialogOpen(true)}
            size="sm"
            type="button"
            variant="outline"
          >
            <Pencil size={15} aria-hidden="true" />
            Применить через диалог
          </Button>
        </div>
      ) : null}

      {employee.hire_date ? (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-muted/30 px-4 py-3 text-sm">
          <div>
            <span className="font-medium">Дата приёма:</span> {formatDate(employee.hire_date)}
            {employee.status === "inactive" ? (
              <span className="text-muted-foreground"> (зафиксирована)</span>
            ) : null}
          </div>
          {canManageHireDate ? (
            <Button
              disabled={hireDateMutation.isPending}
              onClick={openHireDateDialog}
              size="sm"
              type="button"
              variant="outline"
            >
              <CalendarPlus size={15} aria-hidden="true" />
              Изменить
            </Button>
          ) : null}
        </div>
      ) : (
        <div className="grid gap-3 rounded-lg border border-amber-200 bg-amber-50 p-4 text-amber-950">
          <div className="flex items-center gap-2 font-medium">
            <CircleAlert size={16} aria-hidden="true" />
            Дата приёма не указана
          </div>
          <p className="text-sm text-amber-900">
            Без даты приёма стаж считается как 0 и накопительный фонд не начисляется.
          </p>
          {canManageHireDate ? (
            <Button
              className="w-fit border-amber-300 bg-background text-amber-950 hover:bg-amber-100"
              disabled={hireDateMutation.isPending}
              onClick={openHireDateDialog}
              type="button"
              variant="outline"
            >
              <CalendarPlus size={16} aria-hidden="true" />
              Указать дату приёма
            </Button>
          ) : null}
        </div>
      )}

      <div className="grid gap-4">
        <Label className="grid gap-2">
          <span>Имя</span>
          <Input
            autoComplete="off"
            disabled={!canEditEmployee || mutation.isPending}
            onChange={(event) => setDraft({ ...draft, full_name: event.target.value })}
            value={draft.full_name}
          />
          {!nameIsValid ? <span className="text-xs text-destructive">ФИО обязательно</span> : null}
        </Label>

        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-card px-4 py-3">
          <div>
            <div className="text-xs font-medium uppercase text-muted-foreground">Должность</div>
            <div className="mt-1 text-sm font-medium">
              {employee.position || "Должность не указана"}
            </div>
          </div>
          {canEditEmployee ? (
            <Button
              disabled={positionMutation.isPending}
              onClick={() => setPositionDialogOpen(true)}
              type="button"
              variant="outline"
            >
              <Pencil size={15} aria-hidden="true" />
              Изменить должность
            </Button>
          ) : null}
        </div>

        {canViewChangeHistory || canEditEmployee ? (
          <PositionHistorySection
            assignments={positionHistoryQuery.data ?? []}
            canEdit={canEditEmployee}
            isError={positionHistoryQuery.isError}
            isLoading={positionHistoryQuery.isLoading}
            onDelete={(assignment) => setPositionAssignmentDelete({ assignment, comment: "" })}
            onEdit={openPositionAssignmentEdit}
          />
        ) : null}

        {showEditorCashierRole ? (
          <CashierRoleEditor
            category={cashierCategoryValue}
            disabled={!canEditEmployee || mutation.isPending}
            onCategoryChange={(category) => {
              setDraft((current) => ({
                ...current,
                assignments: setCashierAssignmentCategory(current.assignments, category),
              }));
            }}
          />
        ) : null}

        {showEditorCookRoles ? (
          <div className="grid gap-3 rounded-lg border bg-card p-4">
            <div className="flex items-center justify-between gap-3">
              <div className="text-sm font-medium">Роли и категории</div>
              <Button
                disabled={
                  !canEditEmployee ||
                  mutation.isPending ||
                  roleOptions.every((role) => activeRoleIds.has(role))
                }
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
                  const rowCategories = categoriesForPayrollRole(assignment.payroll_role);
                  const canChooseRole = roleOptionsForAssignment.length > 1;
                  return (
                    <div
                      className="grid gap-2 rounded-md border bg-background p-3 sm:grid-cols-[120px_1fr_1fr_auto] sm:items-center"
                      key={assignment.draft_id}
                    >
                      <label className="flex h-10 items-center gap-2 text-sm">
                        <input
                          checked={assignment.is_primary}
                          disabled={assignment.is_primary || !canEditEmployee || mutation.isPending}
                          name="edit-primary-role"
                          onChange={() =>
                            setDraft((current) => ({
                              ...current,
                              assignments: setPrimaryAssignment(
                                current.assignments,
                                assignment.draft_id,
                              ),
                            }))
                          }
                          type="radio"
                        />
                        <span>Основная</span>
                      </label>

                      {canChooseRole ? (
                        <Select
                          disabled={!canEditEmployee || mutation.isPending}
                          onValueChange={(value) => {
                            const payrollRole = value as PayrollRole;
                            const nextCategories = categoriesForPayrollRole(payrollRole);
                            const category = nextCategories.includes(assignment.category)
                              ? assignment.category
                              : nextCategories[0];
                            if (!category) {
                              toast.error("Для роли нет доступных категорий");
                              return;
                            }
                            setDraft((current) => ({
                              ...current,
                              assignments: updateAssignmentDraft(
                                current.assignments,
                                assignment.draft_id,
                                { payroll_role: payrollRole, category },
                              ),
                            }));
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
                      ) : (
                        <StaticField
                          label="Роль"
                          value={PAYROLL_ROLE_LABELS[assignment.payroll_role]}
                        />
                      )}

                      {rowCategories.length === 1 ? (
                        <StaticField
                          label="Категория"
                          value={EMPLOYEE_CATEGORY_LABELS[rowCategories[0]]}
                        />
                      ) : (
                        <Select
                          disabled={!canEditEmployee || mutation.isPending || rowCategories.length === 0}
                          onValueChange={(value) =>
                            setDraft((current) => ({
                              ...current,
                              assignments: updateAssignmentDraft(
                                current.assignments,
                                assignment.draft_id,
                                { category: value as EmployeeCategory },
                              ),
                            }))
                          }
                          value={assignment.category}
                        >
                          <SelectTrigger>
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            {rowCategories.map((value) => (
                              <SelectItem value={value} key={value}>
                                {EMPLOYEE_CATEGORY_LABELS[value]}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      )}

                      <Button
                        disabled={assignment.is_primary || !canEditEmployee || mutation.isPending}
                        onClick={() =>
                          setDraft((current) => ({
                            ...current,
                            assignments: removeAssignmentDraft(
                              current.assignments,
                              assignment.draft_id,
                            ),
                          }))
                        }
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

        {roleValidation ? (
          <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {roleValidation}
          </div>
        ) : null}

        {showSubstituteSection ? (
          <SubstituteRolesSection
            addDisabledReason={substituteAddDisabledReason}
            assignments={employeeSubstitutes}
            canEdit={canEditEmployee}
            onAdd={(targetPosition) => setSubstituteDialog({ targetPosition })}
            onDelete={(assignment) => setSubstituteDeleteTarget(assignment)}
            onEdit={(assignment) => setSubstituteDialog({ assignment })}
            pairs={employeeSubstitutePairs}
          />
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
                  disabled={!canEditEmployee || mutation.isPending}
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
                  disabled={!canEditEmployee || mutation.isPending}
                  onChange={(event) =>
                    setDraft({ ...draft, is_deputy_senior: event.target.checked })
                  }
                  type="checkbox"
                />
              </label>
            ) : null}
          </div>
        ) : null}

        <EmployeeDepositSection
          deposit={employeeDeposit}
          employee={employee}
          isLoading={depositsQuery.isLoading || depositSettingsQuery.isLoading}
          rules={depositSettings.rules}
        />

        {isDepositTargetPosition(employee.position) ? (
          <EmployeeFundSection employee={employee} />
        ) : null}

        {isDepositTargetPosition(employee.position) ? (
          <EmployeeAdjustmentsPreview
            adjustments={(recentAdjustmentsQuery.data ?? []).slice(0, 10)}
            employeeId={employee.id}
            isError={recentAdjustmentsQuery.isError}
            isLoading={recentAdjustmentsQuery.isLoading}
            onNavigate={onNavigate}
          />
        ) : null}

        {editorRequiresPin ? (
          <div className="grid gap-3 rounded-lg border bg-card p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="text-sm font-medium">ПИН-код смены</div>
              <EmployeePinBadge employee={employee} />
            </div>

            {pinTouched ? (
              <Badge className="w-fit rounded-md border-emerald-200 bg-emerald-50 text-emerald-700 shadow-none">
                ПИН будет обновлён после сохранения
              </Badge>
            ) : null}

            {pinOpen ? (
              <div className="grid gap-3 sm:grid-cols-2">
                <Label className="grid gap-2">
                  <span>ПИН</span>
                  <Input
                    autoComplete="off"
                    disabled={!canEditEmployee || mutation.isPending}
                    inputMode="numeric"
                    maxLength={4}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        pin_code: sanitizePinInput(event.target.value),
                      }))
                    }
                    placeholder="0000"
                    type="password"
                    value={draft.pin_code}
                  />
                </Label>
                <Label className="grid gap-2">
                  <span>Подтвердите ПИН</span>
                  <Input
                    autoComplete="off"
                    disabled={!canEditEmployee || mutation.isPending}
                    inputMode="numeric"
                    maxLength={4}
                    onChange={(event) =>
                      setDraft((current) => ({
                        ...current,
                        pin_confirmation: sanitizePinInput(event.target.value),
                      }))
                    }
                    placeholder="0000"
                    type="password"
                    value={draft.pin_confirmation}
                  />
                </Label>
                {pinTouched && !pinFormatValid ? (
                  <span className="text-xs text-destructive sm:col-span-2">
                    ПИН-код должен состоять из 4 цифр
                  </span>
                ) : null}
                {pinFormatValid && !pinMatches ? (
                  <span className="text-xs text-destructive sm:col-span-2">
                    ПИН и подтверждение не совпадают
                  </span>
                ) : null}
                <Button
                  className="w-fit sm:col-span-2"
                  onClick={() => {
                    setDraft((current) => ({ ...current, pin_code: "", pin_confirmation: "" }));
                    setPinOpen(false);
                  }}
                  type="button"
                  variant="outline"
                >
                  Отмена
                </Button>
              </div>
            ) : (
              canEditEmployee ? (
              <Button
                className="w-fit"
                onClick={() => setPinOpen(true)}
                type="button"
                variant="outline"
              >
                <KeyRound size={16} aria-hidden="true" />
                {EMPLOYEE_PIN_BUTTON_LABELS[pinState]}
              </Button>
              ) : null
            )}
          </div>
        ) : null}
      </div>

      <div className="grid gap-2 rounded-lg border bg-muted/30 p-4 text-sm">
        {employee.status === "inactive" ? (
          <InfoRow label="Дата увольнения" value={formatDate(employee.fire_date)} />
        ) : null}
        {canViewChangeHistory && employee.status === "inactive" && employee.fire_reason ? (
          <InfoRow label="Причина" value={employee.fire_reason} />
        ) : null}
        {positionRequiresPin(employee.position) ? (
          <InfoRow label="ПИН изменён" value={formatDateTime(employee.pin_set_at)} />
        ) : null}
        <InfoRow label="Синхронизация" value={formatDateTime(employee.iiko_sync_at)} />
        <InfoRow label="Создано в приложении" value={formatDateTime(employee.created_at)} />
        <InfoRow label="Обновлён" value={formatDateTime(employee.updated_at)} />
      </div>

      {canViewChangeHistory ? (
        <EmployeeChangeHistoryPreview
          changes={(employeeChangesQuery.data ?? []).slice(0, 5)}
          isError={employeeChangesQuery.isError}
          isLoading={employeeChangesQuery.isLoading}
          onShowAll={() => onShowChanges(employee.id)}
        />
      ) : null}

      <div className="grid gap-2">
        {canDismiss && canDismissStatus ? (
          <Button
            className="w-full"
            disabled={dismissMutation.isPending}
            onClick={() => {
              setDismissFireDate(todayDateInputValue());
              setDismissReasonKey("");
              setDismissComment("");
              setDismissDepositAction(
                employee.active_notice?.will_trigger_full_payout ? "payout_full" : "write_off",
              );
              setDismissDepositAmount("");
              setDismissDepositComment("");
              setDismissConfirmOpen(false);
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

      <ChangeEmployeePositionDialog
        currentPosition={employee.position}
        employeeName={employee.full_name}
        isPending={positionMutation.isPending}
        onOpenChange={setPositionDialogOpen}
        onSubmit={(payload) => positionMutation.mutate(payload)}
        open={positionDialogOpen}
      />

      <Dialog
        open={Boolean(positionAssignmentEdit)}
        onOpenChange={(open) => {
          if (!open && !positionAssignmentPatchMutation.isPending) {
            setPositionAssignmentEdit(null);
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Исправить интервал</DialogTitle>
            <DialogDescription>{employee.full_name}</DialogDescription>
          </DialogHeader>

          {positionAssignmentEdit ? (
            <div className="grid gap-4">
              <div className="grid gap-2 rounded-md border bg-muted/30 p-3 text-sm">
                <InfoRow
                  label="Интервал"
                  value={`${formatDate(positionAssignmentEdit.assignment.effective_from)} - ${
                    positionAssignmentEdit.assignment.effective_to
                      ? formatDate(positionAssignmentEdit.assignment.effective_to)
                      : "сейчас"
                  }`}
                />
              </div>

              <Label className="grid gap-2">
                <span>Должность</span>
                <Select
                  disabled={positionAssignmentPatchMutation.isPending}
                  onValueChange={(value) =>
                    setPositionAssignmentEdit((current) =>
                      current ? { ...current, position: value as CanonicalPosition } : current,
                    )
                  }
                  value={positionAssignmentEdit.position || undefined}
                >
                  <SelectTrigger>
                    <SelectValue placeholder="Выберите должность" />
                  </SelectTrigger>
                  <SelectContent>
                    {canonicalPositions.map((item) => (
                      <SelectItem value={item} key={item}>
                        {item}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Label>

              <Label className="grid gap-2">
                <span>Дата начала</span>
                <Input
                  disabled={positionAssignmentPatchMutation.isPending}
                  onChange={(event) =>
                    setPositionAssignmentEdit((current) =>
                      current ? { ...current, effectiveFrom: event.target.value } : current,
                    )
                  }
                  type="date"
                  value={positionAssignmentEdit.effectiveFrom}
                />
              </Label>

              <Label className="grid gap-2">
                <span>
                  {positionAssignmentEditCommentRequired
                    ? "Комментарий"
                    : "Комментарий (опционально)"}
                </span>
                <Textarea
                  disabled={positionAssignmentPatchMutation.isPending}
                  maxLength={500}
                  onChange={(event) =>
                    setPositionAssignmentEdit((current) =>
                      current ? { ...current, comment: event.target.value } : current,
                    )
                  }
                  placeholder={
                    positionAssignmentEditCommentRequired ? "Обязателен для задней даты" : ""
                  }
                  value={positionAssignmentEdit.comment}
                />
              </Label>
            </div>
          ) : null}

          <DialogFooter>
            <Button
              disabled={positionAssignmentPatchMutation.isPending}
              onClick={() => setPositionAssignmentEdit(null)}
              type="button"
              variant="outline"
            >
              Отмена
            </Button>
            <Button
              disabled={!canApplyPositionAssignmentEdit}
              onClick={submitPositionAssignmentEdit}
              type="button"
            >
              {positionAssignmentPatchMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <Save size={16} aria-hidden="true" />
              )}
              Применить
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog
        open={Boolean(positionAssignmentDelete)}
        onOpenChange={(open) => {
          if (!open && !positionAssignmentDeleteMutation.isPending) {
            setPositionAssignmentDelete(null);
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Удалить интервал должности?</AlertDialogTitle>
            <AlertDialogDescription>
              {positionAssignmentDelete
                ? `${positionAssignmentDelete.assignment.position}, с ${formatDate(
                    positionAssignmentDelete.assignment.effective_from,
                  )}`
                : ""}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <Label className="grid gap-2 text-left">
            <span>Комментарий</span>
            <Textarea
              disabled={positionAssignmentDeleteMutation.isPending}
              maxLength={500}
              onChange={(event) =>
                setPositionAssignmentDelete((current) =>
                  current ? { ...current, comment: event.target.value } : current,
                )
              }
              value={positionAssignmentDelete?.comment ?? ""}
            />
          </Label>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={positionAssignmentDeleteMutation.isPending}>
              Отмена
            </AlertDialogCancel>
            <AlertDialogAction
              disabled={!canDeletePositionAssignment}
              onClick={(event) => {
                event.preventDefault();
                submitPositionAssignmentDelete();
              }}
            >
              {positionAssignmentDeleteMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <Trash2 size={16} aria-hidden="true" />
              )}
              Удалить
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={Boolean(closedPeriodAction)}
        onOpenChange={(open) => {
          if (!open && !closedPeriodActionPending) {
            setClosedPeriodAction(null);
            setClosedPeriodAcknowledged(false);
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Затронута закрытая неделя</AlertDialogTitle>
            <AlertDialogDescription>
              Изменение можно сохранить только с подтверждением корректировки ведомости.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <div className="grid gap-3 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">
            <div className="font-medium">Периоды</div>
            <div className="grid gap-1">
              {(closedPeriodAction?.periods ?? []).map((period) => (
                <div key={period.id}>
                  {formatDate(period.start_date)} - {formatDate(period.end_date)}
                </div>
              ))}
            </div>
            <label className="flex items-start gap-2">
              <input
                checked={closedPeriodAcknowledged}
                disabled={closedPeriodActionPending}
                onChange={(event) => setClosedPeriodAcknowledged(event.target.checked)}
                type="checkbox"
              />
              <span>Понимаю, потребуется корректировка ведомости</span>
            </label>
          </div>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={closedPeriodActionPending}>Отмена</AlertDialogCancel>
            <AlertDialogAction
              disabled={!closedPeriodAcknowledged || closedPeriodActionPending}
              onClick={(event) => {
                event.preventDefault();
                confirmClosedPeriodAction();
              }}
            >
              {closedPeriodActionPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <ShieldAlert size={16} aria-hidden="true" />
              )}
              Подтвердить
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <Dialog
        open={Boolean(categoryChange)}
        onOpenChange={(open) => {
          if (!open && !categoryMutation.isPending) {
            setCategoryChange(null);
            setCategoryEffectiveDate(todayDateInputValue());
            setCategoryComment("");
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Изменить категорию</DialogTitle>
            <DialogDescription>
              {employee.full_name},{" "}
              {categoryChange ? (payrollRoleLabel(categoryChange.payrollRole) ?? "") : ""}
            </DialogDescription>
          </DialogHeader>

          {categoryChange ? (
            <div className="grid gap-4">
              <div className="grid gap-2 rounded-md border bg-muted/30 p-3 text-sm">
                <InfoRow label="Сейчас" value={categoryLabel(categoryChange.currentCategory)} />
                <InfoRow label="Новая" value={categoryLabel(categoryChange.nextCategory)} />
              </div>

              <Label className="grid gap-2">
                <span className="text-sm font-medium">Применить с даты</span>
                <Input
                  disabled={categoryMutation.isPending}
                  onChange={(event) => setCategoryEffectiveDate(event.target.value)}
                  type="date"
                  value={categoryEffectiveDate}
                />
              </Label>

              <Label className="grid gap-2">
                <span>{categoryCommentRequired ? "Комментарий" : "Комментарий (опционально)"}</span>
                <Textarea
                  disabled={categoryMutation.isPending}
                  maxLength={1000}
                  onChange={(event) => setCategoryComment(event.target.value)}
                  placeholder={categoryCommentRequired ? "Обязателен для задней даты" : ""}
                  value={categoryComment}
                />
              </Label>
            </div>
          ) : null}

          <DialogFooter>
            <Button
              disabled={categoryMutation.isPending}
              onClick={() => setCategoryChange(null)}
              type="button"
              variant="outline"
            >
              Отмена
            </Button>
            <Button
              disabled={!canApplyCategoryChange}
              onClick={() => {
                if (!categoryChange || !selectedCategoryEffectiveDate) {
                  return;
                }
                categoryMutation.mutate({
                  change: categoryChange,
                  effectiveFrom: selectedCategoryEffectiveDate,
                  comment: categoryComment,
                });
              }}
              type="button"
            >
              {categoryMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <Save size={16} aria-hidden="true" />
              )}
              Применить
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <SubstituteRoleDialog
        isPending={substituteSaveMutation.isPending}
        onOpenChange={(open) => {
          if (!open && !substituteSaveMutation.isPending) {
            setSubstituteDialog(null);
          }
        }}
        onSubmit={(payload) => substituteSaveMutation.mutate(payload)}
        open={Boolean(substituteDialog)}
        state={substituteDialog}
      />

      <AlertDialog
        open={Boolean(substituteDeleteTarget)}
        onOpenChange={(open) => {
          if (!open && !substituteDeleteMutation.isPending) {
            setSubstituteDeleteTarget(null);
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Удалить подменную роль?</AlertDialogTitle>
            <AlertDialogDescription>
              {substituteDeleteTarget
                ? `${payrollRoleLabel(substituteDeleteTarget.payroll_role)} · ${categoryLabel(
                    substituteDeleteTarget.category,
                  )}`
                : ""}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={substituteDeleteMutation.isPending}>
              Отмена
            </AlertDialogCancel>
            <AlertDialogAction
              disabled={!substituteDeleteTarget || substituteDeleteMutation.isPending}
              onClick={(event) => {
                event.preventDefault();
                if (substituteDeleteTarget) {
                  substituteDeleteMutation.mutate(substituteDeleteTarget.id);
                }
              }}
            >
              {substituteDeleteMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : null}
              Удалить
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <Dialog
        open={hireDateOpen}
        onOpenChange={(open) => {
          setHireDateOpen(open);
          if (!open && !hireDateMutation.isPending) {
            setHireDateValue("");
            setHireDateComment("");
            setHireDateConfirmOpen(false);
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Указать дату приёма</DialogTitle>
            <DialogDescription>
              Дата влияет на стаж и ставку накопительного фонда для текущего сотрудника.
            </DialogDescription>
          </DialogHeader>

          <form className="grid gap-4" onSubmit={requestHireDateConfirmation}>
            <Label className="grid gap-2">
              <span>Дата приёма</span>
              <Input
                max={hireDateMax}
                min={hireDateMin}
                onChange={(event) => setHireDateValue(event.target.value)}
                type="date"
                value={hireDateValue}
              />
            </Label>

            <Label className="grid gap-2">
              <span>Комментарий (опционально)</span>
              <Textarea
                maxLength={1000}
                onChange={(event) => setHireDateComment(event.target.value)}
                placeholder="Например: дата из анкеты сотрудника"
                value={hireDateComment}
              />
            </Label>

            <DialogFooter>
              <Button
                disabled={hireDateMutation.isPending}
                onClick={() => {
                  setHireDateOpen(false);
                  setHireDateValue("");
                  setHireDateComment("");
                }}
                type="button"
                variant="outline"
              >
                Отмена
              </Button>
              <Button disabled={!hireDateCanSubmit || hireDateMutation.isPending} type="submit">
                {hireDateMutation.isPending ? (
                  <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                ) : (
                  <Save size={16} aria-hidden="true" />
                )}
                Сохранить
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <AlertDialog open={hireDateConfirmOpen} onOpenChange={setHireDateConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Установить дату приёма {formatDate(hireDateValue)}?</AlertDialogTitle>
            <AlertDialogDescription>
              Стаж и ставка накопительного фонда будут пересчитаны от этой даты.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={hireDateMutation.isPending}>Отмена</AlertDialogCancel>
            <AlertDialogAction
              disabled={hireDateMutation.isPending}
              onClick={(event) => {
                event.preventDefault();
                hireDateMutation.mutate();
              }}
            >
              {hireDateMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : null}
              Сохранить
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <Dialog open={dismissOpen} onOpenChange={setDismissOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Уволить {employee.full_name}?</DialogTitle>
            <DialogDescription>
              Укажите дату увольнения и причину для карточки сотрудника.
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
              <Select
                disabled={dismissalReasonsQuery.isLoading || dismissalReasons.length === 0}
                onValueChange={setDismissReasonKey}
                value={dismissReasonKey}
              >
                <SelectTrigger>
                  <SelectValue
                    placeholder={
                      dismissalReasonsQuery.isLoading ? "Загрузка причин..." : "Выберите причину"
                    }
                  />
                </SelectTrigger>
                <SelectContent>
                  {dismissalReasons.map((reason) => (
                    <SelectItem value={reason.key} key={reason.key}>
                      {reason.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Label>

            {dismissalReasonsQuery.isError ? (
              <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                {apiErrorMessage(dismissalReasonsQuery.error, "Не удалось загрузить причины")}
              </div>
            ) : null}

            <Label className="grid gap-2">
              <span>{dismissCommentRequired ? "Комментарий" : "Комментарий (опционально)"}</span>
              <textarea
                className="min-h-24 rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                onChange={(event) => setDismissComment(event.target.value)}
                placeholder={dismissCommentRequired ? "Обязателен" : "Можно оставить пустым"}
                value={dismissComment}
              />
            </Label>

            {dismissDepositHasBalance ? (
              <section className="grid gap-3 rounded-lg border bg-card p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="text-sm font-medium">Депозит сотрудника</div>
                  <div className="text-sm font-semibold">
                    {formatDepositMoney(dismissDepositBalance)}
                  </div>
                </div>

                {employee.active_notice ? (
                  <div
                    className={cn(
                      "rounded-md border px-3 py-2 text-sm",
                      noticeTriggersFullPayout
                        ? "border-emerald-200 bg-emerald-50 text-emerald-800"
                        : "border-amber-200 bg-amber-50 text-amber-800",
                    )}
                  >
                    Уведомил об уходе {formatDate(employee.active_notice.notice_date)}. Прошло{" "}
                    {noticeDaysToFire ?? employee.active_notice.days_since} дней —{" "}
                    {noticeTriggersFullPayout ? "выплата по умолчанию" : "списание по умолчанию"}
                  </div>
                ) : (
                  <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                    Не было уведомления
                  </div>
                )}

                <div className="grid gap-2 text-sm">
                  <label className="flex items-center gap-2 rounded-md border bg-background px-3 py-2">
                    <input
                      checked={dismissDepositAction === "payout_full"}
                      name="dismiss-deposit-action"
                      onChange={() => setDismissDepositAction("payout_full")}
                      type="radio"
                    />
                    <span>Выплатить полностью ({formatDepositMoney(dismissDepositBalance)})</span>
                  </label>
                  <label className="grid gap-2 rounded-md border bg-background px-3 py-2">
                    <span className="flex items-center gap-2">
                      <input
                        checked={dismissDepositAction === "payout_partial"}
                        name="dismiss-deposit-action"
                        onChange={() => setDismissDepositAction("payout_partial")}
                        type="radio"
                      />
                      <span>Выплатить частично</span>
                    </span>
                    {dismissDepositAction === "payout_partial" ? (
                      <div className="grid gap-1 pl-6">
                        <Input
                          autoFocus
                          inputMode="decimal"
                          max={Math.max(dismissDepositBalance - 0.01, 0)}
                          min={0.01}
                          onChange={(event) => setDismissDepositAmount(event.target.value)}
                          placeholder="0"
                          step="0.01"
                          type="number"
                          value={dismissDepositAmount}
                        />
                        <span
                          className={cn(
                            "text-xs",
                            dismissDepositDecision.isValid
                              ? "text-muted-foreground"
                              : "text-destructive",
                          )}
                        >
                          Остаток ({formatDepositMoney(dismissDepositDecision.writeoff)}) будет
                          списан автоматически
                        </span>
                      </div>
                    ) : null}
                  </label>
                  <label className="flex items-center gap-2 rounded-md border bg-background px-3 py-2">
                    <input
                      checked={dismissDepositAction === "write_off"}
                      name="dismiss-deposit-action"
                      onChange={() => setDismissDepositAction("write_off")}
                      type="radio"
                    />
                    <span>
                      Не выплачивать (списать {formatDepositMoney(dismissDepositBalance)})
                    </span>
                  </label>
                </div>

                <Label className="grid gap-2">
                  <span>Комментарий к решению</span>
                  <textarea
                    className="min-h-20 rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    onChange={(event) => setDismissDepositComment(event.target.value)}
                    value={dismissDepositComment}
                  />
                </Label>
              </section>
            ) : null}
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
              disabled={!canSubmitDismiss}
              onClick={() => setDismissConfirmOpen(true)}
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

      <AlertDialog open={dismissConfirmOpen} onOpenChange={setDismissConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Подтвердите увольнение</AlertDialogTitle>
            <AlertDialogDescription>
              Уволить {employee.full_name} с {formatDate(dismissFireDate)}, выплатить{" "}
              {formatDepositMoney(dismissDepositDecision.payout)}, списать{" "}
              {formatDepositMoney(dismissDepositDecision.writeoff)}.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={dismissMutation.isPending}>Отмена</AlertDialogCancel>
            <AlertDialogAction
              disabled={!canSubmitDismiss}
              onClick={() => dismissMutation.mutate()}
            >
              Уволить
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <Dialog
        open={premiumConfirmOpen}
        onOpenChange={(open) => {
          setPremiumConfirmOpen(open);
          if (!open && !mutation.isPending) {
            setPendingDraftPatch(null);
            setPremiumEffectiveDate(todayDateInputValue());
            setPremiumComment("");
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Подтвердите изменение надбавок</DialogTitle>
            <DialogDescription>
              Изменение будет сохранено с выбранной даты и попадёт в историю штата.
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-4">
            <div className="grid gap-2 text-sm">
              {premiumConfirmActions.map((action) => (
                <div className="rounded-md border bg-muted/30 px-3 py-2" key={action}>
                  {action}
                </div>
              ))}
            </div>

            <Label className="grid gap-2">
              <span className="text-sm font-medium">Применить с даты</span>
              <Input
                disabled={mutation.isPending}
                onChange={(event) => setPremiumEffectiveDate(event.target.value)}
                type="date"
                value={premiumEffectiveDate}
              />
            </Label>

            <Label className="grid gap-2">
              <span>{premiumCommentRequired ? "Комментарий" : "Комментарий (опционально)"}</span>
              <Textarea
                disabled={mutation.isPending}
                maxLength={1000}
                onChange={(event) => setPremiumComment(event.target.value)}
                placeholder={premiumCommentRequired ? "Обязателен для задней даты" : ""}
                value={premiumComment}
              />
            </Label>
          </div>

          <DialogFooter>
            <Button
              disabled={mutation.isPending}
              onClick={() => {
                setPremiumConfirmOpen(false);
                setPendingDraftPatch(null);
                setPremiumEffectiveDate(todayDateInputValue());
                setPremiumComment("");
              }}
              type="button"
              variant="outline"
            >
              Отмена
            </Button>
            <Button
              disabled={!canApplyPremiumChange}
              onClick={() => {
                if (!pendingDraftPatch) {
                  return;
                }
                mutation.mutate({
                  ...pendingDraftPatch,
                  effective_from: premiumEffectiveDate,
                  comment: premiumComment.trim() || undefined,
                });
                setPremiumConfirmOpen(false);
                setPendingDraftPatch(null);
                setPremiumEffectiveDate(todayDateInputValue());
                setPremiumComment("");
              }}
              type="button"
            >
              {mutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <Save size={16} aria-hidden="true" />
              )}
              Сохранить
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog
        onOpenChange={(open) => {
          if (!open) {
            setPremiumTransferConflict(null);
          }
        }}
        open={Boolean(premiumTransferConflict)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Перенести надбавку?</AlertDialogTitle>
            <AlertDialogDescription>
              {premiumTransferConflict?.message ??
                "На эту должность уже назначена такая надбавка."}
              <br />
              Перенести надбавку с {premiumTransferConflict?.existingFullName} на этого
              сотрудника?
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={mutation.isPending}>Отмена</AlertDialogCancel>
            <AlertDialogAction
              disabled={!premiumTransferConflict || mutation.isPending}
              onClick={() => {
                if (!premiumTransferConflict) {
                  return;
                }
                mutation.mutate({
                  ...premiumTransferConflict.patch,
                  transfer_from_existing: true,
                });
              }}
            >
              Перенести
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
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

function RoleReviewMarker({ employee }: { employee: Employee }) {
  if (!employee.requires_role_review) {
    return null;
  }
  return (
    <span
      aria-label="У сотрудника в iiko есть дополнительные должности, требуется уточнение"
      className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-amber-50 text-amber-700"
      title="У сотрудника в iiko есть дополнительные должности, требуется уточнение"
    >
      <CircleAlert size={14} aria-hidden="true" />
    </span>
  );
}

function CategoryCell({
  employee,
  onPendingClick,
}: {
  employee: Employee;
  onPendingClick?: (assignment: EmployeeRoleAssignment) => void;
}) {
  const pending = pendingAssignment(employee);
  return (
    <span className="inline-flex items-center gap-1.5">
      <span>{categoryLabel(primaryAssignment(employee)?.category ?? employee.category)}</span>
      {pending ? (
        <PendingAssignmentIndicator
          assignment={pending}
          employee={employee}
          onClick={onPendingClick}
        />
      ) : null}
    </span>
  );
}

function EmployeeTags({
  employee,
  compact = false,
  onPendingClick,
}: {
  employee: Employee;
  compact?: boolean;
  onPendingClick?: (assignment: EmployeeRoleAssignment) => void;
}) {
  const primary = primaryAssignment(employee);
  const substituteCount = substituteAssignments(employee).length;
  const additionalRoles = Math.max(
    ordinaryAssignments(employee).length - (primary ? 1 : 0),
    0,
  );
  const pending = pendingAssignment(employee);
  const roleTag = primary
    ? `${payrollRoleLabel(primary.payroll_role)} · ${categoryLabel(primary.category)}`
    : employee.category
      ? categoryLabel(employee.category)
      : null;
  const tags = [
    employee.is_senior ? "Старший" : null,
    employee.is_deputy_senior ? "Зам" : null,
    additionalRoles > 0 ? `+${additionalRoles} ролей` : null,
    substituteCount > 0 ? `${substituteCount} подмен` : null,
  ].filter((tag): tag is string => Boolean(tag));

  if (tags.length === 0 && !roleTag && !employee.active_notice && !pending) {
    return <span className="text-sm text-muted-foreground">Без надбавок</span>;
  }

  return (
    <div className={cn("flex flex-wrap gap-2", compact ? "max-w-[240px]" : undefined)}>
      {employee.active_notice ? (
        <Badge className={noticeBadgeClass(employee.active_notice.will_trigger_full_payout)}>
          Уведомил об уходе
        </Badge>
      ) : null}
      {roleTag ? (
        <Badge
          className={cn(
            "rounded-md shadow-none",
            roleColorClasses(primary?.payroll_role).container,
            roleColorClasses(primary?.payroll_role).primaryText,
          )}
        >
          <span>{roleTag}</span>
          {pending ? (
            <PendingAssignmentIndicator
              assignment={pending}
              employee={employee}
              onClick={onPendingClick}
            />
          ) : null}
        </Badge>
      ) : pending ? (
        <PendingAssignmentIndicator
          assignment={pending}
          employee={employee}
          onClick={onPendingClick}
        />
      ) : null}
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

function PendingAssignmentIndicator({
  assignment,
  employee,
  onClick,
}: {
  assignment: EmployeeRoleAssignment;
  employee: Employee;
  onClick?: (assignment: EmployeeRoleAssignment) => void;
}) {
  const tooltip = pendingAssignmentTooltip(employee, assignment);
  return (
    <span className="relative inline-flex shrink-0">
      <span
        aria-label={tooltip}
        className="group inline-flex h-5 w-5 items-center justify-center rounded-full text-primary outline-none hover:bg-primary/10 focus-visible:ring-2 focus-visible:ring-ring"
        onClick={(event) => {
          event.preventDefault();
          event.stopPropagation();
          onClick?.(assignment);
        }}
        onKeyDown={(event) => {
          if (event.key !== "Enter" && event.key !== " ") {
            return;
          }
          event.preventDefault();
          event.stopPropagation();
          onClick?.(assignment);
        }}
        role={onClick ? "button" : "img"}
        tabIndex={onClick ? 0 : -1}
        title={tooltip}
      >
        <Info size={14} aria-hidden="true" />
        <span className="pointer-events-none absolute left-1/2 top-full z-20 mt-2 hidden w-64 -translate-x-1/2 rounded-md border bg-popover px-3 py-2 text-left text-xs font-normal text-popover-foreground shadow-md group-hover:block group-focus-visible:block">
          {tooltip}
        </span>
      </span>
    </span>
  );
}

function employeePinState(employee: Employee): EmployeePinState {
  if (employee.pin_set_at) {
    return "local";
  }
  if (employee.pin_assumed_from_iiko) {
    return "iiko_assumed";
  }
  return "missing";
}

function EmployeePinBadge({ employee }: { employee: Employee }) {
  const state = employeePinState(employee);
  if (state === "local") {
    return (
      <Badge className="rounded-md border-emerald-200 bg-emerald-50 text-emerald-700 shadow-none">
        {EMPLOYEE_PIN_BADGE_LABELS.local} · {formatDateOnlyFromDateTime(employee.pin_set_at)}
      </Badge>
    );
  }
  if (state === "iiko_assumed") {
    return (
      <Badge className="rounded-md border-sky-200 bg-sky-50 text-sky-800 shadow-none">
        {EMPLOYEE_PIN_BADGE_LABELS.iiko_assumed}
      </Badge>
    );
  }
  return (
    <Badge className="rounded-md border-amber-200 bg-amber-50 text-amber-800 shadow-none">
      {EMPLOYEE_PIN_BADGE_LABELS.missing}
    </Badge>
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

function StaticField({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid gap-1">
      <span className="text-xs font-medium uppercase text-muted-foreground">{label}</span>
      <div className="flex h-10 items-center rounded-md border border-input bg-muted/30 px-3 text-sm">
        {value}
      </div>
    </div>
  );
}

function RoleReviewBanner({
  addDisabledReason,
  canEdit,
  employee,
  isIgnoring,
  onAddSubstitute,
  onIgnore,
  onOpenSettings,
  pairs,
}: {
  addDisabledReason?: string | null;
  canEdit: boolean;
  employee: Employee;
  isIgnoring: boolean;
  onAddSubstitute: (targetPosition: "Повар" | "Кассир") => void;
  onIgnore: () => void;
  onOpenSettings: () => void;
  pairs: SubstitutePair[];
}) {
  const payload = employee.role_review_payload ?? {};
  const reason = String(payload.reason ?? "missing_substitute");
  const extraPositions = reviewExtraPositions(employee);
  const firstPosition = extraPositions[0] as "Повар" | "Кассир" | undefined;
  const configured = Boolean(
    firstPosition && pairs.some((pair) => pair.to_position === firstPosition),
  );
  const addDisabled = Boolean(addDisabledReason);

  return (
    <section className="grid gap-3 rounded-lg border border-amber-200 bg-amber-50 p-4 text-amber-950">
      <div className="flex items-center gap-2 font-medium">
        <CircleAlert size={16} aria-hidden="true" />
        Требуется уточнение
      </div>
      <div className="grid gap-1 text-sm text-amber-900">
        <div>В iiko у этого сотрудника есть дополнительная должность:</div>
        <div className="font-semibold">{extraPositions.join(", ") || "Не указана"}</div>
        {reason === "unconfigured_pair" && firstPosition ? (
          <div>
            Пара "{employee.position} - {firstPosition}" не настроена.
          </div>
        ) : null}
      </div>
      {canEdit ? (
        <div className="flex flex-wrap gap-2">
          {reason === "unconfigured_pair" || !configured || !firstPosition ? (
            <Button
              className="border-amber-300 bg-background text-amber-950 hover:bg-amber-100"
              onClick={onOpenSettings}
              size="sm"
              type="button"
              variant="outline"
            >
              <SettingsIcon />
              Добавить пару в Настройках
            </Button>
          ) : (
            <div className="flex flex-wrap items-center gap-2">
              <Button
                className="border-amber-300 bg-background text-amber-950 hover:bg-amber-100"
                disabled={addDisabled}
                onClick={() => {
                  if (addDisabled) {
                    return;
                  }
                  onAddSubstitute(firstPosition);
                }}
                size="sm"
                title={addDisabledReason ?? undefined}
                type="button"
                variant="outline"
              >
                <Plus size={15} aria-hidden="true" />
                Добавить как substitute
              </Button>
              {addDisabledReason ? (
                <span className="text-xs text-amber-800">{addDisabledReason}</span>
              ) : null}
            </div>
          )}
          <Button
            disabled={isIgnoring}
            onClick={onIgnore}
            size="sm"
            type="button"
            variant="ghost"
          >
            {isIgnoring ? (
              <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
            ) : null}
            Игнорировать
          </Button>
        </div>
      ) : null}
    </section>
  );
}

function SettingsIcon() {
  return <ShieldAlert size={15} aria-hidden="true" />;
}

function SubstituteRolesSection({
  addDisabledReason,
  assignments,
  canEdit,
  onAdd,
  onDelete,
  onEdit,
  pairs,
}: {
  addDisabledReason?: string | null;
  assignments: EmployeeRoleAssignment[];
  canEdit: boolean;
  onAdd: (targetPosition: "Повар" | "Кассир") => void;
  onDelete: (assignment: EmployeeRoleAssignment) => void;
  onEdit: (assignment: EmployeeRoleAssignment) => void;
  pairs: SubstitutePair[];
}) {
  const targets = Array.from(new Set(pairs.map((pair) => pair.to_position)));
  const addDisabled = Boolean(addDisabledReason);
  return (
    <section className="grid gap-3 rounded-lg border bg-card p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="text-sm font-medium">Подменные роли</div>
          <div className="mt-1 text-xs text-muted-foreground">
            Дополнительная роль поверх основной должности. Используется для оплаты за конкретные смены
          </div>
        </div>
        {canEdit ? (
          <div className="flex flex-wrap items-center gap-2">
            {targets.map((target) => (
              <Button
                disabled={addDisabled}
                key={target}
                onClick={() => {
                  if (addDisabled) {
                    return;
                  }
                  onAdd(target);
                }}
                size="sm"
                title={addDisabledReason ?? undefined}
                type="button"
                variant="outline"
              >
                <Plus size={15} aria-hidden="true" />
                Добавить {target.toLowerCase()}
              </Button>
            ))}
            {addDisabledReason ? (
              <span className="text-xs text-muted-foreground">{addDisabledReason}</span>
            ) : null}
          </div>
        ) : null}
      </div>
      {targets.length > 0 ? (
        <div className="text-sm text-muted-foreground">
          Может выходить как: {targets.join(", ")}
        </div>
      ) : null}
      {assignments.length === 0 ? (
        <div className="rounded-md border bg-muted/30 px-3 py-2 text-sm text-muted-foreground">
          Подменные роли не назначены
        </div>
      ) : (
        <div className="grid gap-2">
          {assignments.map((assignment) => (
            <div
              className="grid gap-2 rounded-md border bg-background p-3 sm:grid-cols-[1fr_auto] sm:items-center"
              key={assignment.id}
            >
              <div className="min-w-0 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">
                    {payrollRoleLabel(assignment.payroll_role)}
                  </span>
                  <Badge className="rounded-md border-sky-200 bg-sky-50 text-sky-800 shadow-none">
                    подмена
                  </Badge>
                </div>
                <div className="mt-1 text-muted-foreground">
                  {categoryLabel(assignment.category)}, с {formatDate(assignment.effective_from)}
                </div>
              </div>
              {canEdit ? (
                <div className="flex gap-1">
                  <Button
                    onClick={() => onEdit(assignment)}
                    size="icon"
                    title="Изменить подменную роль"
                    type="button"
                    variant="ghost"
                  >
                    <Pencil size={15} aria-hidden="true" />
                  </Button>
                  <Button
                    onClick={() => onDelete(assignment)}
                    size="icon"
                    title="Удалить подменную роль"
                    type="button"
                    variant="ghost"
                  >
                    <Trash2 size={15} aria-hidden="true" />
                  </Button>
                </div>
              ) : null}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function SubstituteRoleDialog({
  isPending,
  onOpenChange,
  onSubmit,
  open,
  state,
}: {
  isPending: boolean;
  onOpenChange: (open: boolean) => void;
  onSubmit: (payload: {
    assignmentId?: string;
    payrollRole: PayrollRole;
    category: EmployeeCategory;
    effectiveFrom: string;
    comment: string;
  }) => void;
  open: boolean;
  state: SubstituteRoleDialogState;
}) {
  const assignment = state?.assignment;
  const assignmentCategory = assignment?.category;
  const assignmentEffectiveFrom = assignment?.effective_from;
  const assignmentId = assignment?.id;
  const assignmentPayrollRole = assignment?.payroll_role;
  const requestedTargetPosition = state?.targetPosition;
  const initialTarget =
    requestedTargetPosition ??
    (assignmentPayrollRole ? targetPositionForPayrollRole(assignmentPayrollRole) : "Повар");
  const [targetPosition, setTargetPosition] = useState<"Повар" | "Кассир">(initialTarget);
  const [payrollRole, setPayrollRole] = useState<PayrollRole>(
    assignmentPayrollRole ?? roleOptionsForSubstituteTarget(initialTarget)[0],
  );
  const [category, setCategory] = useState<EmployeeCategory>(
    assignmentCategory ?? categoriesForPayrollRole(payrollRole)[0],
  );
  const [effectiveFrom, setEffectiveFrom] = useState(
    assignmentEffectiveFrom ?? todayDateInputValue(),
  );
  const [comment, setComment] = useState("");

  useEffect(() => {
    const nextTarget =
      requestedTargetPosition ??
      (assignmentPayrollRole ? targetPositionForPayrollRole(assignmentPayrollRole) : "Повар");
    const nextRole = assignmentPayrollRole ?? roleOptionsForSubstituteTarget(nextTarget)[0];
    setTargetPosition(nextTarget);
    setPayrollRole(nextRole);
    setCategory(assignmentCategory ?? categoriesForPayrollRole(nextRole)[0]);
    setEffectiveFrom(assignmentEffectiveFrom ?? todayDateInputValue());
    setComment("");
  }, [
    assignmentCategory,
    assignmentEffectiveFrom,
    assignmentId,
    assignmentPayrollRole,
    requestedTargetPosition,
  ]);

  const roleOptions = roleOptionsForSubstituteTarget(targetPosition);
  const categoryOptionsForRole = categoriesForPayrollRole(payrollRole);
  const isBackDated = Boolean(effectiveFrom) && effectiveFrom < todayDateInputValue();
  const canSubmit =
    Boolean(payrollRole && category && effectiveFrom) &&
    (!isBackDated || comment.trim().length > 0) &&
    !isPending;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {state?.assignment ? "Изменить подменную роль" : "Добавить подменную роль"}
          </DialogTitle>
          <DialogDescription>Подмена оплачивается по ставке выбранной роли.</DialogDescription>
        </DialogHeader>

        <div className="grid gap-4">
          <StaticField label="Кого подменяет" value={targetPosition} />
          <Label className="grid gap-2">
            <span>Роль</span>
            <Select
              disabled={isPending}
              onValueChange={(value) => {
                const nextRole = value as PayrollRole;
                setPayrollRole(nextRole);
                const categories = categoriesForPayrollRole(nextRole);
                setCategory((current) =>
                  categories.includes(current) ? current : categories[0],
                );
              }}
              value={payrollRole}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {roleOptions.map((role) => (
                  <SelectItem key={role} value={role}>
                    {PAYROLL_ROLE_LABELS[role]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Label>
          <Label className="grid gap-2">
            <span>Категория</span>
            <Select
              disabled={isPending}
              onValueChange={(value) => setCategory(value as EmployeeCategory)}
              value={category}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {categoryOptionsForRole.map((item) => (
                  <SelectItem key={item} value={item}>
                    {EMPLOYEE_CATEGORY_LABELS[item]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Label>
          <Label className="grid gap-2">
            <span>С даты</span>
            <Input
              disabled={isPending}
              onChange={(event) => setEffectiveFrom(event.target.value)}
              type="date"
              value={effectiveFrom}
            />
          </Label>
          <Label className="grid gap-2">
            <span>
              Комментарий
              {isBackDated ? (
                <span className="ml-1 text-destructive">*</span>
              ) : (
                <span className="ml-1 text-muted-foreground">(необязательно)</span>
              )}
            </span>
            <Textarea
              disabled={isPending}
              maxLength={500}
              onChange={(event) => setComment(event.target.value)}
              placeholder={
                isBackDated
                  ? "Обязателен для изменения задним числом"
                  : "Например: подмена на время отпуска"
              }
              rows={3}
              value={comment}
            />
          </Label>
        </div>

        <DialogFooter>
          <Button disabled={isPending} onClick={() => onOpenChange(false)} type="button" variant="outline">
            Отмена
          </Button>
          <Button
            disabled={!canSubmit}
            onClick={() =>
              onSubmit({
                assignmentId: state?.assignment?.id,
                payrollRole,
                category,
                effectiveFrom,
                comment: comment.trim(),
              })
            }
            type="button"
          >
            {isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : (
              <Save size={16} aria-hidden="true" />
            )}
            Сохранить
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function invalidateStaffEmployeeQueries(queryClient: QueryClient, employeeId?: string) {
  void queryClient.invalidateQueries({ queryKey: ["employees"] });
  void queryClient.invalidateQueries({ queryKey: ["employees-roster"] });
  void queryClient.invalidateQueries({ queryKey: ["employees", "changes"] });
  void queryClient.invalidateQueries({ queryKey: ["substitute-pairs"] });
  if (employeeId) {
    void queryClient.invalidateQueries({ queryKey: ["employees", employeeId, "assignments"] });
    void queryClient.invalidateQueries({ queryKey: ["employees", employeeId, "changes"] });
    void queryClient.invalidateQueries({ queryKey: ["employees", employeeId, "position-history"] });
  }
}

function closedPayrollConflictFromError(
  error: unknown,
): { message: string; periods: ClosedPayrollPeriod[] } | null {
  if (apiErrorStatus(error) !== 409) {
    return null;
  }
  const detail = apiErrorDetail(error);
  if (!detail || typeof detail !== "object") {
    return null;
  }
  const record = detail as {
    code?: unknown;
    message?: unknown;
    periods?: unknown;
  };
  if (record.code !== "closed_payroll_period" || !Array.isArray(record.periods)) {
    return null;
  }
  const periods = record.periods.filter(isClosedPayrollPeriod);
  if (periods.length === 0) {
    return null;
  }
  return {
    message:
      typeof record.message === "string"
        ? record.message
        : "Изменение затрагивает закрытую неделю",
    periods,
  };
}

function isClosedPayrollPeriod(value: unknown): value is ClosedPayrollPeriod {
  if (!value || typeof value !== "object") {
    return false;
  }
  const record = value as Record<string, unknown>;
  return (
    typeof record.id === "string" &&
    typeof record.start_date === "string" &&
    typeof record.end_date === "string"
  );
}

function showPayrollWarnings(warnings: PayrollImpactWarning[] | undefined) {
  warnings?.forEach((warning) => {
    if (warning.message) {
      toast.warning(warning.message);
    }
  });
}

function visibleSubstituteAssignments(employee: Employee) {
  return substituteAssignments(employee).filter((assignment) =>
    Boolean(targetPositionForPayrollRole(assignment.payroll_role)),
  );
}

function reviewExtraPositions(employee: Employee): Array<"Повар" | "Кассир"> {
  const payload = employee.role_review_payload ?? {};
  const rawPositions =
    readStringArray(payload.extra_iiko_positions) ??
    readStringArray(payload.all_extra_iiko_positions) ??
    [];
  const positions = rawPositions
    .map((position) => canonicalPosition(position))
    .filter((position): position is "Повар" | "Кассир" =>
      position === "Повар" || position === "Кассир",
    );
  return Array.from(new Set(positions));
}

function readStringArray(value: unknown) {
  return Array.isArray(value) && value.every((item) => typeof item === "string")
    ? value
    : null;
}

function targetPositionForPayrollRole(
  payrollRole: PayrollRole | string | null | undefined,
): "Повар" | "Кассир" {
  return payrollRole === "administrator" ? "Кассир" : "Повар";
}

function roleOptionsForSubstituteTarget(targetPosition: "Повар" | "Кассир"): PayrollRole[] {
  return targetPosition === "Кассир" ? cashierPayrollRoles : cookPayrollRoles;
}

const canonicalPositionByName = new Map(
  canonicalPositions.map((position) => [normalizePosition(position), position]),
);

function isCookPosition(position: string | null) {
  return canonicalPosition(position) === "Повар";
}

function isTargetPosition(position: string | null) {
  return canonicalPosition(position) !== null;
}

function positionRequiresPin(position: string | null) {
  const canonical = canonicalPosition(position);
  return Boolean(canonical && !auxiliaryPositions.has(canonical));
}

function employeeMatchesPositionFilter(employee: Employee, filter: StaffPositionFilter) {
  const option = positionFilterOptions.find((item) => item.value === filter);
  if (!option?.positions) {
    return true;
  }
  const position = canonicalPosition(employee.position);
  return Boolean(position && option.positions.includes(position));
}

function secondaryFilterOptionsForPositionFilter(
  filter: StaffPositionFilter,
): StaffSecondaryFilterOption[] {
  if (filter === "cashiers") {
    return cashierSecondaryFilterOptions;
  }
  if (filter === "cooks") {
    return cookSecondaryFilterOptions;
  }
  return [];
}

function employeeMatchesSecondaryFilter(
  employee: Employee,
  positionFilter: StaffPositionFilter,
  filter: StaffSecondaryFilter,
) {
  if (filter === "all") {
    return true;
  }
  const assignments = activeAssignments(employee);
  if (positionFilter === "cashiers" && isEmployeeCategory(filter)) {
    return assignments.some((assignment) => assignment.category === filter);
  }
  if (positionFilter === "cooks" && isPayrollRole(filter)) {
    return assignments.some((assignment) => assignment.payroll_role === filter);
  }
  return true;
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

function noticeBadgeClass(willTriggerFullPayout: boolean) {
  return willTriggerFullPayout
    ? "rounded-md border-emerald-200 bg-emerald-50 text-emerald-700 shadow-none"
    : "rounded-md border-amber-200 bg-amber-50 text-amber-700 shadow-none";
}

function categoriesForPayrollRole(payrollRole: PayrollRole | "") {
  return payrollRole ? payrollRoleCategories[payrollRole] : [];
}

function isCategoryAllowedForRole(
  payrollRole: PayrollRole,
  category: EmployeeCategory | "" | null | undefined,
) {
  return Boolean(category && categoriesForPayrollRole(payrollRole).includes(category));
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

type StaffPermissions = ReturnType<typeof usePermissions>;

function canReadStaffPosition(position: CanonicalPosition, permissions: StaffPermissions) {
  const area = staffAreaForPosition(position);
  if (area === "administration") return permissions.hasPermission("staff.administration.read");
  if (area === "cooks") return permissions.hasAnyPermission(["staff.cooks.read"]);
  if (area === "cashiers") return permissions.hasAnyPermission(["staff.cashiers.read"]);
  if (area === "auxiliary") return permissions.hasAnyPermission(["staff.auxiliary.read"]);
  if (area === "couriers") return permissions.hasPermission("staff.couriers.read");
  return false;
}

function canCreateStaffPosition(position: CanonicalPosition, permissions: StaffPermissions) {
  const area = staffAreaForPosition(position);
  if (area === "administration") return permissions.hasPermission("staff.administration.create");
  if (area === "cooks") return permissions.hasPermission("staff.cooks.create");
  if (area === "cashiers") return permissions.hasPermission("staff.cashiers.create");
  if (area === "couriers") return permissions.hasPermission("staff.couriers.create");
  return false;
}

function canEditStaffEmployee(employee: Employee, permissions: StaffPermissions) {
  const area = staffAreaForEmployee(employee);
  if (area === "administration") return permissions.hasPermission("staff.administration.edit");
  if (area === "cooks") return permissions.hasAnyPermission(["staff.cooks.edit"]);
  if (area === "cashiers") return permissions.hasAnyPermission(["staff.cashiers.edit"]);
  if (area === "auxiliary") return permissions.hasAnyPermission(["staff.auxiliary.edit"]);
  if (area === "couriers") return permissions.hasPermission("staff.couriers.edit");
  return false;
}

function canDismissStaffEmployee(employee: Employee, permissions: StaffPermissions) {
  const area = staffAreaForEmployee(employee);
  if (area === "administration") return permissions.hasPermission("staff.administration.dismiss");
  if (area === "cooks") return permissions.hasAnyPermission(["staff.cooks.dismiss"]);
  if (area === "cashiers") return permissions.hasAnyPermission(["staff.cashiers.dismiss"]);
  if (area === "auxiliary") return permissions.hasAnyPermission(["staff.auxiliary.dismiss"]);
  if (area === "couriers") return permissions.hasPermission("staff.couriers.dismiss");
  return false;
}

function canReinstateStaffEmployee(employee: Employee, permissions: StaffPermissions) {
  const area = staffAreaForEmployee(employee);
  if (area === "administration") return permissions.hasPermission("staff.administration.reinstate");
  if (area === "cooks") return permissions.hasAnyPermission(["staff.cooks.reinstate"]);
  if (area === "cashiers") return permissions.hasAnyPermission(["staff.cashiers.reinstate"]);
  if (area === "auxiliary") return permissions.hasAnyPermission(["staff.auxiliary.reinstate"]);
  if (area === "couriers") return permissions.hasPermission("staff.couriers.reinstate");
  return false;
}

function canReadStaffEmployeeHistory(employee: Employee, permissions: StaffPermissions) {
  const area = staffAreaForEmployee(employee);
  if (area === "administration") return permissions.hasPermission("staff.administration.history.read");
  if (area === "cooks") return permissions.hasAnyPermission(["staff.cooks.history.read"]);
  if (area === "cashiers") return permissions.hasAnyPermission(["staff.cashiers.history.read"]);
  if (area === "auxiliary") return permissions.hasAnyPermission(["staff.auxiliary.history.read"]);
  if (area === "couriers") return permissions.hasPermission("staff.couriers.history.read");
  return false;
}

function staffAreaForEmployee(employee: Employee) {
  return staffAreaForPosition(canonicalPosition(employee.position));
}

function staffAreaForPosition(position: CanonicalPosition | null): "administration" | "cooks" | "cashiers" | "auxiliary" | "couriers" | null {
  if (!position) return null;
  if (position === "Управляющий" || position === "Менеджер" || position === "Системный администратор") {
    return "administration";
  }
  if (position === "Повар") return "cooks";
  if (position === "Кассир") return "cashiers";
  if (position === "Курьер" || position === "Старший курьер") return "couriers";
  if (auxiliaryPositions.has(position)) return "auxiliary";
  return null;
}

function dateTimeFilterStart(value: string) {
  return value ? `${value}T00:00:00+03:00` : undefined;
}

function dateTimeFilterEnd(value: string) {
  return value ? `${value}T23:59:59+03:00` : undefined;
}

function changeTypeLabel(changeType: string) {
  return EMPLOYEE_CHANGE_TYPE_LABELS[changeType] ?? changeType;
}

function formatEffectivePeriod(change: EmployeeChangeEvent) {
  if (
    change.effective_from &&
    change.effective_to &&
    change.effective_from !== change.effective_to
  ) {
    return `${formatDate(change.effective_from)} - ${formatDate(change.effective_to)}`;
  }
  if (change.effective_from) {
    return formatDate(change.effective_from);
  }
  if (change.effective_to) {
    return `до ${formatDate(change.effective_to)}`;
  }
  return "Не указана";
}

function employeeNameForChange(change: EmployeeChangeEvent, employeeById?: Map<string, Employee>) {
  if (change.employee_id) {
    return (
      employeeById?.get(change.employee_id)?.full_name ?? `Сотрудник ${shortId(change.employee_id)}`
    );
  }
  if (change.source === "iiko_sync") {
    return "Запись IIko";
  }
  return "Не указан";
}

function shortId(value: string) {
  return value.slice(0, 8);
}

function changeMatchesActionDate(change: EmployeeChangeEvent, actionDate: string) {
  if (!actionDate) {
    return true;
  }
  if (change.effective_from && change.effective_to) {
    return change.effective_from <= actionDate && change.effective_to >= actionDate;
  }
  return change.effective_from === actionDate || change.effective_to === actionDate;
}

function isRetroactiveChange(change: EmployeeChangeEvent) {
  if (change.payroll_impact_metadata.retroactive === true) {
    return true;
  }
  if (!change.effective_from) {
    return false;
  }
  return change.effective_from < dateInputValueFromDateTime(change.changed_at);
}

function dateInputValueFromDateTime(value: string) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    day: "2-digit",
    month: "2-digit",
    timeZone: "Europe/Moscow",
    year: "numeric",
  }).formatToParts(new Date(value));
  const dateParts = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${dateParts.year}-${dateParts.month}-${dateParts.day}`;
}

function dismissalReasonOptions(reasons: EmployeeDismissalReason[]): DismissalReasonOption[] {
  const byCode = new Map(reasons.map((reason) => [reason.code, reason]));
  const byLabel = new Map(reasons.map((reason) => [reason.label, reason]));
  const usedIds = new Set<string>();
  const canonicalOptions = dismissalReasonDefinitions.map((definition) => {
    const reason = byCode.get(definition.code) ?? byLabel.get(definition.label);
    if (reason) {
      usedIds.add(reason.id);
    }
    return {
      key: reason?.id ?? definition.code,
      id: reason?.id,
      code: reason?.code ?? definition.code,
      label: reason?.label ?? definition.label,
      requires_comment: reason?.requires_comment ?? definition.requires_comment,
    };
  });
  const extraOptions = reasons
    .filter((reason) => !usedIds.has(reason.id))
    .map((reason) => ({
      key: reason.id,
      id: reason.id,
      code: reason.code,
      label: reason.label,
      requires_comment: reason.requires_comment,
    }));
  return [...canonicalOptions, ...extraOptions];
}

function premiumChangeConfirmations(initial: Draft, draft: Draft) {
  const actions: string[] = [];
  if (draft.is_senior !== initial.is_senior) {
    actions.push(
      draft.is_senior
        ? "Вы уверены, что хотите присвоить сотруднику надбавку Старший?"
        : "Вы уверены, что хотите снять с сотрудника надбавку Старший?",
    );
  }
  if (draft.is_deputy_senior !== initial.is_deputy_senior) {
    actions.push(
      draft.is_deputy_senior
        ? "Вы уверены, что хотите присвоить сотруднику надбавку Зам старшего?"
        : "Вы уверены, что хотите снять с сотрудника надбавку Зам старшего?",
    );
  }
  return actions;
}

function premiumTransferConflictFromError(
  error: unknown,
  patch: EmployeePatch,
): PremiumTransferConflict | null {
  if (apiErrorStatus(error) !== 409) {
    return null;
  }
  const detail = apiErrorDetail(error);
  if (!isPremiumTransferConflictDetail(detail)) {
    return null;
  }
  return {
    patch,
    message: detail.message,
    existingFullName: detail.existing_full_name,
  };
}

function isPremiumTransferConflictDetail(detail: unknown): detail is {
  code: "senior_already_assigned" | "deputy_senior_already_assigned";
  message: string;
  existing_employee_id: string;
  existing_full_name: string;
} {
  if (!detail || typeof detail !== "object") {
    return false;
  }
  const value = detail as Record<string, unknown>;
  return (
    (value.code === "senior_already_assigned" ||
      value.code === "deputy_senior_already_assigned") &&
    typeof value.message === "string" &&
    typeof value.existing_employee_id === "string" &&
    typeof value.existing_full_name === "string"
  );
}

function EmployeeChangeStatusBadge({ status }: { status: EmployeeChangeStatus }) {
  const className =
    status === "success"
      ? "border-emerald-200 bg-emerald-50 text-emerald-700"
      : status === "error"
        ? "border-red-200 bg-red-50 text-red-700"
        : status === "requires_review"
          ? "border-amber-200 bg-amber-50 text-amber-700"
          : "border-zinc-200 bg-zinc-50 text-zinc-600";

  return (
    <Badge className={cn("rounded-md border font-medium shadow-none", className)}>
      {EMPLOYEE_CHANGE_STATUS_LABELS[status]}
    </Badge>
  );
}

function activeAssignments(employee: Employee) {
  const today = todayDateInputValue();
  return (employee.assignments ?? []).filter(
    (assignment) =>
      assignment.effective_from <= today &&
      (!assignment.effective_to || assignment.effective_to > today),
  );
}

function pendingAssignments(employee: Employee) {
  const today = todayDateInputValue();
  return (employee.assignments ?? [])
    .filter((assignment) => assignment.is_pending || assignment.effective_from > today)
    .filter((assignment) => pendingAssignmentIsMeaningful(employee, assignment))
    .sort((left, right) => left.effective_from.localeCompare(right.effective_from));
}

function pendingAssignment(employee: Employee) {
  return pendingAssignments(employee)[0] ?? null;
}

function pendingAssignmentIsMeaningful(
  employee: Employee,
  assignment: EmployeeRoleAssignment,
) {
  const current = currentAssignmentForPending(employee, assignment);
  return !(
    current &&
    current.payroll_role === assignment.payroll_role &&
    current.category === assignment.category
  );
}

function currentAssignmentForPending(
  employee: Employee,
  assignment: EmployeeRoleAssignment,
) {
  const active = activeAssignments(employee);
  return (
    active.find((item) => item.payroll_role === assignment.payroll_role) ??
    active.find((item) => item.is_primary) ??
    active[0] ??
    null
  );
}

function pendingAssignmentTooltip(employee: Employee, assignment: EmployeeRoleAssignment) {
  const current = currentAssignmentForPending(employee, assignment);
  const roleChanged = Boolean(current && current.payroll_role !== assignment.payroll_role);
  const categoryChanged = Boolean(!current || current.category !== assignment.category);
  const dateLabel = formatDate(assignment.effective_from);
  if (roleChanged && categoryChanged) {
    return `Запланировано: с ${dateLabel} категория и роль сменятся на ${payrollRoleLabel(
      assignment.payroll_role,
    )} · ${categoryLabel(assignment.category)}`;
  }
  if (roleChanged) {
    return `Запланировано: с ${dateLabel} роль сменится на ${payrollRoleLabel(
      assignment.payroll_role,
    )}`;
  }
  return `Запланировано: с ${dateLabel} категория сменится на ${categoryLabel(
    assignment.category,
  )}`;
}

function primaryAssignment(employee: Employee) {
  const assignments = ordinaryAssignments(employee);
  return assignments.find((assignment) => assignment.is_primary) ?? assignments[0] ?? null;
}

function ordinaryAssignments(employee: Employee) {
  return activeAssignments(employee).filter((assignment) => !assignment.is_substitute);
}

function substituteAssignments(employee: Employee) {
  return activeAssignments(employee).filter((assignment) => assignment.is_substitute);
}

function payrollRolesForPosition(position: string | null): PayrollRole[] {
  const canonical = canonicalPosition(position);
  return canonical ? positionPayrollRoles[canonical] : [];
}

function toEditorDraft(employee: Employee): StaffEditorDraft {
  return {
    ...toDraft(employee),
    assignments: ordinaryAssignments(employee).map((assignment) => ({
      id: assignment.id,
      draft_id: assignment.id,
      payroll_role: assignment.payroll_role,
      category: assignment.category,
      is_primary: assignment.is_primary,
    })),
    pin_code: "",
    pin_confirmation: "",
  };
}

function toDraft(employee: Employee): Draft {
  return {
    full_name: employee.full_name,
    position: employee.position,
    is_senior: employee.is_senior,
    is_deputy_senior: employee.is_deputy_senior,
  };
}

function editorDraftSnapshot(draft: StaffEditorDraft) {
  return {
    full_name: draft.full_name.trim(),
    position: draft.position,
    is_senior: draft.is_senior,
    is_deputy_senior: draft.is_deputy_senior,
    assignments: assignmentDraftsSnapshot(draft.assignments),
    pin_code: draft.pin_code,
    pin_confirmation: draft.pin_confirmation,
  };
}

function assignmentDraftsSnapshot(assignments: AssignmentDraft[]) {
  return assignments
    .map((assignment) => ({
      payroll_role: assignment.payroll_role,
      category: assignment.category,
      is_primary: assignment.is_primary,
    }))
    .sort((left, right) => left.payroll_role.localeCompare(right.payroll_role));
}

function pinDraftTouched(draft: StaffEditorDraft) {
  return draft.pin_code.length > 0 || draft.pin_confirmation.length > 0;
}

function sanitizePinInput(value: string) {
  return value.replace(/\D/g, "").slice(0, 4);
}

function validateEditorRoles(draft: StaffEditorDraft) {
  const position = canonicalPosition(draft.position);
  if (!position) {
    return "Выберите каноническую должность";
  }
  const allowedRoles = positionPayrollRoles[position];
  if (allowedRoles.length === 0) {
    return draft.assignments.length > 0 ? "Для этой должности роли не предусмотрены" : null;
  }
  if (draft.assignments.length === 0) {
    return "Добавьте основную роль и категорию";
  }
  const primaryCount = draft.assignments.filter((assignment) => assignment.is_primary).length;
  if (primaryCount !== 1) {
    return "Выберите одну основную роль";
  }
  const seenRoles = new Set<PayrollRole>();
  for (const assignment of draft.assignments) {
    if (!allowedRoles.includes(assignment.payroll_role)) {
      return "Роль не соответствует должности";
    }
    if (seenRoles.has(assignment.payroll_role)) {
      return "Роли не должны повторяться";
    }
    seenRoles.add(assignment.payroll_role);
    if (!isCategoryAllowedForRole(assignment.payroll_role, assignment.category)) {
      return "Категория недоступна для этой роли";
    }
  }
  return null;
}

function hasExistingCategoryChange(initial: StaffEditorDraft, draft: StaffEditorDraft) {
  const initialById = new Map(
    initial.assignments
      .filter((assignment) => assignment.id)
      .map((assignment) => [assignment.id, assignment]),
  );
  return draft.assignments.some((assignment) => {
    const initialAssignment = assignment.id ? initialById.get(assignment.id) : null;
    return Boolean(initialAssignment && initialAssignment.category !== assignment.category);
  });
}

function categoryEffectiveChangeForDraft(
  initial: StaffEditorDraft,
  draft: StaffEditorDraft,
  patch: EmployeePatch,
): CategoryEffectiveChange | null {
  const patchKeys = Object.keys(patch);
  if (patchKeys.some((key) => !["roles", "category", "default_cooking_station"].includes(key))) {
    return null;
  }
  if (initial.assignments.length !== draft.assignments.length) {
    return null;
  }

  const initialById = new Map(
    initial.assignments
      .filter((assignment) => assignment.id)
      .map((assignment) => [assignment.id, assignment]),
  );
  const changed: CategoryEffectiveChange[] = [];
  for (const assignment of draft.assignments) {
    const initialAssignment = assignment.id ? initialById.get(assignment.id) : null;
    if (!initialAssignment || !assignment.id) {
      return null;
    }
    if (
      initialAssignment.payroll_role !== assignment.payroll_role ||
      initialAssignment.is_primary !== assignment.is_primary
    ) {
      return null;
    }
    if (initialAssignment.category !== assignment.category) {
      changed.push({
        assignmentId: assignment.id,
        payrollRole: assignment.payroll_role,
        currentCategory: initialAssignment.category,
        nextCategory: assignment.category,
      });
    }
  }
  return changed.length === 1 ? changed[0] : null;
}

function applyCategoryChangeToDraft(
  draft: StaffEditorDraft,
  change: CategoryEffectiveChange,
): StaffEditorDraft {
  return {
    ...draft,
    assignments: draft.assignments.map((assignment) =>
      assignment.id === change.assignmentId
        ? { ...assignment, category: change.nextCategory }
        : assignment,
    ),
  };
}

function buildEmployeePatch(initial: StaffEditorDraft, draft: StaffEditorDraft): EmployeePatch {
  const patch: EmployeePatch = {};
  const trimmedName = draft.full_name.trim();
  if (trimmedName !== initial.full_name.trim()) {
    patch.full_name = trimmedName;
  }
  if (draft.position !== initial.position) {
    patch.position = draft.position;
  }
  if (draft.is_senior !== initial.is_senior) {
    patch.is_senior = draft.is_senior;
  }
  if (draft.is_deputy_senior !== initial.is_deputy_senior) {
    patch.is_deputy_senior = draft.is_deputy_senior;
  }

  const initialRoles = JSON.stringify(assignmentDraftsSnapshot(initial.assignments));
  const currentRoles = JSON.stringify(assignmentDraftsSnapshot(draft.assignments));
  if (initialRoles !== currentRoles) {
    patch.roles = draft.assignments.map((assignment) => ({
      id: assignment.id ?? null,
      payroll_role: assignment.payroll_role,
      category: assignment.category,
      is_primary: assignment.is_primary,
    }));
    const primary = draft.assignments.find((assignment) => assignment.is_primary) ?? null;
    patch.category = primary?.category ?? null;
    patch.default_cooking_station =
      primary && isCookingStationRole(primary.payroll_role) ? primary.payroll_role : null;
  } else {
    const position = canonicalPosition(draft.position);
    if (
      position &&
      positionPayrollRoles[position].length === 0 &&
      draft.position !== initial.position
    ) {
      patch.category = null;
      patch.default_cooking_station = null;
    }
  }

  if (pinDraftTouched(draft)) {
    patch.pin_code = draft.pin_code;
  }
  return patch;
}

function setCashierAssignmentCategory(
  assignments: AssignmentDraft[],
  category: EmployeeCategory,
): AssignmentDraft[] {
  const existing = assignments.find((assignment) => assignment.payroll_role === "administrator");
  if (existing) {
    return [{ ...existing, category, is_primary: true }];
  }
  return [newAssignmentDraft("administrator", category, true)];
}

function newAssignmentDraft(
  payrollRole: PayrollRole,
  category: EmployeeCategory,
  isPrimary: boolean,
): AssignmentDraft {
  return {
    draft_id: newDraftId(),
    payroll_role: payrollRole,
    category,
    is_primary: isPrimary,
  };
}

function newDraftId() {
  return `draft-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function ensurePrimaryAssignment(assignments: AssignmentDraft[]) {
  if (assignments.length === 0 || assignments.some((assignment) => assignment.is_primary)) {
    return assignments;
  }
  return assignments.map((assignment, index) =>
    index === 0 ? { ...assignment, is_primary: true } : assignment,
  );
}

function setPrimaryAssignment(assignments: AssignmentDraft[], draftId: string) {
  return assignments.map((assignment) => ({
    ...assignment,
    is_primary: assignment.draft_id === draftId,
  }));
}

function updateAssignmentDraft(
  assignments: AssignmentDraft[],
  draftId: string,
  patch: Partial<Pick<AssignmentDraft, "payroll_role" | "category">>,
) {
  return assignments.map((assignment) =>
    assignment.draft_id === draftId ? { ...assignment, ...patch } : assignment,
  );
}

function removeAssignmentDraft(assignments: AssignmentDraft[], draftId: string) {
  return ensurePrimaryAssignment(
    assignments.filter((assignment) => assignment.draft_id !== draftId),
  );
}

function isCookingStationRole(role: PayrollRole): role is CookingStation {
  return (cookingStationOptions as PayrollRole[]).includes(role);
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

function dateInputDaysAgo(days: number) {
  const date = new Date();
  date.setDate(date.getDate() - days);
  const parts = new Intl.DateTimeFormat("en-CA", {
    day: "2-digit",
    month: "2-digit",
    timeZone: "Europe/Moscow",
    year: "numeric",
  }).formatToParts(date);
  const value = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${value.year}-${value.month}-${value.day}`;
}

function dateInputYearsAgo(years: number) {
  const [year, month, day] = todayDateInputValue().split("-").map(Number);
  const date = new Date(Date.UTC(year - years, month - 1, day));
  if (date.getUTCMonth() !== month - 1) {
    date.setUTCDate(0);
  }
  return date.toISOString().slice(0, 10);
}

function numericAmount(value: string | number | null | undefined) {
  const amount = Number(String(value ?? "0").replace(",", "."));
  return Number.isFinite(amount) ? amount : 0;
}

function decimalPayload(value: string) {
  return String(numericAmount(value));
}

function daysBetweenDateStrings(start: string, end: string) {
  const startDate = new Date(`${start}T00:00:00+03:00`);
  const endDate = new Date(`${end}T00:00:00+03:00`);
  return Math.floor((endDate.getTime() - startDate.getTime()) / 86_400_000);
}

function depositDismissDecision(
  action: DepositDismissAction,
  balance: number,
  partialAmountValue: string,
) {
  if (balance <= 0) {
    return { isValid: true, payout: 0, writeoff: 0 };
  }
  if (action === "payout_full") {
    return { isValid: true, payout: balance, writeoff: 0 };
  }
  if (action === "write_off") {
    return { isValid: true, payout: 0, writeoff: balance };
  }
  if (action === "payout_partial") {
    const payout = numericAmount(partialAmountValue);
    const isValid = payout > 0 && payout < balance;
    return {
      isValid,
      payout: isValid ? payout : 0,
      writeoff: Math.max(balance - (Number.isFinite(payout) ? payout : 0), 0),
    };
  }
  return { isValid: false, payout: 0, writeoff: balance };
}

function FundStatusBadge({ status }: { status: AccumulationFundAccount["status"] }) {
  const styles: Record<AccumulationFundAccount["status"], string> = {
    active: "border-blue-200 bg-blue-50 text-blue-700",
    paid_out: "border-emerald-200 bg-emerald-50 text-emerald-700",
    forfeited: "border-slate-200 bg-slate-100 text-slate-600",
  };
  const labels: Record<AccumulationFundAccount["status"], string> = {
    active: "Активен",
    paid_out: "Выплачено",
    forfeited: "Сгорело",
  };
  return <Badge className={cn("w-fit shadow-none", styles[status])}>{labels[status]}</Badge>;
}

function formatFundPercent(value: string | number | null | undefined) {
  const amount = Number(value);
  if (!Number.isFinite(amount)) {
    return "0%";
  }
  return `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format(amount)}%`;
}

function nextFundThresholdLabel(fund: AccumulationFundEmployeeDetail) {
  const thresholdDate = fund.employee.next_threshold_date;
  const nextRate = fund.employee.next_rate_percent;
  if (!thresholdDate || !nextRate) {
    return "Максимальная ставка";
  }
  return `${fund.employee.next_threshold_months} мес. (${formatDate(thresholdDate)}) → ${formatFundPercent(
    nextRate,
  )}`;
}

function forfeitedFundLabel(account: AccumulationFundAccount) {
  const date = account.forfeited_at ? formatDateOnlyFromDateTime(account.forfeited_at) : "Не было";
  return `Сгорело при увольнении ${date}: ${formatDepositMoney(account.forfeited)}`;
}

function employeeFundTransactionLabel(transaction: AccumulationFundTransaction) {
  if (transaction.comment) {
    return transaction.comment;
  }
  if (transaction.transaction_type === "accrual") {
    const rate = transaction.rate_percent
      ? `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format(
          Number(transaction.rate_percent) * 100,
        )}%`
      : "0%";
    return `Начисление, ставка ${rate}`;
  }
  if (transaction.transaction_type === "payout") {
    return "Выплата";
  }
  if (transaction.transaction_type === "initial_balance") {
    return "Начальный баланс";
  }
  return "Списание при увольнении";
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

function formatDateOnlyFromDateTime(value: string | null) {
  if (!value) {
    return "Не было";
  }

  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    timeZone: "Europe/Moscow",
    year: "numeric",
  }).format(new Date(value));
}

type EmployeeChangeDiffRow = {
  label: string;
  before?: string;
  after?: string;
  note?: string;
};

function employeeChangeDiffRows(change: EmployeeChangeEvent): EmployeeChangeDiffRow[] {
  if (change.change_type === "change_pin" || change.diff?.pin_changed === true) {
    return [{ label: "ПИН", note: "ПИН изменён" }];
  }

  if (change.change_type === "create_employee" || change.diff?.created === true) {
    return createdEmployeeRows(change.after_value);
  }

  if (change.diff?.assigned === true) {
    return assignmentRows(change.after_value, "Не было");
  }

  if (change.diff?.deleted === true) {
    return assignmentRows(change.before_value, "Закрыто").map((row) => ({
      ...row,
      before: row.after,
      after: "Закрыто",
    }));
  }

  if (change.diff?.skipped === true) {
    return [
      {
        label: "Причина пропуска",
        note: textValue(change.reason ?? valueFromRecord(change.diff, "reason") ?? "Не указана"),
      },
    ];
  }

  if (change.diff?.error === true) {
    return [{ label: "Ошибка", note: change.reason || "Не указана" }];
  }

  const rows = diffRowsFromDiff(change.diff);
  if (rows.length > 0) {
    return rows;
  }

  return diffRowsFromBeforeAfter(change.before_value, change.after_value);
}

function createdEmployeeRows(value: Record<string, unknown> | null): EmployeeChangeDiffRow[] {
  if (!value) {
    return [];
  }
  const fields = [
    "full_name",
    "position",
    "payroll_role",
    "roles",
    "category",
    "default_cooking_station",
    "is_senior",
    "is_deputy_senior",
    "hire_date",
    "fire_date",
    "status",
  ];
  return fields
    .filter((field) => Object.prototype.hasOwnProperty.call(value, field))
    .map((field) => ({
      label: changeFieldLabel(field),
      before: "Не было",
      after: formatChangeValue(field, value[field]),
    }));
}

function assignmentRows(
  value: Record<string, unknown> | null,
  before: string,
): EmployeeChangeDiffRow[] {
  if (!value) {
    return [];
  }
  return ["payroll_role", "category", "is_primary", "effective_from", "effective_to"]
    .filter((field) => Object.prototype.hasOwnProperty.call(value, field))
    .map((field) => ({
      label: changeFieldLabel(field),
      before,
      after: formatChangeValue(field, value[field]),
    }));
}

function diffRowsFromDiff(diff: Record<string, unknown> | null): EmployeeChangeDiffRow[] {
  if (!diff) {
    return [];
  }
  return Object.entries(diff)
    .filter(([field]) => !isHiddenChangeField(field))
    .flatMap(([field, value]) => {
      if (!isRecord(value) || !("before" in value) || !("after" in value)) {
        return [];
      }
      return [
        {
          label: changeFieldLabel(field),
          before: formatChangeValue(field, value.before),
          after: formatChangeValue(field, value.after),
        },
      ];
    });
}

function diffRowsFromBeforeAfter(
  before: Record<string, unknown> | null,
  after: Record<string, unknown> | null,
): EmployeeChangeDiffRow[] {
  if (!before && !after) {
    return [];
  }
  const keys = new Set([...Object.keys(before ?? {}), ...Object.keys(after ?? {})]);
  return Array.from(keys)
    .filter((key) => !isHiddenChangeField(key))
    .filter((key) => before?.[key] !== after?.[key])
    .map((key) => ({
      label: changeFieldLabel(key),
      before: formatChangeValue(key, before?.[key]),
      after: formatChangeValue(key, after?.[key]),
    }));
}

function payrollImpactWarnings(change: EmployeeChangeEvent) {
  const warnings: string[] = [];
  if (change.payroll_impact_metadata.retroactive === true || isRetroactiveChange(change)) {
    warnings.push("Изменение внесено задним числом.");
  }
  if (change.payroll_impact_metadata.requires_correction === true) {
    warnings.push("Есть закрытый расчётный период, может потребоваться корректировка зарплаты.");
  }
  if (change.payroll_impact_metadata.correction_pending === true) {
    warnings.push("Корректировка зарплаты ожидает обработки.");
  }
  return warnings;
}

function changeFieldLabel(field: string) {
  return EMPLOYEE_CHANGE_FIELD_LABELS[field] ?? field;
}

function formatChangeValue(field: string, value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "Не задано";
  }
  if (typeof value === "boolean") {
    return value ? "Да" : "Нет";
  }
  if (typeof value === "number") {
    return String(value);
  }
  if (typeof value === "string") {
    if (field === "category" && isEmployeeCategory(value)) {
      return EMPLOYEE_CATEGORY_LABELS[value];
    }
    if ((field === "payroll_role" || field === "default_cooking_station") && isPayrollRole(value)) {
      return PAYROLL_ROLE_LABELS[value];
    }
    if (field === "default_cooking_station" && isCookingStation(value)) {
      return COOKING_STATION_LABELS[value];
    }
    if (field === "status" && isEmployeeStatus(value)) {
      return EMPLOYEE_STATUS_LABELS[value];
    }
    if (field.includes("date") || field.startsWith("effective_")) {
      return formatDate(value);
    }
    if (field.endsWith("_at")) {
      return formatDateTime(value);
    }
    return value;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return "Нет";
    }
    if (value.every(isRecord)) {
      return value.map((item) => formatRecordValue(item)).join("; ");
    }
    return `${value.length} знач.`;
  }
  if (isRecord(value)) {
    return formatRecordValue(value);
  }
  return "Данные изменены";
}

function formatRecordValue(value: Record<string, unknown>) {
  const parts: string[] = [];
  const fullName = valueFromRecord(value, "full_name");
  if (fullName) {
    parts.push(fullName);
  }
  const payrollRole = valueFromRecord(value, "payroll_role");
  if (payrollRole) {
    parts.push(formatChangeValue("payroll_role", payrollRole));
  }
  const category = valueFromRecord(value, "category");
  if (category) {
    parts.push(formatChangeValue("category", category));
  }
  const position = valueFromRecord(value, "position");
  if (position) {
    parts.push(position);
  }
  if (value.is_primary === true) {
    parts.push("основная");
  }
  return parts.length > 0 ? parts.join(" · ") : "Данные изменены";
}

function valueFromRecord(value: Record<string, unknown> | null, field: string) {
  const fieldValue = value?.[field];
  return typeof fieldValue === "string" ? fieldValue : null;
}

function textValue(value: unknown) {
  return typeof value === "string" && value.trim() ? value : "Не указана";
}

function isHiddenChangeField(field: string) {
  const normalized = field.replace(/_/g, "").toLowerCase();
  return (
    normalized.includes("pin") ||
    field === "id" ||
    field.endsWith("_id") ||
    field === "created_at" ||
    field === "updated_at"
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function isEmployeeCategory(value: string): value is EmployeeCategory {
  return value in EMPLOYEE_CATEGORY_LABELS;
}

function isCookingStation(value: string): value is CookingStation {
  return value in COOKING_STATION_LABELS;
}

function isPayrollRole(value: string): value is PayrollRole {
  return value in PAYROLL_ROLE_LABELS;
}

function isEmployeeStatus(value: string): value is EmployeeStatus {
  return value in EMPLOYEE_STATUS_LABELS;
}
