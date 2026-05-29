import axios, { AxiosError, type InternalAxiosRequestConfig } from "axios";

import { clearSession, getAccessToken, setSession, type AuthUser } from "./auth";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";
const REQUEST_TIMEOUT_MS = 15_000;

type RetriableRequestConfig = InternalAxiosRequestConfig & { _retry?: boolean };

export type IntegrationDefinition = {
  code: string;
  name: string;
  pattern: string;
  script_path: string;
  status: string;
};

export type LoginResponse = {
  access_token: string;
  refresh_token: string;
  token_type: "bearer";
  user: AuthUser;
};

export type AppSetting = {
  id: string;
  key: string;
  value: unknown;
  value_type: string;
  category: string;
  display_name: string;
  description: string | null;
  widget_type: string;
  widget_options: Record<string, unknown> | null;
  unit: string | null;
  is_critical: boolean;
  updated_at: string;
  updated_by_user_id: string | null;
  updated_by_user_name: string | null;
};

export type AppSettingHistory = {
  id: string;
  setting_id: string;
  old_value: unknown;
  new_value: unknown;
  changed_at: string;
  changed_by_user_id: string | null;
  changed_by_user_name: string | null;
};

export type EmployeeStatus = "active" | "inactive" | "requires_setup";
export type EmployeeCategory =
  | "category_1"
  | "category_2"
  | "category_3"
  | "category_4"
  | "intern"
  | "freelancer";
export type CookingStation = "sushi" | "pizza" | "shawarma";
export type PayrollRole = CookingStation | "prep" | "administrator";

export type EmployeeRoleAssignment = {
  id: string;
  employee_id: string;
  payroll_role: PayrollRole;
  category: EmployeeCategory;
  is_primary: boolean;
  effective_from: string;
  effective_to: string | null;
  created_at: string;
  updated_at: string;
};

export type EmployeeRoleAssignmentCreate = {
  payroll_role: PayrollRole;
  category: EmployeeCategory;
  is_primary?: boolean;
  effective_from?: string | null;
};

export type EmployeeRoleAssignmentPatch = Partial<
  Pick<EmployeeRoleAssignment, "payroll_role" | "category" | "is_primary">
>;

export type Employee = {
  id: string;
  full_name: string;
  iiko_id: string;
  position: string | null;
  category: EmployeeCategory | null;
  default_cooking_station: CookingStation | null;
  is_senior: boolean;
  is_deputy_senior: boolean;
  status: EmployeeStatus;
  hire_date: string | null;
  fire_date: string | null;
  fire_reason: string | null;
  pin_set_at: string | null;
  iiko_sync_at: string | null;
  created_at: string;
  updated_at: string;
  assignments: EmployeeRoleAssignment[];
};

export type EmployeePatch = Partial<
  Pick<
    Employee,
    | "position"
    | "category"
    | "default_cooking_station"
    | "is_senior"
    | "is_deputy_senior"
    | "hire_date"
    | "fire_date"
  >
>;

export type EmployeeSyncResult = {
  created: number;
  updated: number;
  deactivated: number;
};

export type EmployeeDismissPayload = {
  fire_date?: string;
  reason?: string;
};

export type EmployeeCreatePayload = {
  full_name: string;
  pin_code: string;
  iiko_role_id: string;
  roles: Array<{
    payroll_role: PayrollRole;
    category: EmployeeCategory;
    is_primary: boolean;
  }>;
  is_senior?: boolean;
  is_deputy_senior?: boolean;
};

export type EmployeePinChangePayload = {
  pin_code: string;
};

export type IikoEmployeeRole = {
  id: string;
  name: string;
  code: string | null;
  deleted: boolean;
};

export type PayrollRoleCategoryOption = {
  code: EmployeeCategory;
  name: string;
};

export type PayrollPeriod = {
  id: string;
  period_type: "week";
  start_date: string;
  end_date: string;
  payroll_date: string;
  status: string;
  finalized_at: string | null;
  finalized_by_user_id: string | null;
};

export type PayrollRun = {
  id: string;
  period_id: string;
  started_at: string;
  finished_at: string | null;
  status: string;
  blocking_issues: Array<Record<string, unknown>>;
  summary: Record<string, unknown>;
  period: PayrollPeriod | null;
};

