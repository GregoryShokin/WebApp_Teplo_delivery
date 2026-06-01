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

export type EmployeeNoticeInfo = {
  notice_date: string;
  days_since: number;
  will_trigger_full_payout: boolean;
};

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
  tenure_started_at: string | null;
  fire_date: string | null;
  fire_reason: string | null;
  pin_assumed_from_iiko: boolean;
  pin_set_at: string | null;
  iiko_sync_at: string | null;
  created_at: string;
  updated_at: string;
  assignments: EmployeeRoleAssignment[];
  active_notice: EmployeeNoticeInfo | null;
};

export type EmployeePatch = Partial<
  Pick<
    Employee,
    | "full_name"
    | "position"
    | "category"
    | "default_cooking_station"
    | "is_senior"
    | "is_deputy_senior"
    | "hire_date"
    | "fire_date"
  >
> & {
  pin_code?: string | null;
  roles?: Array<{
    id?: string | null;
    payroll_role: PayrollRole;
    category: EmployeeCategory;
    is_primary: boolean;
  }>;
};

export type EmployeeSyncResult = {
  created: number;
  updated: number;
  deactivated: number;
};

export type DepositDismissAction = "payout_full" | "payout_partial" | "write_off" | "none";

export type EmployeeDismissPayload = {
  fire_date?: string;
  reason_id?: string;
  reason_code?: string;
  comment?: string;
  reason?: string;
  deposit_action: DepositDismissAction;
  deposit_payout_amount?: string;
  deposit_comment?: string;
};

export type EmployeeNoticePayload = {
  notice_date?: string;
  comment?: string;
};

export type EmployeeNoticeCancelPayload = {
  comment?: string;
};

export type EmployeeNoticeAction = {
  event_id: string;
  effective_from: string;
  days_until_today: number;
};

export type EmployeeDismissalReason = {
  id: string;
  code: string;
  label: string;
  requires_comment: boolean;
  is_system: boolean;
  is_active: boolean;
  sort_order: number;
  created_at: string;
  updated_at: string;
};

export type EmployeeDismissalReasonCreatePayload = {
  code?: string;
  label: string;
  requires_comment?: boolean;
  is_active?: boolean;
  sort_order?: number;
};

export type EmployeeDismissalReasonUpdatePayload = Partial<
  Pick<EmployeeDismissalReason, "label" | "requires_comment" | "is_active" | "sort_order">
>;

export type EmployeeChangeSource = "app" | "iiko_sync" | "system_migration";
export type EmployeeChangeStatus = "success" | "error" | "requires_review" | "skipped";

export type EmployeeChangeEvent = {
  id: string;
  employee_id: string | null;
  changed_at: string;
  effective_from: string | null;
  effective_to: string | null;
  change_type: string;
  source: EmployeeChangeSource;
  actor_user_id: string | null;
  actor_label: string | null;
  status: EmployeeChangeStatus;
  summary: string;
  before_value: Record<string, unknown> | null;
  after_value: Record<string, unknown> | null;
  diff: Record<string, unknown> | null;
  reason_id: string | null;
  reason: string | null;
  reason_code: string | null;
  reason_label: string | null;
  comment: string | null;
  related_agent_run_id: string | null;
  related_agent_action_id: string | null;
  related_entity_type: string | null;
  related_entity_id: string | null;
  payroll_impact: boolean;
  payroll_impact_metadata: Record<string, unknown>;
  created_at: string;
};

