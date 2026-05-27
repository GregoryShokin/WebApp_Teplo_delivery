import axios, { AxiosError, type InternalAxiosRequestConfig } from "axios";

import { clearSession, getAccessToken, setSession, type AuthUser } from "./auth";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

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
  description: string | null;
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

export type EmployeeStatus = "active" | "inactive" | "needs_setup";

export type Employee = {
  id: string;
  full_name: string;
  iiko_id: string;
  position: string | null;
  category: string | null;
  is_senior: boolean;
  is_deputy_senior: boolean;
  status: EmployeeStatus;
  hire_date: string | null;
  fire_date: string | null;
  iiko_sync_at: string | null;
  created_at: string;
  updated_at: string;
};

export type EmployeePatch = Partial<
  Pick<
    Employee,
    "position" | "category" | "is_senior" | "is_deputy_senior" | "status" | "hire_date" | "fire_date"
  >
>;

export type EmployeeSyncResult = {
  created: number;
  updated: number;
  deactivated: number;
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

export const api = axios.create({
  baseURL: `${API_BASE_URL}/api/v1`,
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
    const response = await axios.post<LoginResponse>(
      `${API_BASE_URL}/api/v1/auth/refresh`,
      null,
      { withCredentials: true },
    );
    setSession(response.data.access_token, response.data.user);
    return response.data.access_token;
  } catch {
    clearSession();
    return null;
  }
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
  const response = await api.get<AppSettingHistory[]>(`/settings/${encodeURIComponent(key)}/history`);
  return response.data;
}

export async function updateSetting(key: string, value: unknown): Promise<AppSetting> {
  const response = await api.put<AppSetting>(`/settings/${encodeURIComponent(key)}`, { value });
  return response.data;
}

export async function getEmployees(filters: {
  status?: EmployeeStatus | "all";
  category?: string;
}): Promise<Employee[]> {
  const response = await api.get<Employee[]>("/employees/", {
    params: {
      status: filters.status === "all" ? undefined : filters.status,
      category: filters.category,
    },
  });
  return response.data;
}

export async function patchEmployee(id: string, patch: EmployeePatch): Promise<Employee> {
  const response = await api.patch<Employee>(`/employees/${id}`, patch);
  return response.data;
}

export async function syncEmployees(): Promise<EmployeeSyncResult> {
  const response = await api.post<EmployeeSyncResult>("/employees/sync");
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