export type PayrollLine = {
  id: string;
  run_id: string;
  employee_id: string;
  role: string;
  base_pay: number;
  premium: number;
  percent_pay: number;
  fund_accrual: number;
  deduction: number;
  total_payable: number;
  components: Record<string, unknown>;
};

export type ShiftLedgerSource = "schedule" | "manual_correction" | "fallback_primary";
export type ShiftLedgerStatus = "resolved" | "needs_role_selection" | "needs_employee_setup";

export type ShiftLedgerAvailableRole = {
  payroll_role: PayrollRole | string;
  category: EmployeeCategory;
};

export type ShiftLedgerEntry = {
  id: string;
  work_date: string;
  employee_id: string;
  employee_name: string;
  employee_iiko_id: string;
  payroll_role: PayrollRole | string | null;
  category: EmployeeCategory | null;
  source: ShiftLedgerSource;
  opened_at: string;
  closed_at: string | null;
  notes: string | null;
  is_resolved: boolean;
  status: ShiftLedgerStatus;
  available_roles: ShiftLedgerAvailableRole[];
};

export type ShiftLedgerMatrixSummary = {
  earliest_open: string | null;
  latest_close: string | null;
  shift_count: number;
};

export type ShiftLedgerMatrixShift = {
  ledger_entry_id: string;
  opened_at: string;
  closed_at: string | null;
  payroll_role: PayrollRole | string | null;
  category: EmployeeCategory | null;
  is_resolved: boolean;
  status: ShiftLedgerStatus;
};

export type ShiftLedgerMatrixDay = {
  date: string;
  available_roles: ShiftLedgerAvailableRole[];
  summary: ShiftLedgerMatrixSummary;
  shifts: ShiftLedgerMatrixShift[];
};

export type ShiftLedgerMatrixEmployee = {
  id: string;
  full_name: string;
  iiko_id: string;
  days: ShiftLedgerMatrixDay[];
};

export type ShiftLedgerMatrix = {
  selected_date: string;
  start_date: string;
  end_date: string;
  days: Array<{ date: string; is_today: boolean }>;
  employees: ShiftLedgerMatrixEmployee[];
};

export type ShiftLedgerPatch = {
  payroll_role: PayrollRole | string;
};

export type PayrollRate = {
  id: string | null;
  position_group: string;
  category: string;
  station: string | null;
  rate_type: "daily" | "hourly" | "monthly";
  amount: number | null;
  is_active: boolean;
  is_enabled: boolean;
  effective_from: string | null;
  effective_to: string | null;
  created_at: string | null;
};

export type PayrollRatePayload = {
  position_group: string;
  category: string;
  station: string | null;
  rate_type: "daily" | "hourly" | "monthly";
  amount: number | null;
  is_active: boolean;
  effective_from: string;
  effective_to: string | null;
};

export type PayrollRoleCategoryAvailability = {
  position_group: string;
  category: string;
  is_enabled: boolean;
};

export type PayrollRoleCategoryAvailabilityPayload = {
  is_enabled: boolean;
};

export type PayrollRevenueShare = {
  id: string;
  position_group: string;
  category: string;
  percent: number;
  effective_from: string;
  effective_to: string | null;
  created_at: string;
};

export type PayrollRevenueSharePayload = Omit<PayrollRevenueShare, "id" | "created_at">;

export type PayrollRevenueTier = {
  id: string;
  min_revenue: number;
  max_revenue: number | null;
  rate_percent: number;
  effective_from: string;
  effective_to: string | null;
  created_at: string;
};

export type PayrollRevenueTierPayload = Omit<PayrollRevenueTier, "id" | "created_at">;

export type PayrollCategoryCoefficient = {
  id: string;
  category: EmployeeCategory;
  coefficient: number;
  effective_from: string;
  effective_to: string | null;
  created_at: string;
};

export type PayrollCategoryCoefficientPayload = Omit<
  PayrollCategoryCoefficient,
  "id" | "created_at"
>;

export type PayrollDeductionCategory = {
  id: string;
  code: string;
  display_name: string;
  description: string | null;
  type: "fine" | "withholding" | "deposit_writeoff";
  default_amount: number | null;
  effective_from: string;
  effective_to: string | null;
  created_at: string;
};

export type PayrollDeductionCategoryPayload = Omit<PayrollDeductionCategory, "id" | "created_at">;