export type EmployeeChangeFilters = {
  employeeId?: string;
  changedFrom?: string;
  changedTo?: string;
  effectiveFrom?: string;
  effectiveTo?: string;
  changeType?: string;
  source?: EmployeeChangeSource;
  actor?: string;
  status?: EmployeeChangeStatus;
  onlyErrors?: boolean;
  onlyRequiresReview?: boolean;
  includeSystemMigrations?: boolean;
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

export type EmployeeHireDatePayload = {
  hire_date: string;
  comment?: string;
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
  deposit_withholding: number;
  deposit_payout: number;
  ndfl_deduction: number;
  total_payable: number;
  deposit_excluded_for_run: boolean;
  deposit_exclusion_reason: string | null;
  components: Record<string, unknown>;
};

export type PayrollLineDepositOverridePatch = {
  deposit_excluded_for_run: boolean;
  deposit_exclusion_reason?: string | null;
};

export type AccumulationFundStatus = "active" | "paid_out" | "forfeited";

export type AccumulationFundSummary = {
  year: number;
  total_outstanding: string;
  total_paid_out_ytd: string;
  total_forfeited_ytd: string;
  active_employees_count: number;
  next_payout_date: string;
};

export type AccumulationFundAccount = {
  id: string;
  employee_id: string;
  full_name: string;
  position: string | null;
  year: number;
  status: AccumulationFundStatus;
  tenure_months: number;
  tenure_started_at: string | null;
  current_rate_percent: string;
  accumulated: string;
  paid_out: string;
  forfeited: string;
  outstanding: string;
  paid_out_at: string | null;
  forfeited_at: string | null;
  forfeit_reason: string | null;
  planned_payout_date: string;
};

export type AccumulationFundTransaction = {
  id: string;
  account_id: string;
  employee_id: string;
  year: number;
  transaction_type: "accrual" | "payout" | "forfeit" | "initial_balance";
  amount: string;
  rate_percent: string | null;
  base_pay_amount: string | null;
  run_id: string | null;
  comment: string | null;
  created_at: string | null;
  account_status: AccumulationFundStatus | null;
};

export type AccumulationFundEmployee = {
  id: string;
  full_name: string;
  position: string | null;
  status: EmployeeStatus;
  hire_date: string | null;
  tenure_started_at: string | null;
  tenure_months: number;
  current_rate_percent: string;
  next_threshold_months: number | null;
  next_threshold_date: string | null;
  next_rate_percent: string | null;
};

export type AccumulationFundEmployeeDetail = {
  employee: AccumulationFundEmployee;
  account: AccumulationFundAccount | null;
  accounts: AccumulationFundAccount[];
  transactions: AccumulationFundTransaction[];
};

export type AccumulationFundPayoutResult = {
  year: number;
  paid_out_count: number;
  total_paid_out: string;
};

export type FundTierItem = {
  min_months: number;
  rate: number | string;
};

export type FundTiersRead = {
  tiers: FundTierItem[];
  updated_at: string | null;
  updated_by_label: string | null;
};

export type FundRosterAccount = {
  year: number;
  accumulated: string;
  is_initial_set: boolean;
  initial_set_at: string | null;
  initial_set_by_label: string | null;
};

export type FundExclusionRead = {
  employee_id: string;
  fund_excluded: boolean;
  fund_excluded_until: string | null;
  fund_excluded_reason: string | null;
  is_currently_excluded: boolean;
};

export type FundRosterRow = {
  employee_id: string;
  full_name: string;
  position: string | null;
  hire_date: string | null;
  tenure_months: number | null;
  current_rate_percent: string | null;
  fund_exclusion: FundExclusionRead;
  fund_account: FundRosterAccount | null;
};

export type FundInitialBalanceRead = {
  employee_id: string;
  year: number;
  accumulated: string;
  transaction_id: string;
  created_at: string;
};

export type PayrollAdjustmentType = "bonus" | "penalty";

export type PayrollAdjustmentCategory = {
  id: string;
  type: PayrollAdjustmentType;
  code: string;
  display_name: string;
  description: string | null;
  default_amount: string | null;
  is_active: boolean;
  sort_order: number;
  created_at: string | null;
  updated_at: string | null;
};

export type PayrollAdjustment = {
  id: string;
  employee_id: string;
  employee_full_name: string;
  employee_position: string;
  work_date: string;
  type: PayrollAdjustmentType;
  category_id: string | null;
  category_display_name: string | null;
  custom_label: string | null;
  amount: string;
  comment: string | null;
  created_by_user_id: string | null;
  created_by_label: string | null;
  created_at: string | null;
  updated_at: string | null;
  is_locked: boolean;
};

export type PayrollAdjustmentFilters = {
  employeeId?: string;
  dateFrom?: string;
  dateTo?: string;
  type?: PayrollAdjustmentType | "all";
};

export type PayrollAdjustmentPayload = {
  employee_id: string;
  work_date: string;
  type: PayrollAdjustmentType;
  category_id?: string | null;
  custom_label?: string | null;
  amount: string;
  comment?: string | null;
};

export type PayrollAdjustmentPatch = Partial<PayrollAdjustmentPayload>;

export type PayrollAdjustmentCategoryPayload = {
  type: PayrollAdjustmentType;
  code?: string;
  display_name: string;
  default_amount?: string | null;
  description?: string | null;
  sort_order?: number;
};

export type PayrollAdjustmentCategoryPatch = Partial<
  Pick<
    PayrollAdjustmentCategory,
    "display_name" | "default_amount" | "description" | "is_active" | "sort_order"
  >
>;

export type ScheduleStatus = "draft" | "published" | "superseded";

export type ScheduledShiftRead = {
  id: string;
  business_date: string;
  employee_id: string;
  employee_full_name: string;
  payroll_role: string;
  station_code: string | null;
  planned_start_at: string;
  planned_end_at: string;
  planned_hours: string | number;
  comment_private: string | null;
};

export type ScheduleRead = {
  id: string;
  date_start: string;
  date_end: string;
  status: ScheduleStatus;
  notes: string | null;
  published_at: string | null;
  superseded_by_id: string | null;
  created_by_label: string | null;
  shifts: ScheduledShiftRead[];
};

export type ScheduleCreatePayload = {
  date_start: string;
  date_end: string;
  notes?: string | null;
};

export type SchedulePatchPayload = {
  notes?: string | null;
};

export type RevenueForecastQualityStatus = "ok" | "requires_review" | "manual_override";

export type RevenueForecastHistoryPoint = {
  date: string;
  amount: string | number | null;
  included: boolean;
};

export type RevenueForecastRead = {
  business_date: string;
  weekday: number;
  method_code: string;
  history_window_weeks: number;
  history_points: RevenueForecastHistoryPoint[];
  base_average_amount: string | number | null;
  season_coeff: string | number;
  event_coeff: string | number;
  manual_override_amount: string | number | null;
  manual_override_reason: string | null;
  manual_override_set_by_label: string | null;
  manual_override_set_at: string | null;
  forecast_amount: string | number | null;
  quality_status: RevenueForecastQualityStatus;
  event_review_recommended: boolean;
  computed_at: string | null;
};

export type RevenueForecastOverridePayload = {
  amount: number;
  reason?: string | null;
};

export type RevenueForecastRecomputePayload = {
  date_from: string;
  date_to: string;
  force_refresh_iiko?: boolean;
};

export type ShiftCostQualityStatus = "ok" | "requires_review";

export type ShiftCostEstimateRead = {
  id: string;
  scheduled_shift_id: string;
  business_date: string;
  employee_id: string;
  employee_full_name: string;
  planned_hours: string | number;
  base_salary_estimate: string | number;
  weekday_premium_estimate: string | number;
  allowance_estimate: string | number;
  revenue_percent_estimate: string | number;
  fund_accrual_estimate: string | number;
  total_cost_estimate: string | number;
  quality_status: ShiftCostQualityStatus;
  quality_reasons: string[];
  breakdown: Record<string, unknown>;
};

export type PayrollForecastRunRead = {
  id: string;
  shift_schedule_id: string;
  run_at: string;
  run_by_label: string | null;
  status: "draft" | "completed" | "superseded";
  total_revenue_forecast: string | number | null;
  total_shift_cost_estimate: string | number | null;
  fot_to_revenue_pct: string | number | null;
  fot_warning_threshold_pct: string | number;
  shifts_total: number;
  shifts_with_warnings: number;
  estimates: ShiftCostEstimateRead[];
};

export type PlanFactDeviationStatus =
  | "no_data"
  | "within_threshold"
  | "over_threshold"
  | "plan_no_fact"
  | "fact_no_plan";

export type PlanFactDayRowRead = {
  business_date: string;
  planned_shifts: number;
  planned_hours: string | number;
  planned_cost: string | number | null;
  planned_revenue: string | number | null;
  actual_shifts: number;
  actual_hours: string | number | null;
  actual_cost: string | number | null;
  actual_revenue: string | number | null;
  hours_deviation_pct: string | number | null;
  cost_deviation_pct: string | number | null;
  revenue_deviation_pct: string | number | null;
  deviation_status: PlanFactDeviationStatus;
};

export type PlanFactEmployeeRowRead = {
  employee_id: string;
  full_name: string;
  position: string;
  planned_shifts: number;
  planned_hours: string | number;
  planned_cost: string | number | null;
  actual_shifts: number;
  actual_hours: string | number | null;
  actual_cost: string | number | null;
  hours_deviation_pct: string | number | null;
  cost_deviation_pct: string | number | null;
  deviation_status: PlanFactDeviationStatus;
};

export type PlanFactTotals = {
  total_shifts: number;
  total_hours: string | number | null;
  total_cost: string | number | null;
  total_revenue: string | number | null;
  fot_pct: string | number | null;
  cost_status?: "ok" | "no_cost_forecast" | string;
};

export type PlanFactSummaryRead = {
  schedule: {
    id: string;
    date_start: string;
    date_end: string;
    status: ScheduleStatus;
  };
  fact_availability: "full" | "partial" | "none";
  covered_dates: string[];
  planned: PlanFactTotals;
  actual: PlanFactTotals | null;
  deviation: {
    shifts_pct: string | number | null;
    hours_pct: string | number | null;
    cost_pct: string | number | null;
    revenue_pct: string | number | null;
    fot_pct_diff: string | number | null;
  } | null;
  warning_threshold_pct: string | number;
  by_date: PlanFactDayRowRead[];
  by_employee: PlanFactEmployeeRowRead[];
  sources: Record<string, unknown>;
};

export type ScheduledShiftUpsertPayload = {
  business_date: string;
  employee_id: string;
  payroll_role?: string | null;
  station_code?: string | null;
  planned_start_at?: string | null;
  planned_end_at?: string | null;
  comment_private?: string | null;
};

export type EmployeeRosterAvailableRole = {
  payroll_role: string;
  category: string;
  is_primary: boolean;
  default_station_code: string | null;
};

export type EmployeeRosterRow = {
  id: string;
  full_name: string;
  position: "Повар" | "Кассир" | string;
  primary_payroll_role: string | null;
  default_cooking_station: string | null;
  available_roles: EmployeeRosterAvailableRole[];
  allowances: {
    senior: boolean;
    deputy: boolean;
  };
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
  payroll_locked: boolean;
};

export type ShiftLedgerMatrixDay = {
  date: string;
  payroll_locked: boolean;
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

export type DepositListItem = {
  id: string;
  full_name: string;
  position: string | null;
  category: EmployeeCategory | null;
  balance: string;
  initial_balance: string;
  target: string | null;
  withholding: string | null;
  is_excluded: boolean;
  excluded_until: string | null;
  progress_pct: string;
  deposit_target_override?: string | null;
  deposit_withholding_override?: string | null;
  deposit_excluded_reason?: string | null;
};

export type DepositTransaction = {
  id: string;
  employee_id: string;
  run_id: string | null;
  transaction_type: string;
  amount: string;
  created_at: string | null;
  comment?: string | null;
  reason?: string | null;
};

export type DepositConfigPatch = {
  deposit_target_override?: string | null;
  deposit_withholding_override?: string | null;
  deposit_excluded?: boolean;
  deposit_excluded_until?: string | null;
  deposit_excluded_reason?: string | null;
};

export type DepositPayoutPayload = {
  amount: string;
  comment?: string | null;
};

export type DepositWriteoffPayload = {
  amount: string;
  reason?: string | null;
};

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

export async function setEmployeeHireDate(
  employeeId: string,
  payload: EmployeeHireDatePayload,
): Promise<Employee> {
  const response = await api.post<Employee>(`/employees/${employeeId}/hire-date`, payload);
  return response.data;
}

export async function dismissEmployee(
  id: string,
  payload: EmployeeDismissPayload,
): Promise<Employee> {
  const response = await api.post<Employee>(`/employees/${id}/dismiss`, payload);
  return response.data;
}

export async function recordEmployeeNotice(
  id: string,
  payload: EmployeeNoticePayload,
): Promise<EmployeeNoticeAction> {
  const response = await api.post<EmployeeNoticeAction>(`/employees/${id}/notice`, payload);
  return response.data;
}

export async function cancelEmployeeNotice(
  id: string,
  payload: EmployeeNoticeCancelPayload,
): Promise<EmployeeNoticeAction> {
  const response = await api.delete<EmployeeNoticeAction>(`/employees/${id}/notice`, {
    data: payload,
  });
  return response.data;
}

export async function getEmployeeDismissalReasons(
  includeInactive = false,
): Promise<EmployeeDismissalReason[]> {
  const response = await api.get<EmployeeDismissalReason[]>("/employees/dismissal-reasons", {
    params: { include_inactive: includeInactive || undefined },
  });
  return response.data;
}

export async function getEmployeeChanges(
  filters: EmployeeChangeFilters = {},
): Promise<EmployeeChangeEvent[]> {
  const response = await api.get<EmployeeChangeEvent[]>("/employees/changes", {
    params: {
      employee_id: filters.employeeId,
      changed_from: filters.changedFrom,
      changed_to: filters.changedTo,
      effective_from: filters.effectiveFrom,
      effective_to: filters.effectiveTo,
      change_type: filters.changeType,
      source: filters.source,
      actor: filters.actor,
      status: filters.status,
      only_errors: filters.onlyErrors || undefined,
      only_requires_review: filters.onlyRequiresReview || undefined,
      include_system_migrations: filters.includeSystemMigrations || undefined,
    },
  });
  return response.data;
}

export async function createEmployeeDismissalReason(
  payload: EmployeeDismissalReasonCreatePayload,
): Promise<EmployeeDismissalReason> {
  const response = await api.post<EmployeeDismissalReason>("/employees/dismissal-reasons", payload);
  return response.data;
}

export async function updateEmployeeDismissalReason(
  id: string,
  payload: EmployeeDismissalReasonUpdatePayload,
): Promise<EmployeeDismissalReason> {
  const response = await api.patch<EmployeeDismissalReason>(
    `/employees/dismissal-reasons/${id}`,
    payload,
  );
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
  const response = await api.get<Partial<Record<PayrollRole, PayrollRoleCategoryOption[]>>>(
    "/payroll/role-categories",
  );
  return response.data;
}

export async function autoCreateNextPayrollPeriod(): Promise<PayrollPeriod> {
  const response = await api.post<PayrollPeriod>("/payroll/periods/auto-create-next");
  return response.data;
}

export async function createPayrollRun(
  periodId: string,
  options: { forceRefresh?: boolean } = {},
): Promise<PayrollRun> {
  // По умолчанию forceRefresh=true: пользователь жмёт «Пересчитать» когда хочет
  // увидеть актуальные данные, в том числе подтянуть свежую выручку из iiko.
  const force_refresh = options.forceRefresh ?? true;
  const response = await api.post<PayrollRun>("/payroll/runs", {
    period_id: periodId,
    force_refresh,
  });
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

export async function patchPayrollLineDepositOverride(
  id: string,
  payload: PayrollLineDepositOverridePatch,
): Promise<PayrollLine> {
  const response = await api.patch<PayrollLine>(`/payroll/lines/${id}`, payload);
  return response.data;
}

export async function finalizePayrollRun(id: string): Promise<PayrollRun> {
  const response = await api.post<PayrollRun>(`/payroll/runs/${id}/finalize`);
  return response.data;
}

export async function getAccumulationFundSummary(year: number): Promise<AccumulationFundSummary> {
  const response = await api.get<AccumulationFundSummary>("/payroll/fund/summary", {
    params: { year },
  });
  return response.data;
}

export async function getAccumulationFundAccounts(
  year: number,
): Promise<AccumulationFundAccount[]> {
  const response = await api.get<AccumulationFundAccount[]>("/payroll/fund", {
    params: { year },
  });
  return response.data;
}

export async function getEmployeeAccumulationFund(
  employeeId: string,
  year?: number,
): Promise<AccumulationFundEmployeeDetail> {
  const response = await api.get<AccumulationFundEmployeeDetail>(`/payroll/fund/${employeeId}`, {
    params: { year },
  });
  return response.data;
}

export async function postAccumulationFundPayout(
  year: number,
  payload: { comment?: string | null } = {},
): Promise<AccumulationFundPayoutResult> {
  const response = await api.post<AccumulationFundPayoutResult>(
    `/payroll/fund/payout/${year}`,
    payload,
  );
  return response.data;
}

export async function getFundTiers(): Promise<FundTiersRead> {
  const response = await api.get<FundTiersRead>("/payroll/fund/tiers");
  return response.data;
}

export async function putFundTiers(tiers: FundTierItem[]): Promise<FundTiersRead> {
  const response = await api.put<FundTiersRead>("/payroll/fund/tiers", { tiers });
  return response.data;
}

export async function getFundInitialBalanceRoster(year: number): Promise<FundRosterRow[]> {
  const response = await api.get<FundRosterRow[]>("/payroll/fund/initial-balance-roster", {
    params: { year },
  });
  return response.data;
}

export async function setFundInitialBalance(
  employeeId: string,
  payload: { amount: number; comment?: string },
): Promise<FundInitialBalanceRead> {
  const response = await api.post<FundInitialBalanceRead>(
    `/payroll/fund/${employeeId}/initial-balance`,
    payload,
  );
  return response.data;
}

export async function patchFundExclusion(
  employeeId: string,
  payload: {
    fund_excluded: boolean;
    fund_excluded_until?: string | null;
    fund_excluded_reason?: string | null;
  },
): Promise<FundExclusionRead> {
  const response = await api.patch<FundExclusionRead>(
    `/payroll/fund/${employeeId}/exclusion`,
    payload,
  );
  return response.data;
}

export async function getPayrollAdjustments(
  filters: PayrollAdjustmentFilters = {},
): Promise<PayrollAdjustment[]> {
  const response = await api.get<PayrollAdjustment[]>("/payroll/adjustments", {
    params: {
      employee_id: filters.employeeId,
      date_from: filters.dateFrom || undefined,
      date_to: filters.dateTo || undefined,
      type: filters.type && filters.type !== "all" ? filters.type : undefined,
    },
  });
  return response.data;
}

export async function createPayrollAdjustment(
  payload: PayrollAdjustmentPayload,
): Promise<PayrollAdjustment> {
  const response = await api.post<PayrollAdjustment>("/payroll/adjustments", payload);
  return response.data;
}

export async function patchPayrollAdjustment(
  id: string,
  payload: PayrollAdjustmentPatch,
): Promise<PayrollAdjustment> {
  const response = await api.patch<PayrollAdjustment>(`/payroll/adjustments/${id}`, payload);
  return response.data;
}

export async function deletePayrollAdjustment(id: string): Promise<void> {
  await api.delete(`/payroll/adjustments/${id}`);
}

export async function listSchedules(params?: {
  status?: ScheduleStatus | string;
  date_from?: string;
  date_to?: string;
}): Promise<ScheduleRead[]> {
  const response = await api.get<ScheduleRead[]>("/schedule", { params });
  return response.data;
}

export async function getSchedule(id: string): Promise<ScheduleRead> {
  const response = await api.get<ScheduleRead>(`/schedule/${id}`);
  return response.data;
}

export async function createSchedule(payload: ScheduleCreatePayload): Promise<ScheduleRead> {
  const response = await api.post<ScheduleRead>("/schedule", payload);
  return response.data;
}

export async function updateSchedule(
  id: string,
  payload: SchedulePatchPayload,
): Promise<ScheduleRead> {
  const response = await api.patch<ScheduleRead>(`/schedule/${id}`, payload);
  return response.data;
}

export async function publishSchedule(id: string): Promise<ScheduleRead> {
  const response = await api.post<ScheduleRead>(`/schedule/${id}/publish`);
  return response.data;
}

export async function createNewVersion(id: string): Promise<ScheduleRead> {
  const response = await api.post<ScheduleRead>(`/schedule/${id}/new-version`);
  return response.data;
}

export async function deleteSchedule(id: string): Promise<void> {
  await api.delete(`/schedule/${id}`);
}

export async function upsertShift(
  scheduleId: string,
  payload: ScheduledShiftUpsertPayload,
): Promise<ScheduledShiftRead> {
  const response = await api.post<ScheduledShiftRead>(`/schedule/${scheduleId}/shifts`, payload);
  return response.data;
}

export async function patchShift(
  scheduleId: string,
  shiftId: string,
  payload: ScheduledShiftUpsertPayload,
): Promise<ScheduledShiftRead> {
  const response = await api.patch<ScheduledShiftRead>(
    `/schedule/${scheduleId}/shifts/${shiftId}`,
    payload,
  );
  return response.data;
}

export async function deleteShift(scheduleId: string, shiftId: string): Promise<void> {
  await api.delete(`/schedule/${scheduleId}/shifts/${shiftId}`);
}

export async function copyWeek(
  scheduleId: string,
  payload: { from_date: string; to_date: string },
): Promise<{ copied: number }> {
  const response = await api.post<{ copied: number }>(`/schedule/${scheduleId}/copy-week`, payload);
  return response.data;
}

export async function getForecastRange(params: {
  date_from: string;
  date_to: string;
}): Promise<RevenueForecastRead[]> {
  const response = await api.get<RevenueForecastRead[]>("/schedule/forecast", { params });
  return response.data;
}

export async function recomputeForecast(
  payload: RevenueForecastRecomputePayload,
): Promise<{ recomputed: number }> {
  const response = await api.post<{ recomputed: number }>("/schedule/forecast/recompute", payload);
  return response.data;
}

export async function overrideForecast(
  businessDate: string,
  payload: RevenueForecastOverridePayload,
): Promise<RevenueForecastRead> {
  const response = await api.post<RevenueForecastRead>(
    `/schedule/forecast/${businessDate}/override`,
    payload,
  );
  return response.data;
}

export async function removeForecastOverride(
  businessDate: string,
): Promise<RevenueForecastRead> {
  const response = await api.delete<RevenueForecastRead>(
    `/schedule/forecast/${businessDate}/override`,
  );
  return response.data;
}

export async function runCostForecast(scheduleId: string): Promise<PayrollForecastRunRead> {
  const response = await api.post<PayrollForecastRunRead>(
    `/schedule/${scheduleId}/cost-forecast`,
  );
  return response.data;
}

export async function getLatestRun(
  scheduleId: string,
): Promise<PayrollForecastRunRead | null> {
  const response = await api.get<PayrollForecastRunRead | null>(
    `/schedule/${scheduleId}/cost-forecast/latest`,
  );
  return response.data;
}

export async function listRuns(scheduleId: string): Promise<PayrollForecastRunRead[]> {
  const response = await api.get<PayrollForecastRunRead[]>(
    `/schedule/${scheduleId}/cost-forecast/runs`,
  );
  return response.data;
}

export async function getRun(
  scheduleId: string,
  runId: string,
): Promise<PayrollForecastRunRead> {
  const response = await api.get<PayrollForecastRunRead>(
    `/schedule/${scheduleId}/cost-forecast/runs/${runId}`,
  );
  return response.data;
}

export async function getPlanFact(scheduleId: string): Promise<PlanFactSummaryRead> {
  const response = await api.get<PlanFactSummaryRead>(`/schedule/${scheduleId}/plan-fact`);
  return response.data;
}

export async function getEmployeesRoster(): Promise<EmployeeRosterRow[]> {
  const response = await api.get<EmployeeRosterRow[]>("/schedule/employees-roster");
  return response.data;
}

export async function getPayrollAdjustmentCategories(
  type?: PayrollAdjustmentType,
  includeInactive = false,
): Promise<PayrollAdjustmentCategory[]> {
  const response = await api.get<PayrollAdjustmentCategory[]>("/payroll/adjustment-categories", {
    params: {
      type,
      include_inactive: includeInactive || undefined,
    },
  });
  return response.data;
}

export async function createPayrollAdjustmentCategory(
  payload: PayrollAdjustmentCategoryPayload,
): Promise<PayrollAdjustmentCategory> {
  const response = await api.post<PayrollAdjustmentCategory>(
    "/payroll/adjustment-categories",
    payload,
  );
  return response.data;
}

export async function patchPayrollAdjustmentCategory(
  id: string,
  payload: PayrollAdjustmentCategoryPatch,
): Promise<PayrollAdjustmentCategory> {
  const response = await api.patch<PayrollAdjustmentCategory>(
    `/payroll/adjustment-categories/${id}`,
    payload,
  );
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

export async function getDeposits(): Promise<DepositListItem[]> {
  const response = await api.get<DepositListItem[]>("/deposits");
  return response.data;
}

export async function getDepositTransactions(employeeId: string): Promise<DepositTransaction[]> {
  const response = await api.get<DepositTransaction[]>(`/deposits/${employeeId}/transactions`);
  return response.data;
}

export async function patchDepositConfig(
  employeeId: string,
  payload: DepositConfigPatch,
): Promise<void> {
  await api.patch(`/deposits/${employeeId}/config`, payload);
}

export async function postDepositPayout(
  employeeId: string,
  payload: DepositPayoutPayload,
): Promise<void> {
  await api.post(`/deposits/${employeeId}/payout`, payload);
}

export async function postDepositWriteoff(
  employeeId: string,
  payload: DepositWriteoffPayload,
): Promise<void> {
  await api.post(`/deposits/${employeeId}/writeoff`, payload);
}

export async function postDepositInitialBalance(employeeId: string, amount: string): Promise<void> {
  await api.post(`/deposits/${employeeId}/initial-balance`, { amount });
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

export function apiErrorStatus(error: unknown) {
  return axios.isAxiosError(error) ? error.response?.status : undefined;
}