export type PayrollSeniorityPremium = {
  id: string;
  role: "senior" | "deputy_senior";
  percent_of_base: number;
  effective_from: string;
  effective_to: string | null;
  created_at: string;
};

export type PayrollSeniorityPremiumPayload = Omit<PayrollSeniorityPremium, "id" | "created_at">;

export const api = axios.create({
  baseURL: `${API_BASE_URL}/api/v1`,
  timeout: REQUEST_TIMEOUT_MS,
  withCredentials: true,
});

let refreshPromise: Promise<string | null> | null = null;

api.interceptors.request.use((config) => {
  const token = getAccessToken();
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

api.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const config = error.config as RetriableRequestConfig | undefined;
    if (error.response?.status !== 401 || !config || config._retry) {
      throw error;
    }

    config._retry = true;
    refreshPromise ??= refreshAccessToken().finally(() => {
      refreshPromise = null;
    });
    const token = await refreshPromise;
    if (!token) {
      clearSession();
      if (window.location.pathname !== "/login") {
        window.history.pushState({}, "", "/login");
        window.dispatchEvent(new PopStateEvent("popstate"));
      }
      throw error;
    }

    config.headers.Authorization = `Bearer ${token}`;
    return api(config);
  },
);

async function refreshAccessToken() {
  try {
    const response = await axios.post<LoginResponse>(`${API_BASE_URL}/api/v1/auth/refresh`, null, {
      timeout: REQUEST_TIMEOUT_MS,
      withCredentials: true,
    });
    setSession(response.data.access_token, response.data.user);
    return response.data.access_token;
  } catch {
    clearSession();
    return null;
  }
}

export async function restoreSession() {
  return refreshAccessToken();
}

export async function login(email: string, password: string) {
  const response = await api.post<LoginResponse>("/auth/login", { email, password });
  setSession(response.data.access_token, response.data.user);
  return response.data;
}

export async function logout() {
  await api.post("/auth/logout");
  clearSession();
}

export async function getHealth(): Promise<{ status: string }> {
  const response = await api.get<{ status: string }>("/health");
  return response.data;
}

export async function getIntegrationDefinitions(): Promise<IntegrationDefinition[]> {
  const response = await api.get<IntegrationDefinition[]>("/integrations/definitions");
  return response.data;
}

export async function getSettings(category?: string): Promise<AppSetting[]> {
  const response = await api.get<AppSetting[]>("/settings", { params: { category } });
  return response.data;
}

export async function getSettingHistory(key: string): Promise<AppSettingHistory[]> {
  const response = await api.get<AppSettingHistory[]>(
    `/settings/${encodeURIComponent(key)}/history`,
  );
  return response.data;
}

export async function updateSetting(key: string, value: unknown): Promise<AppSetting> {
  const response = await api.put<AppSetting>(`/settings/${encodeURIComponent(key)}`, { value });
  return response.data;
}

export async function getEmployees(filters: {
  status?: EmployeeStatus | "all";
  category?: EmployeeCategory;
  cookingStation?: CookingStation;
}): Promise<Employee[]> {
  const response = await api.get<Employee[]>("/employees/", {
    params: {
      status: filters.status === "all" ? undefined : filters.status,
      category: filters.category,
      cooking_station: filters.cookingStation,
    },
  });
  return response.data;
}

export async function patchEmployee(id: string, patch: EmployeePatch): Promise<Employee> {
  const response = await api.patch<Employee>(`/employees/${id}`, patch);
  return response.data;
}

export async function getIikoEmployeeRoles(): Promise<IikoEmployeeRole[]> {
  const response = await api.get<IikoEmployeeRole[]>("/employees/iiko-roles");
  return response.data;
}

export async function createEmployee(payload: EmployeeCreatePayload): Promise<Employee> {
  const response = await api.post<Employee>("/employees/", payload);
  return response.data;
}

export async function changeEmployeePin(
  id: string,
  payload: EmployeePinChangePayload,
): Promise<Employee> {
  const response = await api.post<Employee>(`/employees/${id}/pin`, payload);
  return response.data;
}

export async function dismissEmployee(
  id: string,
  payload: EmployeeDismissPayload,
): Promise<Employee> {
  const response = await api.post<Employee>(`/employees/${id}/dismiss`, payload);
  return response.data;
}

export async function reinstateEmployee(id: string): Promise<Employee> {
  const response = await api.post<Employee>(`/employees/${id}/reinstate`);
  return response.data;
}

export async function getEmployeeAssignments(id: string): Promise<EmployeeRoleAssignment[]> {
  const response = await api.get<EmployeeRoleAssignment[]>(`/employees/${id}/assignments`);
  return response.data;
}

export async function createEmployeeAssignment(
  id: string,
  payload: EmployeeRoleAssignmentCreate,
): Promise<EmployeeRoleAssignment> {
  const response = await api.post<EmployeeRoleAssignment>(`/employees/${id}/assignments`, payload);
  return response.data;
}

export async function patchEmployeeAssignment(
  employeeId: string,
  assignmentId: string,
  payload: EmployeeRoleAssignmentPatch,
): Promise<EmployeeRoleAssignment> {
  const response = await api.patch<EmployeeRoleAssignment>(
    `/employees/${employeeId}/assignments/${assignmentId}`,
    payload,
  );
  return response.data;
}

export async function deleteEmployeeAssignment(
  employeeId: string,
  assignmentId: string,
): Promise<void> {
  await api.delete(`/employees/${employeeId}/assignments/${assignmentId}`);
}

export async function syncEmployees(): Promise<EmployeeSyncResult> {
  const response = await api.post<EmployeeSyncResult>("/employees/sync");
  return response.data;
}

export async function getPayrollRoleCategories(): Promise<
  Partial<Record<PayrollRole, PayrollRoleCategoryOption[]>>
> {
  const response =
    await api.get<Partial<Record<PayrollRole, PayrollRoleCategoryOption[]>>>(
      "/payroll/role-categories",
    );
  return response.data;
}

export async function autoCreateNextPayrollPeriod(): Promise<PayrollPeriod> {
  const response = await api.post<PayrollPeriod>("/payroll/periods/auto-create-next");
  return response.data;
}

export async function createPayrollRun(periodId: string): Promise<PayrollRun> {
  const response = await api.post<PayrollRun>("/payroll/runs", { period_id: periodId });
  return response.data;
}

export async function getPayrollRuns(): Promise<PayrollRun[]> {
  const response = await api.get<PayrollRun[]>("/payroll/runs");
  return response.data;
}

export async function getPayrollRun(id: string): Promise<PayrollRun> {
  const response = await api.get<PayrollRun>(`/payroll/runs/${id}`);
  return response.data;
}

export async function getPayrollRunLines(id: string): Promise<PayrollLine[]> {
  const response = await api.get<PayrollLine[]>(`/payroll/runs/${id}/lines`);
  return response.data;
}

export async function finalizePayrollRun(id: string): Promise<PayrollRun> {
  const response = await api.post<PayrollRun>(`/payroll/runs/${id}/finalize`);
  return response.data;
}

export async function getShiftLedger(workDate: string): Promise<ShiftLedgerEntry[]> {
  const response = await api.get<ShiftLedgerEntry[]>("/shifts/ledger", {
    params: { date: workDate },
  });
  return response.data;
}

export async function buildShiftLedger(workDate: string): Promise<ShiftLedgerEntry[]> {
  const response = await api.post<ShiftLedgerEntry[]>("/shifts/ledger/build", {
    work_date: workDate,
  });
  return response.data;
}

export async function getShiftLedgerMatrix(workDate: string): Promise<ShiftLedgerMatrix> {
  const response = await api.get<ShiftLedgerMatrix>("/shifts/ledger/matrix", {
    params: { date: workDate },
  });
  return response.data;
}

export async function buildShiftLedgerWeek(workDate: string): Promise<ShiftLedgerMatrix> {
  const response = await api.post<ShiftLedgerMatrix>("/shifts/ledger/build-week", {
    work_date: workDate,
  });
  return response.data;
}

export async function patchShiftLedgerEntry(
  id: string,
  payload: ShiftLedgerPatch,
): Promise<ShiftLedgerEntry> {
  const response = await api.patch<ShiftLedgerEntry>(`/shifts/ledger/${id}`, payload);
  return response.data;
}

export async function getPayrollRates(
  history = false,
  includeDisabled = false,
): Promise<PayrollRate[]> {
  const response = await api.get<PayrollRate[]>("/payroll/config/rates", {
    params: {
      history: history || undefined,
      include_disabled: includeDisabled || undefined,
    },
  });
  return response.data;
}

export async function putPayrollRate(payload: PayrollRatePayload): Promise<PayrollRate> {
  const response = await api.put<PayrollRate>("/payroll/config/rates", payload);
  return response.data;
}

export async function getPayrollRateAvailability(): Promise<PayrollRoleCategoryAvailability[]> {
  const response = await api.get<PayrollRoleCategoryAvailability[]>("/payroll/config/availability");
  return response.data;
}

export async function putPayrollRateAvailability(
  positionGroup: string,
  category: string,
  payload: PayrollRoleCategoryAvailabilityPayload,
): Promise<PayrollRoleCategoryAvailability> {
  const response = await api.put<PayrollRoleCategoryAvailability>(
    `/payroll/config/availability/${encodeURIComponent(positionGroup)}/${encodeURIComponent(category)}`,
    payload,
  );
  return response.data;
}

export async function getPayrollRevenueShares(history = false): Promise<PayrollRevenueShare[]> {
  const response = await api.get<PayrollRevenueShare[]>("/payroll/config/revenue-share", {
    params: { history: history || undefined },
  });
  return response.data;
}

export async function putPayrollRevenueShare(
  payload: PayrollRevenueSharePayload,
): Promise<PayrollRevenueShare> {
  const response = await api.put<PayrollRevenueShare>("/payroll/config/revenue-share", payload);
  return response.data;
}

export async function getPayrollRevenueTiers(history = false): Promise<PayrollRevenueTier[]> {
  const response = await api.get<PayrollRevenueTier[]>("/payroll/config/revenue-tiers", {
    params: { history: history || undefined },
  });
  return response.data;
}

export async function putPayrollRevenueTiers(
  payload: PayrollRevenueTierPayload[],
): Promise<PayrollRevenueTier[]> {
  const response = await api.put<PayrollRevenueTier[]>("/payroll/config/revenue-tiers", payload);
  return response.data;
}

export async function getPayrollCategoryCoefficients(
  history = false,
): Promise<PayrollCategoryCoefficient[]> {
  const response = await api.get<PayrollCategoryCoefficient[]>(
    "/payroll/config/category-coefficients",
    {
      params: { history: history || undefined },
    },
  );
  return response.data;
}

export async function putPayrollCategoryCoefficients(
  payload: PayrollCategoryCoefficientPayload[],
): Promise<PayrollCategoryCoefficient[]> {
  const response = await api.put<PayrollCategoryCoefficient[]>(
    "/payroll/config/category-coefficients",
    payload,
  );
  return response.data;
}

export async function getPayrollDeductions(history = false): Promise<PayrollDeductionCategory[]> {
  const response = await api.get<PayrollDeductionCategory[]>("/payroll/config/deductions", {
    params: { history: history || undefined },
  });
  return response.data;
}

export async function putPayrollDeduction(
  payload: PayrollDeductionCategoryPayload,
): Promise<PayrollDeductionCategory> {
  const response = await api.put<PayrollDeductionCategory>("/payroll/config/deductions", payload);
  return response.data;
}

export async function getPayrollSeniorityPremiums(
  history = false,
): Promise<PayrollSeniorityPremium[]> {
  const response = await api.get<PayrollSeniorityPremium[]>("/payroll/config/seniority-premium", {
    params: { history: history || undefined },
  });
  return response.data;
}

export async function putPayrollSeniorityPremium(
  payload: PayrollSeniorityPremiumPayload,
): Promise<PayrollSeniorityPremium> {
  const response = await api.put<PayrollSeniorityPremium>(
    "/payroll/config/seniority-premium",
    payload,
  );
  return response.data;
}

export function apiErrorMessage(error: unknown, fallback = "Не удалось выполнить запрос") {
  if (axios.isAxiosError(error)) {
    const data = error.response?.data as { detail?: unknown } | undefined;
    const detail = data?.detail;
    if (typeof detail === "string" && detail.trim()) {
      return detail;
    }
    if (detail && typeof detail === "object") {
      return JSON.stringify(detail);
    }
  }
  return error instanceof Error ? error.message : fallback;
}
