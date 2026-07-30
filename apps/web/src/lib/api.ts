import axios, { AxiosError, type InternalAxiosRequestConfig } from "axios";

import { clearSession, getAccessToken, setSession, type AuthUser } from "./auth";

export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";
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

export type AccessPermission = {
  code: string;
  description: string;
};

export type AccessPermissionModule = {
  module: string;
  permissions: AccessPermission[];
};

export type AccessRole = {
  id: string;
  code: string;
  name: string;
  is_editable: boolean;
  permission_codes: string[];
};

export type AccessUser = {
  id: string;
  email: string;
  full_name: string;
  is_active: boolean;
  role_codes: string[];
};

export type AccessUserCreatePayload = {
  email: string;
  full_name: string;
  password: string;
  is_active: boolean;
};

export type AccessUserPatchPayload = Partial<Pick<AccessUser, "full_name" | "is_active">>;

export type AccessAuditEvent = {
  type: string;
  actor_email: string | null;
  role_code: string | null;
  permission_code: string | null;
  action: string;
  created_at: string;
};

export type AccessAuditParams = {
  limit?: number;
  offset?: number;
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

export type SubstitutePair = {
  from_position: string;
  to_position: "Повар" | "Кассир";
  add_to_schedule: boolean;
};

export type SubstitutePairsResponse = {
  pairs: SubstitutePair[];
};

export type PositionArchetype =
  | "okladnik"
  | "production_percent"
  | "shift_pool"
  | "courier"
  | "none";
export type PositionPermissionGroup =
  | "administration"
  | "cooks"
  | "cashiers"
  | "auxiliary"
  | "couriers"
  | "none";
export type PositionScheduleType = "SESSION" | "FIXED" | "HOURS";
export type PositionStatus = "active" | "excluded";

export type Position = {
  id: string;
  name: string;
  iiko_role_id: string | null;
  iiko_role_code: string | null;
  archetype: PositionArchetype;
  permission_group: PositionPermissionGroup;
  participates_in_access: boolean;
  access_role_code: string | null;
  status: PositionStatus;
  schedule_type: PositionScheduleType;
  employee_count: number;
  created_at: string;
  updated_at: string;
};

export type PositionPayload = {
  name: string;
  archetype: PositionArchetype;
  permission_group: PositionPermissionGroup;
  participates_in_access?: boolean;
  schedule_type?: PositionScheduleType;
};

export type PositionsSyncResult = {
  linked: number;
  imported: number;
  provisioned: number;
  positions: Position[];
};

export type EmployeeStatus = "active" | "inactive" | "requires_setup" | "dismissing";
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
  is_substitute: boolean;
  effective_from: string;
  effective_to: string | null;
  is_pending: boolean;
  created_at: string;
  updated_at: string;
};

export type EmployeeRoleAssignmentCreate = {
  payroll_role: PayrollRole;
  category: EmployeeCategory;
  is_primary?: boolean;
  is_substitute?: boolean;
  effective_from?: string | null;
  comment?: string | null;
};

export type EmployeeRoleAssignmentPatch = Partial<
  Pick<EmployeeRoleAssignment, "payroll_role" | "category" | "is_primary" | "is_substitute">
> & {
  effective_from?: string | null;
  comment?: string | null;
};

export type EmployeeNoticeInfo = {
  notice_date: string;
  days_since: number;
  will_trigger_full_payout: boolean;
};

export type FreelancerCard = {
  period_from: string;
  period_to: string;
  placeholder_employee_id: string;
  // Имя плейсхолдера «Внештат №N» — видно всем со Штатом.
  placeholder_name: string | null;
  // Открытый ПИН открытия смены. Приходит null, если у пользователя нет права
  // staff.freelancer_pin.read (серверный гейт). null → строку с ПИН не показываем.
  pin_code: string | null;
  archived_at: string | null;
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
  is_courier_placeholder: boolean;
  is_freelancer_placeholder?: boolean;
  is_freelancer_temp?: boolean;
  freelancer_shift_rate?: string | null;
  freelancer_card?: FreelancerCard | null;
  status: EmployeeStatus;
  hire_date: string | null;
  tenure_started_at: string | null;
  fire_date: string | null;
  fire_reason: string | null;
  requires_role_review: boolean;
  requires_position_review?: boolean;
  role_review_payload: Record<string, unknown> | null;
  admin_payroll_excluded?: boolean;
  // Попадает в персональный отчёт ЗП (получает зарплату, кроме обычных курьеров) — с бэка.
  in_personal_report: boolean;
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
    | "is_courier_placeholder"
    | "requires_role_review"
    | "hire_date"
    | "fire_date"
  >
> & {
  pin_code?: string | null;
  effective_from?: string | null;
  comment?: string | null;
  transfer_from_existing?: boolean;
  acknowledge_closed_period?: boolean;
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

export type EmployeePositionAssignment = {
  id: string;
  employee_id: string;
  position: string;
  effective_from: string;
  effective_to: string | null;
  comment: string | null;
  created_by_name: string | null;
  created_at: string | null;
  warnings?: PayrollImpactWarning[];
};

export type EmployeePositionChangePayload = {
  position: string;
  effective_from: string;
  comment?: string | null;
  acknowledge_closed_period?: boolean;
};

export type PayrollImpactWarning = {
  code: string;
  message: string;
  periods?: Array<{
    id: string;
    start_date: string;
    end_date: string;
    label?: string;
  }>;
};

export type EmployeePositionAssignmentPatch = {
  position?: string | null;
  effective_from?: string | null;
  comment?: string | null;
  acknowledge_closed_period?: boolean;
};

export type EmployeePositionAssignmentDeletePayload = {
  comment?: string | null;
  acknowledge_closed_period?: boolean;
};

export type DepositDismissAction =
  | "payout_full"
  | "payout_partial"
  | "write_off"
  | "schedule_payout"
  | "none";

export type DepositPayoutMethod = "cash_tk" | "cash_safe" | "bank_draft" | "bank_draft_sber";

export type DepositPayoutTarget = "account" | "payroll";

export type EmployeeDismissPayload = {
  fire_date?: string;
  reason_id?: string;
  reason_code?: string;
  comment?: string;
  reason?: string;
  deposit_action: DepositDismissAction;
  deposit_payout_target?: DepositPayoutTarget;
  deposit_payout_method?: DepositPayoutMethod;
  deposit_payout_period_id?: string;
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
  position: string;
  roles: Array<{
    payroll_role: PayrollRole;
    category: EmployeeCategory;
    is_primary: boolean;
  }>;
  is_senior?: boolean;
  is_deputy_senior?: boolean;
  // Контур «вне штата»: временный внештатник через пул iiko-плейсхолдеров.
  is_freelancer?: boolean;
  freelancer_shift_rate?: number;
  period_from?: string;
  period_to?: string;
};

export type FreelancerCardPatchPayload = {
  period_from?: string;
  period_to?: string;
  freelancer_shift_rate?: number;
};

export type FreelancerAttendanceCase = {
  id: string;
  placeholder_employee_id: string;
  placeholder_name: string | null;
  work_date: string;
  minutes: number;
  opened_at: string | null;
  status: string;
  resolved_employee_id: string | null;
  note: string | null;
  created_at: string;
  updated_at: string;
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
  period_type: "week" | "half_month";
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
  summary: PayrollRunSummary;
  is_imported_legacy: boolean;
  payout_cash_total: number;
  payout_cash_wallet_id: string | null;
  needs_recalc: boolean;
  period: PayrollPeriod | null;
};

export type PayrollAttendanceWarning = {
  type: string;
  employee_id: string;
  employee_name: string;
  work_date: string;
  quality_status: string;
  notes: string | null;
};

export type PayrollPaymentState = "unpaid" | "in_progress" | "partial" | "paid";

export type PayrollRunSummary = Record<string, unknown> & {
  attendance_warnings?: PayrollAttendanceWarning[];
  attendance_warning_count?: number;
  revenue_total?: number;
  employee_count?: number;
  // Агрегаты выплаты (для подсветки недоплаченной ведомости в списке).
  payment_state?: PayrollPaymentState;
  paid_total?: number;
  remaining_shortfall?: number;
  underpaid_count?: number;
};

export type PayrollPaymentMethod = "business_card" | "cash" | "transfer" | "other";
export type PayrollCashWalletCode = "cash_safe" | "tk_chernikova";
export type PayrollPayoutStatus = "pending" | "planned" | "draft_created" | "paid";
export type PayrollPayoutDeltaClassification = "unchanged" | "topup" | "overpay";

export type PayrollPaymentPayload = {
  paid_at: string;
  method: PayrollPaymentMethod;
  cash_wallet_code: PayrollCashWalletCode;
};

export type MarkPayrollPaymentPayload = PayrollPaymentPayload & {
  employee_id: string;
};

export type MarkAllPayrollPaymentsResponse = {
  marked_count: number;
};

export type PayrollPayoutDelta = {
  employee_id: string;
  previous_amount: number | string;
  new_amount: number | string;
  delta: number | string;
  classification: PayrollPayoutDeltaClassification;
};

export type RunPayoutDelta = {
  run_id: string;
  document_id: string | null;
  previous_amount: number | string;
  new_amount: number | string;
  delta: number | string;
  classification: PayrollPayoutDeltaClassification;
};

export type PayrollBankDraft = {
  id: string;
  run_id: string;
  document_id: string;
  amount: number | string;
  status: "created" | "updated" | "paid" | "failed" | string;
  provider_ref: string | null;
  payload: Record<string, unknown>;
  last_error: string | null;
  synced_at: string | null;
  created_at: string;
};

export type PayrollPayoutDraftsResponse = {
  drafts_count: number;
};

export type PayrollPayoutApplyDeltasResponse = {
  applied_count: number;
};

export type PayrollLine = {
  id: string;
  run_id: string;
  employee_id: string;
  role: string;
  base_pay: number;
  premium: number;
  percent_pay: number;
  vacation_pay: number;
  ndfl_withheld: number;
  fund_accrual: number;
  deduction: number;
  deposit_withholding: number;
  deposit_payout: number;
  deposit_payout_scheduled: number;
  advance_issued: number;
  ndfl_deduction: number;
  total_payable: number;
  deposit_excluded_for_run: boolean;
  deposit_exclusion_reason: string | null;
  payment_status: "paid" | "pending" | "partially_paid";
  paid_amount: number | null;
  paid_at: string | null;
  paid_method: PayrollPaymentMethod | null;
  payment_comment: string | null;
  amount_cash: number;
  amount_account: number;
  payout_status: PayrollPayoutStatus;
  draft_status: string | null;
  overpaid_amount: number;
  // Режим оклада «по востребованию» (ЗП собственника): начисляется в долг, не выплачивается
  // автоматически. debt = accrued − paid (накопительно по всем периодам).
  on_demand: boolean;
  on_demand_accrued: number;
  on_demand_paid: number;
  on_demand_debt: number;
  components: Record<string, unknown>;
};

export type PayrollLineDepositOverridePatch = {
  deposit_excluded_for_run: boolean;
  deposit_exclusion_reason?: string | null;
};

export type AdminPayoutMode = "split" | "first_half" | "second_half" | "on_demand";

export type AdminSalaryDefault = {
  position: string;
  amount: number | null;
  effective_from: string | null;
  payout_mode: AdminPayoutMode;
};

export type DishwasherEmployee = {
  id: string;
  full_name: string;
};

export type DishwasherShift = {
  employee_id: string;
  work_date: string;
};

export type AdminSalaryOverride = {
  employee_id: string;
  employee_name: string;
  position: string;
  amount: number;
  effective_from: string | null;
};

export type AdminSalariesResponse = {
  defaults: AdminSalaryDefault[];
  overrides: AdminSalaryOverride[];
};

export type AdminSalaryDefaultPayload = {
  position: string;
  amount: number;
  effective_from: string | null;
};

export type AdminSalaryOverridePayload = {
  position: string;
  amount: number;
  effective_from: string | null;
};

export type PayrollPersonalReport = {
  employee_id: string;
  employee_name: string;
  employee_position: string | null;
  date_from: string;
  date_to: string;
  periods: Array<{
    period_id: string;
    run_id: string;
    run_status: string;
    // Ярлык объединённой расчётки: перечисление ролей («pizza, sushi»). Для подсветки
    // используйте `roles` (по роли — свой цвет), а не этот текст.
    role: string;
    period_start: string;
    period_end: string;
    base_pay: number;
    premium: number;
    percent_pay: number;
    vacation_pay: number;
    ndfl_withheld: number;
    fund_accrual: number;
    deduction: number;
    deposit_withholding: number;
    deposit_payout: number;
    bonus_total: number;
    penalty_total: number;
    total_payable: number;
    is_substitute: boolean;
    // Разбивка объединённой расчётки по ролям (для секций/чипов внутри расчётки).
    roles: Array<{
      role: string;
      base_pay: number;
      premium: number;
      percent_pay: number;
      vacation_pay: number;
      ndfl_withheld: number;
      fund_accrual: number;
      deduction: number;
      deposit_withholding: number;
      deposit_payout: number;
      bonus_total: number;
      penalty_total: number;
      total_payable: number;
    }>;
  }>;
  daily: Array<{
    date: string;
    base_pay: number;
    percent_pay: number;
    premium: number;
    vacation_pay: number;
    ndfl_withheld: number;
    fund_accrual: number;
    deposit_in: number;
    deposit_out: number;
    penalty: number;
    audit_penalty: number;
    comment: string | null;
    // Роль(и) дня для подсветки по сменам: `role` — основная (по оплате), `roles` — все.
    role: string | null;
    roles: string[];
  }>;
  opening_balance: string;
  closing_balance: string;
  // Накопительный фонд из леджера (синхронно с вкладкой «Накопит. фонд»).
  fund_accumulated: number;
  fund_outstanding: number;
  shifts_count: number;
  adjustments: Array<{
    id: string;
    type: PayrollAdjustmentType;
    work_date: string;
    category_id: string | null;
    category_name: string;
    custom_label: string | null;
    amount: number;
    comment: string | null;
  }>;
  deposit_transactions: Array<{
    id: string;
    transaction_type: string;
    amount: number;
    created_at: string;
    run_id: string | null;
  }>;
  totals: {
    base_pay: number;
    premium: number;
    percent_pay: number;
    vacation_pay: number;
    ndfl_withheld: number;
    fund_accrual: number;
    deduction: number;
    deposit_withholding: number;
    deposit_payout: number;
    bonus_total: number;
    penalty_total: number;
    audit_penalty_total: string;
    total_payable: number;
  };
};

export type PayrollAggregate = {
  date_from: string;
  date_to: string;
  employees_count: number;
  runs_count: number;
  totals: {
    base_pay: string;
    premium: string;
    percent_pay: string;
    vacation_pay: string;
    fund_accrual: string;
    deduction: string;
    bonus_total: string;
    penalty_total: string;
    deposit_withheld: string;
    ndfl_total: string;
    gross: string;
    total_payable: string;
  };
  periods: Array<{
    period_id: string;
    period_start: string;
    period_end: string;
    run_id: string;
    run_status: string;
    lines_count: number;
    total_payable: string;
    base_pay: string;
    premium: string;
    percent_pay: string;
    deduction: string;
    fund_accrual: string;
  }>;
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
  fund_exclusion?: FundExclusionRead;
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
  role: string | null;
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
  role?: string | null;
  category_id?: string | null;
  custom_label?: string | null;
  amount: string;
  comment?: string | null;
};

export type PayrollAdjustmentPatch = Partial<PayrollAdjustmentPayload>;

export type PayrollAdvanceAvailability = {
  employee_id: string;
  as_of: string;
  period_start: string | null;
  period_end: string | null;
  basis: string;
  earned_to_date: number;
  already_advanced: number;
  available: number;
  note: string | null;
  // true в день выплаты периода: заработанное уходит с ведомостью, аванс недоступен.
  payout_reached: boolean;
};

export type PayrollAdvancePayoutStatus =
  | "disbursed"
  | "sent_to_bank"
  | "awaiting_payout"
  // Разрешение на выдачу через кассу: уйдёт в кассу — выдаст администратор.
  | "awaiting_kassa"
  | "failed"
  | "cancelled"
  // Разрешение отклонено администратором кассы.
  | "cancelled_by_kassa";

export type PayrollAdvance = {
  id: string;
  employee_id: string;
  role: string;
  kind: string;
  amount: number;
  per_installment_amount: number;
  installments_count: number;
  recovered_amount: number;
  status: string;
  issued_on: string;
  recovery_start_date: string | null;
  payout_method: string | null;
  wallet_id: string | null;
  comment: string | null;
  payout_status: PayrollAdvancePayoutStatus;
};

export type PayrollAdvancePayload = {
  employee_id: string;
  amount: string;
  kind?: "advance" | "loan";
  issued_on?: string;
  payout_method?: string;
  wallet_id?: string;
  installments_count?: number;
  installment_amount?: string;
  recovery_start_date?: string;
  comment?: string | null;
  override_ceiling?: boolean;
};

export type PayrollAdvanceConfig = { loan_max: number };

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

export type DeferredChargeStatus = "pending" | "partially_applied" | "applied" | "cancelled";

export type DeferredChargeSplit = {
  id: string;
  split_index: number;
  amount: string;
  run_id: string | null;
  adjustment_id: string | null;
  applied_at: string | null;
};

export type DeferredChargeRecipient = {
  id: string;
  employee_id: string;
  employee_name: string | null;
  employee_position: string | null;
  per_split_amount: string;
  splits_remaining: number;
  collapsed_at: string | null;
  collapse_run_id: string | null;
  splits: DeferredChargeSplit[];
};

export type DeferredCharge = {
  id: string;
  source_audit_id: string;
  source_item_id: string | null;
  source_audit_date: string | null;
  source_item_name: string | null;
  allocation_group: "chefs" | "admins" | "common";
  total_penalty_amount: string;
  splits_count: number;
  start_period_start: string | null;
  status: DeferredChargeStatus;
  reason: string;
  created_by_name: string | null;
  created_at: string | null;
  updated_at: string | null;
  recipients: DeferredChargeRecipient[];
};

export type DeferredChargeCreatePayload = {
  source_audit_id: string;
  source_item_id: string;
  total_penalty_amount: string;
  splits_count: number;
  start_period_start?: string | null;
  reason: string;
};

export type InventoryAllocationGroup = "chefs" | "common" | "admins";
export type InventoryAuditStatus = "draft" | "applied" | "cancelled";

export type InventoryPosition = {
  id: string;
  code: string;
  display_name: string;
  allocation_group: InventoryAllocationGroup | null;
  swap_group: string | null;
  iiko_product_guid: string | null;
  is_active: boolean;
  sort_order: number;
  created_at: string | null;
  updated_at: string | null;
};

export type InventoryPositionPayload = {
  code: string;
  display_name: string;
  allocation_group?: InventoryAllocationGroup | null;
  swap_group?: string | null;
  iiko_product_guid?: string | null;
  sort_order?: number;
};

export type InventoryPositionPatch = Partial<
  Pick<InventoryPosition, "allocation_group" | "swap_group" | "is_active" | "sort_order">
>;

export type IikoProduct = {
  guid: string;
  name: string;
  code: string | null;
};

export type InventoryPositionsSyncResult = {
  added: number;
  updated: number;
  total: number;
};

export type IikoCandidate = {
  document_id: string;
  document_num: string;
  items_count: number;
  total_shortage: string;
  matched_active_count: number;
};

export type InventoryAuditItem = {
  id: string;
  audit_id: string;
  position_id: string | null;
  position_code: string | null;
  position_display_name: string | null;
  allocation_group: InventoryAllocationGroup | null;
  is_considered: boolean;
  amount: string;
  amount_iiko?: string | null;
  manual_shortage_adjustment?: string | null;
  manual_shortage_adjustment_reason?: string | null;
  has_manual_adjustment?: boolean;
  swap_group: string | null;
  swap_group_default: string | null;
  swap_group_override: string | null;
  has_swap_group_override: boolean;
  is_excluded?: boolean;
  exclusion_reason?: string | null;
  iiko_product_guid: string | null;
  product_name_snapshot: string;
  shortage_amount: string;
  created_at: string | null;
};

export type InventoryAuditCarryoverSuggestion = {
  item_id: string;
  prior_audit_date: string;
  prior_amount: string;
  current_shortage: string;
  suggested_reduction: string;
};

export type InventoryAuditExclusionLogItem = {
  id: string;
  item_id?: string | null;
  product_name?: string | null;
  amount?: string | null;
  employee_id?: string | null;
  employee_name?: string | null;
  employee_position?: string | null;
  reason: string;
  created_by_name?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export type InventoryAuditExclusionLogRow = {
  id: string;
  audit_id: string;
  audit_business_date: string | null;
  audit_status: InventoryAuditStatus | null;
  item_id?: string | null;
  product_name?: string | null;
  amount?: string | null;
  employee_id?: string | null;
  employee_name?: string | null;
  employee_position?: string | null;
  reason: string;
  created_by_name?: string | null;
  created_at: string | null;
};

export type InventoryAuditAllExclusions = {
  items: InventoryAuditExclusionLogRow[];
  employees: InventoryAuditExclusionLogRow[];
};

export type InventoryAudit = {
  id: string;
  business_date: string;
  previous_audit_date: string | null;
  iiko_document_id: string | null;
  iiko_document_num: string | null;
  source: "iiko" | "manual";
  status: InventoryAuditStatus;
  total_shortage_amount: string;
  total_shortage_iiko?: string;
  total_shortage_considered?: string;
  total_penalty_amount: string;
  employee_count: number;
  notes: string | null;
  penalty_work_date_override: string | null;
  penalty_work_date_effective?: string | null;
  created_at: string | null;
  updated_at: string | null;
  applied_at: string | null;
  items_skipped_count?: number;
  items?: InventoryAuditItem[];
  item_exclusions_log?: InventoryAuditExclusionLogItem[];
  employee_exclusions_log?: InventoryAuditExclusionLogItem[];
  swap_groups?: InventorySwapGroupSummary[];
  computation_snapshot?: InventoryComputationSnapshot | null;
};

export type InventoryPayoutOption = {
  override_value: string | null;
  period_start: string;
  period_end: string;
  payout_date: string;
  locked: boolean;
  is_default: boolean;
};

export type InventorySwapGroupSummary = {
  group: string;
  allocation_group: InventoryAllocationGroup | null;
  allocation_groups?: string[];
  net_amount: string;
  effective_shortage?: string;
  is_covered: boolean;
  comment?: string;
  items: Array<Record<string, unknown>>;
};

export type InventoryComputationSnapshot = {
  period?: { start?: string; end?: string; previous_audit_date?: string | null };
  groups?: Record<string, InventoryGroupSnapshot>;
  employee_penalties?: InventoryEmployeePenalty[];
  employee_penalties_by_id?: Record<string, string>;
  employee_recipients?: InventoryEmployeeRecipient[];
  prepaid_revision_charges?: InventoryPrepaidRevisionCharge[];
  skipped_items?: Array<{ item_id?: string; reason?: string }>;
  swap_groups?: InventorySwapGroupSummary[];
  warnings?: string[];
};

export type InventoryGroupSnapshot = {
  group?: string;
  total_shortage?: string;
  sum?: string;
  rate?: string;
  rate_reason?: string;
  rate_percent?: string;
  penalty?: string;
  gross_penalty?: string;
  prepaid?: string;
  threshold?: string;
  items?: Array<Record<string, unknown>>;
  recipients?: Record<
    string,
    { total?: string; count?: number; items?: InventoryEmployeePenalty[] }
  >;
};

export type InventoryEmployeePenalty = {
  employee_id: string;
  full_name: string;
  position: string | null;
  amount: string;
};

export type InventoryEmployeeRecipient = InventoryEmployeePenalty & {
  recipient_group?: "chefs" | "admins" | string;
  is_excluded?: boolean;
  exclusion_reason?: string | null;
};

export type InventoryPrepaidRevisionCharge = {
  employee_id: string;
  full_name: string;
  amount: string;
  work_date: string;
  group: "chefs" | "admins" | string;
};

export type InventoryComputation = {
  audit_id: string;
  total_shortage_amount: string;
  total_penalty_amount: string;
  period_start: string;
  period_end: string;
  groups: Record<string, InventoryGroupSnapshot>;
  swap_groups?: InventorySwapGroupSummary[];
  employee_penalties: InventoryEmployeePenalty[];
  employee_recipients?: InventoryEmployeeRecipient[];
  warnings: string[];
};

export type InventoryManualAuditPayload = {
  business_date: string;
  notes?: string | null;
  items: Array<{
    position_id?: string | null;
    position_code?: string | null;
    product_name_snapshot?: string | null;
    shortage_amount: string;
  }>;
};

export type InventoryAuditItemPayload = {
  position_id?: string | null;
  position_code?: string | null;
  iiko_product_guid?: string | null;
  product_name_snapshot?: string | null;
  shortage_amount: string;
  swap_group_override?: string | null;
};

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

export type ScheduleLedgerEntryRead = {
  id: string;
  business_date: string;
  employee_id: string;
  employee_full_name: string;
  position: string;
  payroll_role: string | null;
  station_code: string | null;
  opened_at: string;
  closed_at: string | null;
  minutes_worked: number;
  is_closed: boolean;
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
  deviation_flags: string[];
  planned_cashier_allowance: CashierAllowancePlanFactInfo | null;
  actual_cashier_allowance: CashierAllowancePlanFactInfo | null;
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

export type VacationStatus = "planned" | "paid" | "cancelled";

export type VacationPeriodRead = {
  id: string;
  employee_id: string;
  employee_full_name: string;
  date_start: string;
  date_end: string;
  days_count: number;
  payout_date: string | null;
  payout_amount: string | number | null;
  status: VacationStatus;
  comment: string | null;
  created_by_label: string | null;
  created_at: string;
};

export type VacationBalanceRead = {
  employee_id: string;
  year: number;
  limit: number;
  used: number;
  remaining: number;
  periods: VacationPeriodRead[];
};

export type VacationRosterRow = {
  employee_id: string;
  employee_full_name: string;
  position: string;
  year: number;
  limit: number;
  used: number;
  remaining: number;
  periods: VacationPeriodRead[];
};

export type VacationPeriodPayload = {
  employee_id: string;
  date_start: string;
  date_end: string;
  payout_date?: string | null;
  comment?: string | null;
  force_remove_conflicting_shifts?: boolean;
};

export type VacationPeriodPatchPayload = {
  date_start?: string;
  date_end?: string;
  payout_date?: string | null;
  comment?: string | null;
  force_remove_conflicting_shifts?: boolean;
};

export type VacationShiftConflict = {
  shift_id: string;
  business_date: string;
  schedule_id: string;
  schedule_status: string;
};

export type VacationConflictResponse = {
  detail: string;
  conflicting_shifts: VacationShiftConflict[];
};

export type EmployeeRosterAvailableRole = {
  payroll_role: string;
  category: string;
  is_primary: boolean;
  is_substitute: boolean;
  default_station_code: string | null;
};

export type EmployeeRosterRow = {
  id: string;
  full_name: string;
  position: "Повар" | "Кассир" | string;
  primary_payroll_role: string | null;
  default_cooking_station: string | null;
  requires_role_review: boolean;
  available_roles: EmployeeRosterAvailableRole[];
  allowances: {
    senior: boolean;
    deputy: boolean;
  };
};

export type CashierAllowanceRole = "senior" | "deputy_senior" | "none";

export type ShiftAllowanceOverrideRead = {
  id: string;
  shift_schedule_id: string;
  business_date: string;
  position: "Кассир";
  recipient_employee_id: string | null;
  recipient_role: CashierAllowanceRole;
  comment: string | null;
  set_by_user_id: string | null;
  set_at: string;
  created_at: string;
  updated_at: string;
};

export type CashierAllowanceOverridePayload = {
  business_date: string;
  recipient_employee_id?: string | null;
  recipient_role: CashierAllowanceRole;
  comment?: string | null;
};

export type AllowanceCandidateRead = {
  employee_id: string;
  full_name: string;
  is_senior: boolean;
  is_deputy_senior: boolean;
  is_planned: boolean;
  is_actual: boolean;
  minutes_worked: number;
};

export type AllowanceAssignmentRead = {
  business_date: string;
  position: "Кассир";
  recipient_employee_id: string | null;
  recipient_full_name: string | null;
  recipient_role: CashierAllowanceRole;
  reason: string;
  candidates: AllowanceCandidateRead[];
  has_manual_override: boolean;
};

export type CashierAllowancePlanFactInfo = Pick<
  AllowanceAssignmentRead,
  "recipient_employee_id" | "recipient_full_name" | "recipient_role" | "reason"
>;

export type ShiftLedgerSource = "schedule" | "manual_correction" | "fallback_primary";
export type ShiftLedgerStatus = "resolved" | "needs_role_selection" | "needs_employee_setup";

export type ShiftLedgerAvailableRole = {
  payroll_role: PayrollRole | string;
  category: EmployeeCategory;
  is_substitute?: boolean;
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
  position: "Повар" | "Кассир";
  role: "senior" | "deputy_senior";
  amount: number;
  effective_from: string;
  effective_to: string | null;
  created_at: string;
};

export type PayrollSeniorityPremiumPayload = Pick<
  PayrollSeniorityPremium,
  "position" | "role" | "amount" | "effective_from"
>;

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
  // Излишек: собрано сверх текущей цели (после понижения индивидуальной цели) —
  // «долг» перед сотрудником, UI подсвечивает и предлагает выдать.
  surplus?: string | null;
  deposit_target_override?: string | null;
  deposit_withholding_override?: string | null;
  deposit_excluded_reason?: string | null;
  // Отложенная выдача депозита (этап 4–6): есть ли pending-план и на какую сумму.
  scheduled_payout_pending?: boolean;
  scheduled_payout_amount?: string | null;
};

export type DepositSchedulePayoutPayload = {
  // Сумма выдачи; пропуск/null = весь накопленный баланс на момент ближайшей ведомости.
  amount?: string | null;
  account_choice?: "safe" | "cash_tk" | "bank_draft" | "bank_draft_sber";
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
  // Счёт немедленной выдачи: ТК Черникова (по умолч., +iiko) / Сейф / банк-черновик Т-Банк
  // (bank_draft) или Сбер (bank_draft_sber).
  payout_method?: DepositPayoutMethod;
  // Режим наличных каналов: immediate — выдать сразу; reserve — завести резерв («В кассе»/
  // «На Сейфе»), выдать позже. Для банк-каналов игнорируется (там всегда черновик).
  payout_mode?: "immediate" | "reserve";
};

export type DepositWriteoffPayload = {
  amount: string;
  reason?: string | null;
};

export type CourierDepositStatusFilter = "active" | "fired" | "all";
export type CourierDepositCategory = "primary" | "secondary";
export type CourierDepositCategoryFilter = CourierDepositCategory | "all";
export type CourierDepositTransactionType = "top_up" | "return" | "forfeit";
export type CourierScheduleCategory = "primary" | "secondary";
export type CourierWorkStatus = "working" | "reserve";
export type CourierListWorkStatusFilter = CourierWorkStatus | "all";
export type CourierScheduleMatchStatus =
  | "matched_primary"
  | "short_primary"
  | "no_show_primary"
  | "matched_secondary"
  | "short_secondary"
  | "no_show_secondary"
  | "helping"
  | "not_counted"
  | "not_started";

export type CourierDepositSettings = {
  target_amount: number;
  target_amount_senior: number;
  withhold_primary: number;
  withhold_secondary: number;
  auto_withhold_enabled: boolean;
};

export type CourierDepositSettingsUpdate = Partial<CourierDepositSettings>;

export type CourierDepositTransaction = {
  id: number | null;
  account_employee_id: string;
  transaction_type: CourierDepositTransactionType;
  amount_cents: number;
  transaction_date: string;
  comment: string | null;
  created_by: string;
  created_by_name?: string | null;
  created_at: string | null;
};

export type CourierDepositRow = {
  employee_id: string;
  full_name: string;
  status: string;
  category: CourierDepositCategory | null;
  target_amount_cents: number;
  opening_balance_cents: number;
  opening_date: string;
  balance_cents: number;
  progress_pct: number;
  remaining_to_target_cents: number;
  last_transaction: CourierDepositTransaction | null;
};

export type CourierDepositAccount = {
  employee_id: string;
  target_amount_cents: number;
  opening_balance_cents: number;
  opening_date: string;
  created_at: string | null;
  updated_at: string | null;
};

export type CourierDepositCard = {
  account: CourierDepositAccount;
  balance_cents: number;
  transactions: CourierDepositTransaction[];
};

export type CourierDepositOpeningPayload = {
  amount_cents: number;
  opening_date: string;
};

export type CourierDepositTransactionPayload = {
  transaction_type: CourierDepositTransactionType;
  amount_cents: number;
  transaction_date: string;
  comment?: string | null;
  // Канал выдачи только для возврата: ТК Черникова (по умолч.) / Сейф / банк-черновик
  // Т-Банк (bank_draft) или Сбер (bank_draft_sber).
  payout_method?: "cash_tk" | "cash_safe" | "bank_draft" | "bank_draft_sber";
  // Явное подтверждение повторного пополнения за ту же дату (обходит защиту от задвоения,
  // бэкенд иначе вернёт 409). Ставится только после подтверждения кассира.
  allow_duplicate?: boolean;
};

export type CourierEvaluationSource = "web" | "telegram" | "api";

export type CourierEvaluationCriterion = {
  id: number;
  code: string;
  label: string;
  score: number;
  is_active: boolean;
  display_order: number;
};

export type CourierEvaluation = {
  id: number | null;
  courier_employee_id: string;
  criterion_id: number;
  score_snapshot: number;
  comment: string | null;
  evaluated_at: string;
  source: CourierEvaluationSource;
  created_by: string;
  author_name: string | null;
  created_at: string | null;
  updated_at: string | null;
  deleted_at: string | null;
};

export type CourierEvaluationListParams = {
  courier?: string;
  author?: string;
  from?: string;
  to?: string;
  criterion?: number;
};

export type CourierEvaluationPayload = {
  courier_employee_id: string;
  criterion_id: number;
  evaluated_at?: string | null;
  comment?: string | null;
  source?: CourierEvaluationSource;
};

export type CourierEvaluationPatch = {
  criterion_id?: number;
  evaluated_at?: string | null;
  comment?: string | null;
};

export type CourierEvaluationTopCriterion = {
  criterion_id: number;
  code: string;
  label: string;
  count: number;
};

export type CourierEvaluationMonthlyAggregate = {
  courier_employee_id: string;
  month: string;
  score_sum: number;
  positive_count: number;
  negative_count: number;
  neutral_count: number;
  top_criteria: CourierEvaluationTopCriterion[];
};

export type KpiThreshold = "green" | "yellow" | "red";

export type KpiValue = {
  value: number | null;
  threshold: KpiThreshold | null;
};

export type CourierKpi = {
  courier_id: string;
  courier_name: string;
  speed_minutes: KpiValue;
  discipline_percent: KpiValue;
  productivity: KpiValue;
  help_count: number;
  deliveries_total: number;
  primary_shifts_worked: number;
  secondary_shifts_worked: number;
  primary_shifts_planned: number;
};

export type CourierEvaluationSummary = {
  score_sum: number;
  positive_count: number;
  negative_count: number;
  neutral_count: number;
  top_criteria: CourierEvaluationTopCriterion[];
};

export type CourierStatisticsRow = {
  kpi: CourierKpi;
  evaluations: CourierEvaluationSummary;
};

export type CourierEvaluationDetail = CourierEvaluation & {
  criterion_code: string | null;
  criterion_label: string | null;
  author_name: string | null;
};

export type CourierShiftBrief = {
  id: number;
  opened_at: string;
  closed_at: string | null;
  attendance_type: string;
  worked_minutes: number | null;
};

export type CourierDisciplineBreakdown = {
  planned: number;
  worked: number;
  help: number;
  no_show: number;
};

export type CourierStatisticsDetail = {
  courier_iiko_id: string;
  kpi: CourierKpi;
  evaluations: CourierEvaluationSummary;
  latest_evaluations: CourierEvaluationDetail[];
  last_shift: CourierShiftBrief | null;
  discipline_breakdown: CourierDisciplineBreakdown;
};

export type CourierListRow = {
  employee_id: string;
  full_name: string;
  iiko_id: string;
  is_courier_placeholder: boolean;
  status: string;
  work_status: CourierWorkStatus | null;
  open_shift_now: boolean;
  primary_shifts_in_month: number;
  secondary_shifts_in_month: number;
};

export type CourierListSummary = {
  active_total: number;
  fired_this_month: number;
  open_shift_now_total: number;
  reserve_total: number;
};

export type CourierListResponse = {
  month: string;
  summary: CourierListSummary;
  rows: CourierListRow[];
};

export type CourierScheduleEntry = {
  id: number | null;
  courier_employee_id: string;
  work_date: string;
  category: CourierScheduleCategory;
  planned_start_at: string;
  planned_end_at: string;
  comment: string | null;
  created_by: string;
  created_at: string | null;
  updated_at: string | null;
};

export type CourierScheduleMatchedEntry = {
  id: number | null;
  courier_employee_id: string;
  work_date: string;
  work_status: CourierWorkStatus | null;
  category: CourierScheduleCategory | null;
  planned_start_at: string | null;
  planned_end_at: string | null;
  comment: string | null;
  status: CourierScheduleMatchStatus | null;
  late_minutes: number | null;
  worked_minutes: number | null;
  deliveries_count: number | null;
  iiko_shift_id: number | null;
  opened_at: string | null;
  closed_at: string | null;
};

export type CourierAttendanceSyncResult = {
  total_records: number;
  matched_couriers: number;
  new: number;
  updated: number;
  unresolved_employees: number;
  errors: string[];
};

export type CourierScheduleUpsertPayload = {
  category: CourierScheduleCategory;
  planned_start_at?: string | null;
  planned_end_at?: string | null;
  comment?: string | null;
};

export type DdsProvider = "sber" | "tbank";
export type DdsDirection = "in" | "out";
export type DdsClassificationStatus =
  | "pending"
  | "classified"
  | "internal_transfer"
  | "needs_review"
  | "excluded";

export type WalletRead = {
  id: string;
  code: string;
  name: string;
  type: string;
  currency: string;
  is_internal_transfer_eligible: boolean;
  status: string;
  account_id: string | null;
  bank_code: string | null;
  opening_balance: string;
  opening_balance_date: string | null;
  balance: string;
  // Раскладка подотчётного Сейфа и Торговой кассы (целёвки); null для прочих.
  reserved_total: string | null;
  free_total: string | null;
  active_allocations: number | null;
  // Только Торговая касса: позиции «К выдаче» (целёвки в кассе + разрешения на авансы).
  pending_payout_count: number | null;
};

export type BankOperationRead = {
  id: string;
  provider: string;
  provider_operation_id: string;
  account_id: string | null;
  operation_date: string;
  posted_at: string | null;
  direction: string;
  amount: string;
  currency: string;
  counterparty_name_raw: string | null;
  counterparty_inn_raw: string | null;
  counterparty_account_raw: string | null;
  payment_purpose: string | null;
  document_number: string | null;
  classification_status: string;
  cashflow_transaction_id: string | null;
  transfer_group_id: string | null;
  raw_payload: Record<string, unknown> | null;
  // Карт-операция (получатель — эквайер): при ручной привязке к накладной диалог показывает
  // мягкое предупреждение вместо жёсткой ошибки.
  is_card: boolean;
};

export type BankOperationsQuery = {
  from?: string;
  to?: string;
  provider?: DdsProvider | "all";
  classification_status?: DdsClassificationStatus | "all";
  limit?: number;
  offset?: number;
};

export type CashflowTransactionRead = {
  id: string;
  wallet_id: string;
  direction: string;
  amount: string;
  operation_date: string;
  article_id: string | null;
  counterparty_id: string | null;
  transfer_group_id: string | null;
  source_kind: string;
  source_id: string | null;
  payment_purpose: string | null;
  comment: string | null;
  quality_status: string;
};

export type CashflowQuery = {
  from?: string;
  to?: string;
  wallet_id?: string;
  article_id?: string;
  direction?: DdsDirection | "all";
  limit?: number;
  offset?: number;
};

export type DdsArticleRead = {
  id: string;
  code: string;
  name: string;
  movement_type: "inflow" | "outflow" | "internal" | string;
  activity_type: string;
  parent_id: string | null;
  is_active: boolean;
  // Доступна администратору в «Выплате из кассы» (только расходные, без движковых).
  kassa_enabled: boolean;
  // Расход по статье обязан указывать помещение (аренда, коммуналка): фронт требует его в
  // строке разбора и в окне платежа, а для аренды предлагает арендодателей помещения.
  location_required: boolean;
  // Статья-аренда помещения: свободный «кому платим» скрыт, получатель — арендодатель договора.
  lease_bound: boolean;
  // Что расход по статье делает с основным средством. purchase — платёж покупает объект;
  // repair — капитальный ремонт, увеличивает стоимость; maintenance — текущий ремонт, стоимость
  // не трогает. Пусто — статья к ОС отношения не имеет, и объект на ней бэкенд отвергнет.
  asset_link_kind: "purchase" | "repair" | "maintenance" | null;
  description: string | null;
  aliases: DdsAliasRead[];
};

export type DdsArticleCreate = {
  code: string;
  name: string;
  movement_type: "inflow" | "outflow" | "internal";
  activity_type: string;
  parent_id?: string | null;
  is_active?: boolean;
  kassa_enabled?: boolean;
  description?: string | null;
};

export type DdsAliasRead = {
  id: string;
  alias: string;
  source: string | null;
};

export type DdsAliasCreate = {
  alias: string;
  source?: string | null;
};

export type ClassificationRuleRead = {
  id: string;
  name: string;
  priority: number;
  is_active: boolean;
  provider: DdsProvider | null;
  direction: DdsDirection | null;
  counterparty_inn_match: string | null;
  counterparty_name_pattern: string | null;
  purpose_pattern: string | null;
  amount_min: string | null;
  amount_max: string | null;
  action: "set_article" | "mark_internal_transfer" | "exclude" | string;
  article_id: string | null;
  counterparty_id: string | null;
  comment: string | null;
};

export type ClassificationRuleCreate = {
  name: string;
  priority?: number;
  is_active?: boolean;
  provider?: DdsProvider | null;
  direction?: DdsDirection | null;
  counterparty_inn_match?: string | null;
  counterparty_name_pattern?: string | null;
  purpose_pattern?: string | null;
  amount_min?: string | null;
  amount_max?: string | null;
  action: "set_article" | "mark_internal_transfer" | "exclude";
  article_id?: string | null;
  counterparty_id?: string | null;
  comment?: string | null;
};

export type OwnerReviewKind =
  | "unclassified_operation"
  | "invalid_credentials"
  | "unmatched_transfer"
  | "unconfirmed_cheque"
  | "payer_wallet_unresolved"
  | "iiko_payment_unsettled"
  | "iiko_cash_payout_unsettled"
  | "card_refund_after_cheque"
  | "cheque_refund_missing"
  | "deposit_bank_draft_failed";

export type ReconciliationCaseRead = {
  id: string;
  kind: string;
  status: string;
  provider: string | null;
  bank_operation_id: string | null;
  payload: Record<string, unknown>;
  created_at: string;
  operation: BankOperationRead | null;
};

export type OwnerReviewQuery = {
  kind?: OwnerReviewKind | "all";
  limit?: number;
  offset?: number;
};

export type ClassifyPayload = {
  article_id?: string | null;
  counterparty_id?: string | null;
  action: "set_article" | "mark_internal_transfer" | "exclude";
  remember_as_rule: boolean;
};

export type OperationSplitItem = {
  article_id: string;
  amount: string;
  comment?: string | null;
  // Для статьи «Оплата поставщикам»: накладная, которую гасит эта сумма (привязка операции).
  invoice_id?: string | null;
  // Для зарплатной статьи: сотрудник-получатель (заводит EmployeePayout → учёт в ведомости).
  employee_id?: string | null;
  // Контрагент ЭТОЙ строки: один платёж часто покрывает расходы разных контрагентов.
  counterparty_id?: string | null;
  // Аналитика «где»: помещение обязательно для статей с location_required, аренда — когда
  // платёж закрывает договор (арендодатель подставится в контрагента).
  location_id?: string | null;
  lease_id?: string | null;
  // Основное средство ЭТОЙ доли: один платёж покупает три стеллажа — это три разные карточки,
  // поэтому объект живёт в строке, а не в разборе целиком.
  asset_id?: string | null;
};

export type OperationClassifyPayload = {
  action: "split" | "mark_internal_transfer" | "exclude" | "mark_safe_topup" | "employee_advance";
  splits?: OperationSplitItem[];
  counterparty_id?: string | null;
  new_counterparty_name?: string | null;
  new_counterparty_inn?: string | null;
  remember_as_rule?: boolean;
  // Карт-операция: оператор явно подтвердил ручную привязку оплаты к накладной (получатель в
  // банке — эквайер, не поставщик). Требует права оплаты накладной; правило не запоминается.
  allow_card?: boolean;
  // Для action='employee_advance' (разбор операции как аванс/заём сотруднику).
  advance_kind?: "advance" | "loan" | null;
  advance_installment_amount?: string | null;
  advance_recovery_start_date?: string | null;
  advance_override_ceiling?: boolean;
};

export type CashflowSplitItem = {
  article_id: string;
  amount: string;
  comment?: string | null;
  // Счёт-получатель для строки со статьёй «перевод между счетами» (заводит встречную ногу).
  transfer_wallet_id?: string | null;
  // Для зарплатной статьи: сотрудник-получатель (заводит EmployeePayout → учёт в ведомости).
  employee_id?: string | null;
  // Контрагент ЭТОЙ строки (см. OperationSplitItem).
  counterparty_id?: string | null;
  location_id?: string | null;
  lease_id?: string | null;
  // Основное средство ЭТОЙ строки (см. OperationSplitItem).
  asset_id?: string | null;
};

// Полный разбор РУЧНОЙ проводки ДДС (без bank-операции), сохраняющий баланс кошелька.
export type CashflowClassifyPayload = {
  action: "split" | "exclude";
  splits?: CashflowSplitItem[];
  counterparty_id?: string | null;
};

export type CredentialRead = {
  id: string;
  provider: DdsProvider;
  credential_kind:
    | "access_token"
    | "client_secret"
    | "bearer_token"
    | "mtls_cert_path"
    | "mtls_key_path";
  is_active: boolean;
  expires_at: string | null;
  metadata: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
};

export type CredentialCreate = {
  provider: DdsProvider;
  credential_kind: CredentialRead["credential_kind"];
  value: string;
  expires_at?: string | null;
  metadata?: Record<string, unknown> | null;
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

export async function getAccessPermissions(): Promise<AccessPermissionModule[]> {
  const response = await api.get<AccessPermissionModule[]>("/access-control/permissions");
  return response.data;
}

export async function getAccessRoles(): Promise<AccessRole[]> {
  const response = await api.get<AccessRole[]>("/access-control/roles");
  return response.data;
}

export async function updateRolePermissions(roleId: string, codes: string[]): Promise<AccessRole> {
  const response = await api.put<AccessRole>(`/access-control/roles/${roleId}/permissions`, {
    permission_codes: codes,
  });
  return response.data;
}

export async function getAccessUsers(): Promise<AccessUser[]> {
  const response = await api.get<AccessUser[]>("/access-control/users");
  return response.data;
}

export async function createAccessUser(payload: AccessUserCreatePayload): Promise<AccessUser> {
  const response = await api.post<AccessUser>("/access-control/users", payload);
  return response.data;
}

export async function patchAccessUser(
  userId: string,
  payload: AccessUserPatchPayload,
): Promise<AccessUser> {
  const response = await api.patch<AccessUser>(`/access-control/users/${userId}`, payload);
  return response.data;
}

export async function assignUserRole(userId: string, roleCode: string): Promise<void> {
  await api.post(`/access-control/users/${userId}/roles`, { role_code: roleCode });
}

export async function revokeUserRole(userId: string, roleCode: string): Promise<void> {
  await api.delete(`/access-control/users/${userId}/roles/${encodeURIComponent(roleCode)}`);
}

export async function getAccessAudit(params: AccessAuditParams = {}): Promise<AccessAuditEvent[]> {
  const response = await api.get<AccessAuditEvent[]>("/access-control/audit", { params });
  return response.data;
}

export async function getDdsWallets(): Promise<WalletRead[]> {
  const response = await api.get<WalletRead[]>("/dds/wallets");
  return response.data;
}

export async function getDdsBankOperations(
  params: BankOperationsQuery,
): Promise<{ items: BankOperationRead[]; total: number }> {
  const response = await api.get<{ items: BankOperationRead[]; total: number }>(
    "/dds/bank-operations",
    { params: cleanDdsParams(params) },
  );
  return response.data;
}

export async function getDdsCashflow(
  params: CashflowQuery,
): Promise<{ items: CashflowTransactionRead[]; total: number }> {
  const response = await api.get<{ items: CashflowTransactionRead[]; total: number }>(
    "/dds/cashflow",
    { params: cleanDdsParams(params) },
  );
  return response.data;
}

export type JournalRow = {
  kind: "cashflow" | "operation";
  id: string;
  bank_operation_id: string | null;
  status: string;
  operation_date: string;
  occurred_at: string;
  direction: string;
  amount: string;
  article_id: string | null;
  counterparty_id: string | null;
  wallet_id: string | null;
  provider: string | null;
  payment_purpose: string | null;
  counterparty_name_raw: string | null;
  counterparty_inn_raw: string | null;
  // Карт-операция (получатель в банке — эквайер): при ручной привязке к накладной диалог
  // показывает мягкое предупреждение вместо жёсткой ошибки. Для проводок всегда false.
  is_card: boolean;
  // Основное средство проводки. У банк-операции объект берётся из /split, а у РУЧНОЙ
  // проводки другого источника нет — без него переоткрытие разбора снимало бы привязку.
  asset_id?: string | null;
};

export type JournalQuery = {
  status?: "all" | "marked" | "unmarked" | "transfers";
  from?: string;
  to?: string;
  direction?: "in" | "out" | "all";
  wallet_id?: string;
  article_id?: string;
  counterparty_id?: string;
  limit?: number;
  offset?: number;
};

export type JournalListResponse = {
  items: JournalRow[];
  total: number;
  marked_total: number;
  unmarked_total: number;
  transfer_total: number;
};

export async function getDdsJournal(params: JournalQuery): Promise<JournalListResponse> {
  const response = await api.get<JournalListResponse>("/dds/journal", {
    params: cleanDdsParams(params),
  });
  return response.data;
}

export async function getDdsArticles(): Promise<DdsArticleRead[]> {
  const response = await api.get<DdsArticleRead[]>("/dds/articles");
  return response.data;
}

// --- Окно «Новый платёж» (FAB): контекст формы и «просто трата» без получателя ---

export type NewPaymentFlow =
  | "expense"
  | "income"
  | "employee_payout"
  | "employee_advance"
  | "employee_loan"
  | "supplier_prepayment"
  | "internal_transfer";

export type NewPaymentArticleCounterparty = {
  counterparty_id: string;
  name: string;
  inn: string | null;
  relationship: "official" | "informal" | "barter";
  has_requisites: boolean;
  requisites_verified: boolean;
  service_period_required: boolean;
  default_service_period_offset_months: number | null;
};

export type NewPaymentArticle = {
  id: string;
  code: string;
  name: string;
  flow: NewPaymentFlow;
  // Вид деятельности — леджер-фильтр палитры (operating/financing/investing).
  activity?: string | null;
  // Закреплённые за статьёй контрагенты — «кому платим» для свободного вывода.
  counterparties?: NewPaymentArticleCounterparty[];
  // Статье нужна аналитика по помещению (аренда): форма требует помещение и арендодателя.
  location_required?: boolean;
  // Статья-аренда помещения: свободный «кому платим» скрыт, получатель — арендодатель договора.
  lease_bound?: boolean;
};

export type NewPaymentWallet = {
  id: string;
  code: string;
  name: string;
  bank_code: string | null;
  // kind=bank → банковский черновик; kind=cash → наличными, сразу резерв.
  kind: "bank" | "cash";
  // Для наличных: safe (Сейф) / kassa (Торговая касса). null у банковских.
  location: "safe" | "kassa" | null;
};

export type NewPaymentEmployee = {
  id: string;
  full_name: string;
  position: string | null;
  on_demand: boolean;
};

export type NewPaymentContext = {
  articles: NewPaymentArticle[];
  wallets: NewPaymentWallet[];
  employees: NewPaymentEmployee[];
};

export type NewPaymentExpenseLine = {
  article_id: string;
  amount: number;
  purpose: string;
  // Необязательная атрибуция: кому платим (для статей с привязанными контрагентами).
  counterparty_id?: string | null;
  service_period_start?: string | null;
  service_period_end?: string | null;
  location_id?: string | null;
  lease_id?: string | null;
};

export type NewPaymentExpenseDraftPayload = {
  // Транш свободного вывода на Сейф: одна или несколько строк (статья, сумма, назначение).
  lines: NewPaymentExpenseLine[];
  // Банк-плательщик черновика: bank_draft (Т-Банк, по умолчанию) или bank_draft_sber (Сбер).
  channel?: "bank_draft" | "bank_draft_sber";
  // Явное подтверждение вывода на карту ИП для официального контрагента без реквизитов.
  allow_official_via_safe?: boolean;
};

export type NewPaymentExpenseDraft = {
  id: string;
  amount: number;
  status: string;
  provider_ref: string | null;
  last_error: string | null;
  created_at: string;
};

export async function getNewPaymentContext(): Promise<NewPaymentContext> {
  const response = await api.get<NewPaymentContext>("/dds/new-payment/context");
  return response.data;
}

export async function createNewPaymentExpenseDraft(
  payload: NewPaymentExpenseDraftPayload,
): Promise<NewPaymentExpenseDraft> {
  const response = await api.post<NewPaymentExpenseDraft>(
    "/dds/new-payment/expense-draft",
    payload,
  );
  return response.data;
}

export type ExpenseCashReservePayload = {
  wallet_id: string;
  lines: NewPaymentExpenseLine[];
  // «Создать платёж»: сразу оплатить каждый резерв (деньги ушли). Требует finance.safe.confirm_paid.
  pay_now?: boolean;
};

export type ExpenseCashReserveResult = {
  created: number;
  total: number;
  location: "safe" | "kassa";
  paid?: boolean;
};

// Свободный вывод НАЛИЧНЫМИ: создаёт резерв(ы) на Сейфе/в Кассе (без банковского черновика).
export async function createExpenseCashReserves(
  payload: ExpenseCashReservePayload,
): Promise<ExpenseCashReserveResult> {
  const response = await api.post<ExpenseCashReserveResult>(
    "/dds/new-payment/expense-cash",
    payload,
  );
  return response.data;
}

export type NewPaymentIncomeResult = {
  created: number;
  total: number;
  location: "safe" | "kassa";
};

// Наличное поступление: in-проводка(и) на Сейф/в Кассу сразу (приход — факт, не намерение).
export async function createNewPaymentIncome(payload: {
  wallet_id: string;
  lines: NewPaymentExpenseLine[];
}): Promise<NewPaymentIncomeResult> {
  const response = await api.post<NewPaymentIncomeResult>("/dds/new-payment/income-cash", payload);
  return response.data;
}

export type InternalTransferPayload = {
  source_wallet_id: string;
  dest_wallet_id: string;
  mode: "plain" | "targeted";
  amount?: number;
  purpose?: string | null;
  lines?: NewPaymentExpenseLine[];
};

export type InternalTransferResult = { transfer_id: string; amount: number; reserves: number };

// Внутренний перевод между наличными счетами (Сейф↔Касса): обычный или целевой (с резервом).
export async function createInternalTransfer(
  payload: InternalTransferPayload,
): Promise<InternalTransferResult> {
  const response = await api.post<InternalTransferResult>("/dds/internal-transfer", payload);
  return response.data;
}

export type NewPaymentTransferPayload = {
  source_wallet_id: string;
  dest_wallet_id: string;
  amount: number;
  purpose?: string | null;
};

export type NewPaymentTransferResult = {
  kind: "transfer" | "draft";
  amount: number;
  draft_id: string | null;
};

// Обычный внутренний перевод из «Нового платежа»: наличный источник → перевод,
// банковский → черновик-пополнение Сейфа.
export async function createNewPaymentInternalTransfer(
  payload: NewPaymentTransferPayload,
): Promise<NewPaymentTransferResult> {
  const response = await api.post<NewPaymentTransferResult>(
    "/dds/new-payment/internal-transfer",
    payload,
  );
  return response.data;
}

export async function createDdsArticle(payload: DdsArticleCreate): Promise<DdsArticleRead> {
  const response = await api.post<DdsArticleRead>("/dds/articles", payload);
  return response.data;
}

export async function patchDdsArticle(
  id: string,
  payload: Partial<DdsArticleCreate>,
): Promise<DdsArticleRead> {
  const response = await api.patch<DdsArticleRead>(`/dds/articles/${id}`, payload);
  return response.data;
}

export async function deleteDdsArticle(id: string): Promise<void> {
  await api.delete(`/dds/articles/${id}`);
}

export async function createDdsArticleAlias(
  articleId: string,
  payload: DdsAliasCreate,
): Promise<DdsAliasRead> {
  const response = await api.post<DdsAliasRead>(`/dds/articles/${articleId}/aliases`, payload);
  return response.data;
}

export async function deleteDdsArticleAlias(aliasId: string): Promise<void> {
  await api.delete(`/dds/articles/aliases/${aliasId}`);
}

// Неоплаченные накладные контрагента — для привязки оплаты при разборе операции
// (статья «Оплата поставщикам»). direction=payable: гасим только наши обязательства.
export type DdsUnpaidInvoice = {
  id: string;
  number: string | null;
  invoice_date: string | null;
  amount: number;
  remaining: number;
  payment_status: string;
};

export async function getDdsUnpaidInvoices(counterpartyId: string): Promise<DdsUnpaidInvoice[]> {
  const response = await api.get<DdsUnpaidInvoice[]>("/counterparties/invoices", {
    params: {
      counterparty_id: counterpartyId,
      status: "unpaid,partially_paid",
      direction: "payable",
    },
  });
  return response.data;
}

// Текущий разбор банк-операции: диалог открывается на том, что уже размечено, а не с чистой
// строки на всю сумму (иначе повторный разбор пришлось бы набивать заново).
export type DdsOperationSplitLine = {
  cashflow_transaction_id: string;
  article_id: string | null;
  amount: string;
  counterparty_id: string | null;
  invoice_id: string | null;
  employee_id: string | null;
  location_id: string | null;
  lease_id: string | null;
  // Основное средство доли. Без него повторное открытие диалога отдавало бы пустое поле, и
  // «Разнести» сняло бы привязку, по которой покупка стоит на балансе.
  asset_id: string | null;
};

export type DdsOperationSplit = {
  bank_operation_id: string;
  amount: string;
  classification_status: string;
  lines: DdsOperationSplitLine[];
};

export async function getDdsOperationSplit(operationId: string): Promise<DdsOperationSplit> {
  const response = await api.get<DdsOperationSplit>(`/dds/operations/${operationId}/split`);
  return response.data;
}

// Сотрудники для привязки выплаты при разборе операции журнала (зарплатная статья):
// активные + увольняемые + уволенные. on_demand — режим оклада «по востребованию».
export type DdsPayoutEmployee = {
  id: string;
  full_name: string;
  position: string | null;
  on_demand: boolean;
  status: string;
};

export async function getDdsPayoutEmployees(): Promise<DdsPayoutEmployee[]> {
  const response = await api.get<DdsPayoutEmployee[]>("/dds/payout-employees");
  return response.data;
}

export async function getDdsClassificationRules(): Promise<ClassificationRuleRead[]> {
  const response = await api.get<ClassificationRuleRead[]>("/dds/classification-rules");
  return response.data;
}

export async function createClassificationRule(
  payload: ClassificationRuleCreate,
): Promise<ClassificationRuleRead> {
  const response = await api.post<ClassificationRuleRead>("/dds/classification-rules", payload);
  return response.data;
}

export async function patchClassificationRule(
  id: string,
  payload: Partial<ClassificationRuleCreate>,
): Promise<ClassificationRuleRead> {
  const response = await api.patch<ClassificationRuleRead>(
    `/dds/classification-rules/${id}`,
    payload,
  );
  return response.data;
}

export async function deleteClassificationRule(id: string): Promise<void> {
  await api.delete(`/dds/classification-rules/${id}`);
}

export async function toggleClassificationRule(id: string): Promise<ClassificationRuleRead> {
  const response = await api.post<ClassificationRuleRead>(`/dds/classification-rules/${id}/toggle`);
  return response.data;
}

export async function getDdsOwnerReview(
  params: OwnerReviewQuery,
): Promise<{ items: ReconciliationCaseRead[]; total: number }> {
  const response = await api.get<{ items: ReconciliationCaseRead[]; total: number }>(
    "/dds/owner-review",
    { params: cleanDdsParams(params) },
  );
  return response.data;
}

export async function classifyOwnerReviewCase(
  caseId: string,
  payload: ClassifyPayload,
): Promise<void> {
  await api.post(`/dds/owner-review/${caseId}/classify`, payload);
}

export async function classifyOperation(
  operationId: string,
  payload: OperationClassifyPayload,
): Promise<void> {
  await api.post(`/dds/operations/${operationId}/classify`, payload);
}

export async function classifyCashflowTransaction(
  transactionId: string,
  payload: CashflowClassifyPayload,
): Promise<void> {
  await api.post(`/dds/transactions/${transactionId}/classify`, payload);
}

export async function dismissOwnerReviewCase(caseId: string): Promise<void> {
  await api.post(`/dds/owner-review/${caseId}/dismiss`);
}

export async function applyCardRefundCase(caseId: string, invoiceId?: string): Promise<void> {
  // «Учесть возврат»: если возврат относится к чеку (единственный кандидат или выбранный
  // invoiceId при неоднозначности) — привязка к чеку гасит его ожидание; иначе (сирота) —
  // входящая проводка «Возврат расходов». Чек и iiko не мутируются.
  await api.post(`/dds/owner-review/${caseId}/apply-card-refund`, null, {
    params: invoiceId ? { invoice_id: invoiceId } : undefined,
  });
}

// «Повторить отправку» по кейсу «оплата в iiko не проведена»: снять кап и переотправить в iiko
// (синхронно для одиночного банка; иначе сверочный джоб добьёт). status='resolved' → успех.
export async function retryIikoPaymentCase(
  caseId: string,
): Promise<{ status: string; iiko_payment_push_error?: string | null }> {
  const response = await api.post<{ status: string; iiko_payment_push_error?: string | null }>(
    `/dds/owner-review/${caseId}/retry-iiko-payment`,
  );
  return response.data;
}

// «Оплата проведена вручную» по кейсу: ok-маркер (джоб больше не шлёт) + закрытие кейса.
export async function confirmIikoManualCase(caseId: string): Promise<void> {
  await api.post(`/dds/owner-review/${caseId}/confirm-iiko-manual`);
}

export type SafeAllocationRead = {
  id: string;
  wallet_id: string;
  amount: string;
  amount_paid: string;
  outstanding: string;
  article_id: string | null;
  counterparty_id: string | null;
  counterparty_name: string | null;
  purpose: string | null;
  // Происхождение авто-резерва: черновик выплаты на карту ИП (неофициальный поставщик).
  source_draft_id: string | null;
  status: "reserved" | "partially_paid" | "paid" | "cancelled";
  // Где живёт целёвка: 'safe' — на карте «Сейф», 'kassa' — передана в Торговую кассу.
  location: "safe" | "kassa";
  created_at: string;
};

export type SafeAllocationCreatePayload = {
  amount: string | number;
  article_id: string;
  counterparty_id?: string | null;
  purpose?: string | null;
  pay_full?: boolean;
};

export async function getSafeAllocations(
  walletId: string,
  status: "active" | "all" = "active",
): Promise<SafeAllocationRead[]> {
  const response = await api.get<SafeAllocationRead[]>(`/dds/wallets/${walletId}/allocations`, {
    params: { status },
  });
  return response.data;
}

export async function createSafeAllocation(
  walletId: string,
  payload: SafeAllocationCreatePayload,
): Promise<SafeAllocationRead> {
  const response = await api.post<SafeAllocationRead>(
    `/dds/wallets/${walletId}/allocations`,
    payload,
  );
  return response.data;
}

export async function paySafeAllocation(
  allocationId: string,
  amount: string | number,
): Promise<SafeAllocationRead> {
  const response = await api.post<SafeAllocationRead>(`/dds/allocations/${allocationId}/pay`, {
    amount,
  });
  return response.data;
}

export async function cancelSafeAllocation(allocationId: string): Promise<SafeAllocationRead> {
  const response = await api.post<SafeAllocationRead>(`/dds/allocations/${allocationId}/cancel`);
  return response.data;
}

/** «Передать в кассу»: целёвка переезжает в ТК Черникова вместе с деньгами (весь остаток). */
export async function transferSafeAllocationToKassa(
  allocationId: string,
): Promise<SafeAllocationRead> {
  const response = await api.post<SafeAllocationRead>(
    `/dds/allocations/${allocationId}/transfer-to-kassa`,
  );
  return response.data;
}

// Целёвки в Торговой кассе + ожидающие разрешения (read-only диалог «Денег сегодня»).
export type DdsKassaTarget = {
  id: string;
  article_id: string | null;
  article_name: string | null;
  counterparty_id: string | null;
  counterparty_name: string | null;
  purpose: string | null;
  amount: number;
  amount_paid: number;
  outstanding: number;
  from_bank_payout: boolean;
  // Пул ведомости выдаётся через «Активные платежи», не как обычная целёвка кассы.
  is_payroll: boolean;
  created_at: string;
};

export type DdsKassaAdvancePermission = {
  id: string;
  employee_id: string;
  employee_name: string;
  kind: "advance" | "loan";
  amount: number;
  comment: string | null;
  created_by_label: string | null;
  created_at: string;
};

export type DdsKassaTargets = {
  wallet_name: string;
  balance: number;
  targets: DdsKassaTarget[];
  permissions: DdsKassaAdvancePermission[];
  targets_total: number;
  pending_count: number;
};

export async function getDdsKassaTargets(): Promise<DdsKassaTargets> {
  const response = await api.get<DdsKassaTargets>("/dds/kassa-targets");
  return response.data;
}

export async function withdrawSafeCash(
  walletId: string,
  amount: string | number,
): Promise<WalletRead> {
  const response = await api.post<WalletRead>(`/dds/wallets/${walletId}/withdraw-cash`, { amount });
  return response.data;
}

export type SafeReconcileResult = {
  accounted: string;
  actual: string;
  delta: string;
  adjusted: boolean;
};

export async function reconcileSafe(
  walletId: string,
  actualBalance: string | number,
  applyAdjustment: boolean,
): Promise<SafeReconcileResult> {
  const response = await api.post<SafeReconcileResult>(`/dds/wallets/${walletId}/reconcile`, {
    actual_balance: actualBalance,
    apply_adjustment: applyAdjustment,
  });
  return response.data;
}

export async function triggerBankSync(
  provider: DdsProvider,
  payload: { date_from: string; date_to: string },
): Promise<{ job_id: string; status: string }> {
  const response = await api.post<{ job_id: string; status: string }>(
    `/dds/bank-sync/${provider}`,
    payload,
  );
  return response.data;
}

export async function getDdsCredentials(): Promise<CredentialRead[]> {
  const response = await api.get<CredentialRead[]>("/dds/credentials");
  return response.data;
}

export async function createDdsCredential(payload: CredentialCreate): Promise<CredentialRead> {
  const response = await api.post<CredentialRead>("/dds/credentials", payload);
  return response.data;
}

export async function deleteDdsCredential(id: string): Promise<void> {
  await api.delete(`/dds/credentials/${id}`);
}

export async function getSubstitutePairs(): Promise<SubstitutePairsResponse> {
  const response = await api.get<SubstitutePairsResponse>("/settings/substitute-pairs");
  return response.data;
}

export async function updateSubstitutePairs(
  pairs: SubstitutePair[],
): Promise<SubstitutePairsResponse> {
  const response = await api.put<SubstitutePairsResponse>("/settings/substitute-pairs", { pairs });
  return response.data;
}

export type LocationKind = "point" | "warehouse" | "office";

export type LocationRecord = {
  id: string;
  name: string;
  kind: LocationKind;
  status: "active" | "inactive";
  address: string | null;
  iiko_organization_id: string | null;
  iiko_department_id: string | null;
  iiko_store_ids: string[];
  opened_on: string | null;
  closed_on: string | null;
  note: string | null;
  iiko_linked: boolean;
};

export type LocationPayload = {
  name: string;
  kind: LocationKind;
  address?: string | null;
  iiko_organization_id?: string | null;
  iiko_department_id?: string | null;
  iiko_store_ids?: string[];
  opened_on?: string | null;
  closed_on?: string | null;
  note?: string | null;
  status?: "active" | "inactive";
};

export async function getLocations(): Promise<LocationRecord[]> {
  const response = await api.get<{ items: LocationRecord[] }>("/locations");
  return response.data.items;
}

export type IikoDirectoryItem = { id: string; name: string };

export type IikoDirectory = {
  // live — данные из iiko; mock — демо на стенде без iiko; unavailable — iiko не ответил (прод).
  source: "live" | "mock" | "unavailable";
  organizations: IikoDirectoryItem[];
  departments: IikoDirectoryItem[];
  stores: IikoDirectoryItem[];
};

/** Организации/подразделения/склады из iiko — чтобы выбрать привязку помещения, а не вводить ID. */
export async function getIikoDirectory(): Promise<IikoDirectory> {
  const response = await api.get<IikoDirectory>("/locations/iiko-directory");
  return response.data;
}

export async function createLocation(payload: LocationPayload): Promise<LocationRecord> {
  const response = await api.post<LocationRecord>("/locations", payload);
  return response.data;
}

export async function updateLocation(
  id: string,
  payload: LocationPayload,
): Promise<LocationRecord> {
  const response = await api.patch<LocationRecord>(`/locations/${id}`, payload);
  return response.data;
}

export type LeaseRecord = {
  id: string;
  location_id: string;
  location_name: string;
  counterparty_id: string;
  counterparty_name: string;
  monthly_amount: number;
  payment_day: number | null;
  payment_mode: "prepaid" | "postpaid";
  documents_mode: "official" | "informal";
  deposit_amount: number;
  started_on: string;
  ended_on: string | null;
  note: string | null;
  dds_article_id: string | null;
  is_active: boolean;
};

/** Данные арендодателя. Для официальной аренды реквизиты обязательны. */
export type LandlordInput = {
  counterparty_id?: string | null;
  name?: string | null;
  inn?: string | null;
  bank_bik?: string | null;
  bank_account?: string | null;
  corr_account?: string | null;
};

/** Условия аренды с ТЕМ ЖЕ арендодателем — смена собственника идёт отдельным действием. */
export type LeaseTermsPayload = {
  monthly_amount: number;
  payment_day?: number | null;
  payment_mode: "prepaid" | "postpaid";
  documents_mode: "official" | "informal";
  deposit_amount: number;
  started_on: string;
  ended_on?: string | null;
  note?: string | null;
  dds_article_id?: string | null;
};

export type LeasePayload = LeaseTermsPayload & { landlord: LandlordInput };

export type LandlordReplacement = {
  previous: LeaseRecord;
  current: LeaseRecord;
  previous_archived: boolean;
};

export async function getLocationLeases(locationId: string): Promise<LeaseRecord[]> {
  const response = await api.get<{ items: LeaseRecord[] }>(`/locations/${locationId}/leases`);
  return response.data.items;
}

export type LeaseAccrual = {
  invoice_id: string;
  number: string | null;
  invoice_date: string | null;
  amount: number;
  paid_amount: number;
  // 'unpaid' | 'partially_paid' | 'paid' | …
  payment_status: string;
  // 'active' — обязательство в силе (в КЗ); 'pending' — будущий документ, ждёт своей даты.
  activation_status: string;
  period_start: string | null;
  period_end: string | null;
};

export type LeaseLedger = {
  accruals: LeaseAccrual[];
  accrued_total: number;
  paid_total: number;
  outstanding_total: number;
  deposit_outstanding: number;
};

/** Начисления и оплаты по договору аренды: что начислено, что оплачено, сколько висит залога. */
export async function getLeaseLedger(locationId: string, leaseId: string): Promise<LeaseLedger> {
  const response = await api.get<LeaseLedger>(`/locations/${locationId}/leases/${leaseId}/ledger`);
  return response.data;
}

/** Пересобрать обязательство за месяц (по умолчанию текущий) под текущие условия договора. */
export async function rebuildLeaseAccrual(
  locationId: string,
  leaseId: string,
  month?: string,
): Promise<LeaseLedger> {
  const response = await api.post<LeaseLedger>(
    `/locations/${locationId}/leases/${leaseId}/accruals/rebuild`,
    undefined,
    { params: month ? { month } : undefined },
  );
  return response.data;
}

export async function createLocationLease(
  locationId: string,
  payload: LeasePayload,
): Promise<LeaseRecord> {
  const response = await api.post<LeaseRecord>(`/locations/${locationId}/leases`, payload);
  return response.data;
}

export async function updateLocationLease(
  locationId: string,
  leaseId: string,
  payload: LeaseTermsPayload,
): Promise<LeaseRecord> {
  const response = await api.patch<LeaseRecord>(
    `/locations/${locationId}/leases/${leaseId}`,
    payload,
  );
  return response.data;
}

/** Закрытие аренды — так оформляется смена арендодателя, прошлые месяцы остаются за прежним. */
export async function closeLocationLease(
  locationId: string,
  leaseId: string,
  endedOn: string,
): Promise<LeaseRecord> {
  const response = await api.post<LeaseRecord>(`/locations/${locationId}/leases/${leaseId}/close`, {
    ended_on: endedOn,
  });
  return response.data;
}

/** Смена собственника: прежняя аренда закрывается, новая заводится отдельной строкой. */
export async function replaceLeaseLandlord(
  locationId: string,
  leaseId: string,
  payload: { landlord: LandlordInput; terms: LeaseTermsPayload; previous_ended_on: string },
): Promise<LandlordReplacement> {
  const response = await api.post<LandlordReplacement>(
    `/locations/${locationId}/leases/${leaseId}/replace-landlord`,
    payload,
  );
  return response.data;
}

export type LocationLeaseOption = {
  lease_id: string;
  counterparty_id: string;
  counterparty_name: string;
  monthly_amount: number;
  payment_day: number | null;
  documents_mode: "official" | "informal";
  // Реквизитный контур арендодателя — окно платежа выбирает канал (банк / карта ИП / наличные).
  relationship: string;
  has_requisites: boolean;
  requisites_verified: boolean;
};

export type LocationOption = {
  location_id: string;
  location_name: string;
  kind: LocationKind;
  status: "active" | "inactive";
  leases: LocationLeaseOption[];
};

export type AssetOption = {
  asset_id: string;
  inventory_number: string | null;
  name: string;
  brand_model: string | null;
  location_name: string | null;
  status: "in_use" | "in_storage" | "not_working";
  status_title: string;
  initial_cost: string | number;
};

/** Объекты для выбора в разборе платежа по статье, привязанной к основным средствам.
 *
 * Свой лёгкий маршрут, не реестр: реестр требует права бухгалтера по ОС и тянет начисления по
 * каждой карточке. Выбывшие объекты сюда не приходят — гейт их всё равно отклонит.
 */
export async function getAssetOptions(): Promise<AssetOption[]> {
  const response = await api.get<{ items: AssetOption[] }>("/fixed-assets/options");
  return response.data.items;
}

export type AssetCategoryOption = {
  id: string;
  name: string;
  useful_life_months: number;
  note: string | null;
};

/** Категории ОС со сроками — для заведения карточки прямо из разбора платежа. */
export async function getAssetCategories(): Promise<AssetCategoryOption[]> {
  const response = await api.get<{ items: AssetCategoryOption[] }>("/fixed-assets/categories");
  return response.data.items;
}

/** Завести карточку ОС из платежа: стоимость = сумма строки, оценка = «по платежу».
 *
 * Купили новое — карточки ещё нет, и привязывать в разборе не к чему. Без этого пути покупка
 * уходит в расход мимо баланса: оператор выберет статью попроще, лишь бы платёж провёлся.
 */
export async function createAssetFromPayment(payload: {
  name: string;
  initial_cost: string;
  category_id?: string | null;
  location_id?: string | null;
  brand_model?: string | null;
  commissioned_on?: string | null;
}): Promise<AssetOption> {
  const response = await api.post<AssetOption>("/fixed-assets/from-payment", payload);
  return response.data;
}

/** Помещения и их арендодатели для платежа по статье с аналитикой по помещению. */
export async function getLocationOptionsForArticle(
  articleId: string,
  onDate?: string,
): Promise<LocationOption[]> {
  const response = await api.get<{ items: LocationOption[] }>(
    `/locations/options/for-article/${articleId}`,
    { params: onDate ? { on_date: onDate } : undefined },
  );
  return response.data.items;
}

export async function getCounterpartyLeases(counterpartyId: string): Promise<LeaseRecord[]> {
  const response = await api.get<{ items: LeaseRecord[] }>(
    `/locations/leases/by-counterparty/${counterpartyId}`,
  );
  return response.data.items;
}

export async function getPositions(): Promise<Position[]> {
  const response = await api.get<{ positions: Position[] }>("/settings/positions");
  return response.data.positions;
}

export async function createPosition(payload: PositionPayload): Promise<Position> {
  const response = await api.post<Position>("/settings/positions", payload);
  return response.data;
}

export async function updatePosition(
  id: string,
  payload: Partial<PositionPayload>,
): Promise<Position> {
  const response = await api.patch<Position>(`/settings/positions/${id}`, payload);
  return response.data;
}

export async function excludePosition(id: string): Promise<Position> {
  const response = await api.post<Position>(`/settings/positions/${id}/exclude`);
  return response.data;
}

export async function restorePosition(id: string): Promise<Position> {
  const response = await api.post<Position>(`/settings/positions/${id}/restore`);
  return response.data;
}

export async function deletePosition(id: string): Promise<void> {
  await api.delete(`/settings/positions/${id}`);
}

export async function syncPositionsWithIiko(): Promise<PositionsSyncResult> {
  const response = await api.post<PositionsSyncResult>("/settings/positions/sync-iiko");
  return response.data;
}

export async function getEmployees(filters: {
  status?: EmployeeStatus | "all";
  category?: EmployeeCategory;
  cookingStation?: CookingStation;
  includePending?: boolean;
  presentFrom?: string;
  presentTo?: string;
}): Promise<Employee[]> {
  const params: Record<string, string | boolean> = {};
  if (filters.status && filters.status !== "all") {
    params.status = filters.status;
  }
  if (filters.category) {
    params.category = filters.category;
  }
  if (filters.cookingStation) {
    params.cooking_station = filters.cookingStation;
  }
  if (filters.includePending) {
    params.include_pending = true;
  }
  if (filters.presentFrom) {
    params.present_from = filters.presentFrom;
  }
  if (filters.presentTo) {
    params.present_to = filters.presentTo;
  }
  const response = await api.get<Employee[]>("/employees/", { params });
  return response.data;
}

export async function patchEmployee(id: string, patch: EmployeePatch): Promise<Employee> {
  const response = await api.patch<Employee>(`/employees/${id}`, patch);
  return response.data;
}

/** Официальный контур сотрудника — субресурс под правом staff.official.read/manage. */
export interface OfficialProfile {
  is_official: boolean;
  official_full_name: string | null;
  official_tab_number: string | null;
  official_salary: string | null;
  /** Вычет НДФЛ не вводится руками: считается из числа детей (ст. 218 НК). */
  official_children_count: number;
  official_single_parent: boolean;
  official_status: "working" | "maternity_leave";
  /** Посчитанный сервером вычет, ₽/мес — только для показа. */
  ndfl_deduction_monthly?: string;
}

export async function fetchOfficialProfile(employeeId: string): Promise<OfficialProfile> {
  const response = await api.get<OfficialProfile>(`/employees/${employeeId}/official`);
  return response.data;
}

export async function putOfficialProfile(
  employeeId: string,
  payload: OfficialProfile,
): Promise<OfficialProfile> {
  const response = await api.put<OfficialProfile>(`/employees/${employeeId}/official`, payload);
  return response.data;
}

export async function changeEmployeePosition(
  employeeId: string,
  payload: EmployeePositionChangePayload,
): Promise<EmployeePositionAssignment> {
  const response = await api.patch<EmployeePositionAssignment>(
    `/employees/${employeeId}/position`,
    payload,
  );
  return response.data;
}

export async function getEmployeePositionHistory(
  employeeId: string,
): Promise<EmployeePositionAssignment[]> {
  const response = await api.get<EmployeePositionAssignment[]>(
    `/employees/${employeeId}/position-history`,
  );
  return response.data;
}

export async function patchEmployeePositionAssignment(
  employeeId: string,
  assignmentId: string,
  payload: EmployeePositionAssignmentPatch,
): Promise<EmployeePositionAssignment> {
  const response = await api.patch<EmployeePositionAssignment>(
    `/employees/${employeeId}/position-assignments/${assignmentId}`,
    payload,
  );
  return response.data;
}

export async function deleteEmployeePositionAssignment(
  employeeId: string,
  assignmentId: string,
  payload: EmployeePositionAssignmentDeletePayload,
): Promise<{ ok: boolean; warnings?: PayrollImpactWarning[] }> {
  const response = await api.delete<{ ok: boolean; warnings?: PayrollImpactWarning[] }>(
    `/employees/${employeeId}/position-assignments/${assignmentId}`,
    { data: payload },
  );
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

export async function patchFreelancerCard(
  employeeId: string,
  payload: FreelancerCardPatchPayload,
): Promise<Employee> {
  const response = await api.patch<Employee>(`/employees/${employeeId}/freelancer-card`, payload);
  return response.data;
}

export async function listFreelancerAttendanceCases(
  status: "open" | "resolved" | "dismissed" | "all" = "open",
): Promise<FreelancerAttendanceCase[]> {
  const response = await api.get<FreelancerAttendanceCase[]>(
    "/employees/freelancer/attendance-cases",
    { params: { status } },
  );
  return response.data;
}

export async function dismissFreelancerAttendanceCase(
  caseId: string,
): Promise<FreelancerAttendanceCase> {
  const response = await api.post<FreelancerAttendanceCase>(
    `/employees/freelancer/attendance-cases/${caseId}/dismiss`,
  );
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

export async function cancelDismissal(id: string): Promise<Employee> {
  const response = await api.post<Employee>(`/employees/${id}/cancel-dismissal`);
  return response.data;
}

export async function getEmployeeAssignments(
  id: string,
  includePending = false,
): Promise<EmployeeRoleAssignment[]> {
  const response = await api.get<EmployeeRoleAssignment[]>(`/employees/${id}/assignments`, {
    params: { include_pending: includePending || undefined },
  });
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

export async function autoCreateNextAdminPayrollPeriod(): Promise<PayrollPeriod> {
  const response = await api.post<PayrollPeriod>("/payroll/admin/periods/auto-create-next");
  return response.data;
}

export async function createAdminPayrollRun(
  options: { periodId?: string | null; forceRefresh?: boolean } = {},
): Promise<PayrollRun> {
  const response = await api.post<PayrollRun>("/payroll/admin/runs", {
    period_id: options.periodId ?? null,
    force_refresh: options.forceRefresh ?? false,
  });
  return response.data;
}

export async function getAdminPayrollRuns(): Promise<PayrollRun[]> {
  const response = await api.get<PayrollRun[]>("/payroll/admin/runs");
  return response.data;
}

export async function getAdminPayrollRun(id: string): Promise<PayrollRun> {
  const response = await api.get<PayrollRun>(`/payroll/admin/runs/${id}`);
  return response.data;
}

export async function getAdminPayrollRunLines(id: string): Promise<PayrollLine[]> {
  const response = await api.get<PayrollLine[]>(`/payroll/admin/runs/${id}/lines`);
  return response.data;
}

export async function getAdminSalaries(): Promise<AdminSalariesResponse> {
  const response = await api.get<AdminSalariesResponse>("/payroll/admin/salaries");
  return response.data;
}

export async function putAdminSalaryDefault(
  payload: AdminSalaryDefaultPayload,
): Promise<AdminSalariesResponse> {
  const response = await api.put<AdminSalariesResponse>("/payroll/admin/salaries/default", payload);
  return response.data;
}

export async function putAdminSalaryOverride(
  employeeId: string,
  payload: AdminSalaryOverridePayload,
): Promise<AdminSalariesResponse> {
  const response = await api.put<AdminSalariesResponse>(
    `/payroll/admin/salaries/override/${employeeId}`,
    payload,
  );
  return response.data;
}

export async function deleteAdminSalaryOverride(
  employeeId: string,
): Promise<AdminSalariesResponse> {
  const response = await api.delete<AdminSalariesResponse>(
    `/payroll/admin/salaries/override/${employeeId}`,
  );
  return response.data;
}

export async function setAdminPayrollExclusion(
  employeeId: string,
  excluded: boolean,
): Promise<{ employee_id: string; admin_payroll_excluded: boolean }> {
  const response = await api.put<{ employee_id: string; admin_payroll_excluded: boolean }>(
    `/payroll/admin/salaries/exclusion/${employeeId}`,
    { excluded },
  );
  return response.data;
}

export async function putAdminPayoutMode(
  position: string,
  mode: AdminPayoutMode,
): Promise<AdminSalariesResponse> {
  const response = await api.put<AdminSalariesResponse>(
    `/payroll/admin/salaries/payout-mode/${encodeURIComponent(position)}`,
    { mode },
  );
  return response.data;
}

export async function getDishwasherShiftRate(): Promise<{ rate: number }> {
  const response = await api.get<{ rate: number }>("/payroll/admin/dishwasher/shift-rate");
  return response.data;
}

export async function putDishwasherShiftRate(rate: number): Promise<{ rate: number }> {
  const response = await api.put<{ rate: number }>("/payroll/admin/dishwasher/shift-rate", {
    rate,
  });
  return response.data;
}

export async function getDishwasherEmployees(): Promise<DishwasherEmployee[]> {
  const response = await api.get<DishwasherEmployee[]>("/payroll/admin/dishwasher/employees");
  return response.data;
}

export async function getDishwasherShifts(params: {
  period_start: string;
  period_end: string;
}): Promise<DishwasherShift[]> {
  const response = await api.get<DishwasherShift[]>("/payroll/admin/dishwasher/shifts", {
    params,
  });
  return response.data;
}

export async function putDishwasherShift(payload: {
  employee_id: string;
  work_date: string;
  worked: boolean;
}): Promise<DishwasherShift> {
  const response = await api.put<DishwasherShift>("/payroll/admin/dishwasher/shifts", payload);
  return response.data;
}

export async function getEmployeePayrollReport(params: {
  employee_id: string;
  date_from: string;
  date_to: string;
}): Promise<PayrollPersonalReport> {
  const response = await api.get<PayrollPersonalReport>("/payroll/employee-report", { params });
  return response.data;
}

export async function getPayrollAggregate(params: {
  date_from: string;
  date_to: string;
}): Promise<PayrollAggregate> {
  const response = await api.get<PayrollAggregate>("/payroll/aggregate", { params });
  return response.data;
}

export async function patchPayrollLineDepositOverride(
  id: string,
  payload: PayrollLineDepositOverridePatch,
): Promise<PayrollLine> {
  const response = await api.patch<PayrollLine>(`/payroll/lines/${id}`, payload);
  return response.data;
}

export async function markPayrollPayment(
  runId: string,
  payload: MarkPayrollPaymentPayload,
): Promise<void> {
  await api.post(`/payroll/runs/${runId}/payments`, payload);
}

export async function unmarkPayrollPayment(runId: string, employeeId: string): Promise<void> {
  await api.delete(`/payroll/runs/${runId}/payments/${employeeId}`);
}

export type PartialPayrollPaymentPayload = {
  employee_id: string;
  // Сумма транша; null/опущено = выплатить весь остаток.
  amount?: number | null;
  paid_at: string;
  method?: PayrollPaymentMethod | null;
  comment?: string | null;
  cash_wallet_code: PayrollCashWalletCode;
};

export async function markPartialPayrollPayment(
  runId: string,
  payload: PartialPayrollPaymentPayload,
): Promise<void> {
  await api.post(`/payroll/runs/${runId}/payments/partial`, payload);
}

export type PoolPayoutPayload = {
  selected_ids?: string[] | null;
  boundary_id?: string | null;
  allow_overflow?: boolean;
  paid_at: string;
};

export type PoolPayoutResponse = {
  reserve_id: string;
  primary_booked: number;
  overflow_reserve_id: string | null;
  overflow_booked: number;
  employees_paid: number;
};

// Выплата сотрудникам ведомости из пула-резерва (Сейф/касса) с перетоком на второй пул.
export async function payRunFromPool(
  reserveId: string,
  payload: PoolPayoutPayload,
): Promise<PoolPayoutResponse> {
  const response = await api.post<PoolPayoutResponse>(
    `/payroll/reserves/${reserveId}/payout`,
    payload,
  );
  return response.data;
}

export type PayrollReserveTransferResponse = {
  source_reserve_id: string;
  destination_reserve_id: string;
  transfer_id: string;
  amount: number;
  destination_location: "safe" | "kassa";
  allocations: Array<{ employee_id: string; amount: number }>;
};

// Перенос выбранной части зарплатного резерва между Сейфом и кассой вместе с деньгами.
export async function transferPayrollReserve(
  reserveId: string,
  payload: { selected_ids: string[]; boundary_id?: string | null; operation_date: string },
): Promise<PayrollReserveTransferResponse> {
  const response = await api.post<PayrollReserveTransferResponse>(
    `/payroll/reserves/${reserveId}/transfer`,
    payload,
  );
  return response.data;
}

export type PayrollReserveCancelResponse = {
  reserve_id: string;
  released: number;
  status: string;
};

// Снять непогашенный остаток одного зарплатного резерва без движения денег.
export async function cancelPayrollReserve(
  reserveId: string,
): Promise<PayrollReserveCancelResponse> {
  const response = await api.post<PayrollReserveCancelResponse>(
    `/payroll/reserves/${reserveId}/cancel`,
  );
  return response.data;
}

export type ReserveEmployeePayResponse = {
  booked: number;
  employee_total_paid: number;
  employee_remaining: number;
  reserve_status: string;
  reserve_outstanding: number;
};

// Ручная выплата одному сотруднику из резерва (карандаш → сумма → ✓); остаток лежит резервом.
export async function payEmployeeFromReserve(
  reserveId: string,
  payload: { employee_id: string; amount: number; paid_at: string },
): Promise<ReserveEmployeePayResponse> {
  const response = await api.post<ReserveEmployeePayResponse>(
    `/payroll/reserves/${reserveId}/pay-employee`,
    payload,
  );
  return response.data;
}

export type RunSolvency = {
  available: number;
  required_total: number;
  remaining: number;
  shortfall: number;
  overdraft_limit: number;
  safe_balance: number;
  kassa_balance: number;
  bank_total: number;
  reserved_other: number;
  solvent: boolean;
};

// Платёжеспособность под выплату ведомости (advisory — банк/овердрафт по последней выписке).
export async function getRunSolvency(runId: string): Promise<RunSolvency> {
  const response = await api.get<RunSolvency>(`/payroll/runs/${runId}/solvency`);
  return response.data;
}

export async function markAllPayrollPayments(
  runId: string,
  payload: PayrollPaymentPayload,
): Promise<MarkAllPayrollPaymentsResponse> {
  const response = await api.post<MarkAllPayrollPaymentsResponse>(
    `/payroll/runs/${runId}/payments/mark-all`,
    payload,
  );
  return response.data;
}

export async function bulkMarkPayrollPayments(
  runId: string,
  employeeIds: string[],
  paidAt: string,
  cashWalletCode: PayrollCashWalletCode,
): Promise<MarkAllPayrollPaymentsResponse> {
  const response = await api.post<MarkAllPayrollPaymentsResponse>(
    `/payroll/runs/${runId}/payments/bulk-mark`,
    { employee_ids: employeeIds, paid_at: paidAt, cash_wallet_code: cashWalletCode },
  );
  return response.data;
}

export async function setRunPayoutCash(
  runId: string,
  amountCash: number,
  cashWalletCode?: string | null,
  bankProvider: "tbank" | "sber" = "tbank",
): Promise<PayrollRun> {
  const response = await api.patch<PayrollRun>(`/payroll/runs/${runId}/payout-cash`, {
    amount_cash: amountCash,
    cash_wallet_code: cashWalletCode ?? null,
    bank_provider: bankProvider,
  });
  return response.data;
}

export type PayrollPayoutCashflow = {
  id: string;
  wallet_id: string;
  wallet_code: string;
  wallet_name: string;
  amount: number | string;
  operation_date: string;
  quality_status: string;
  article_code: string | null;
  article_name: string | null;
  purpose: string | null;
};

export type PayrollPayoutWalletCorrection = {
  run_id: string;
  transaction_ids: string[];
  source_wallet_id: string;
  source_wallet_name: string;
  target_wallet_id: string;
  target_wallet_code: string;
  target_wallet_name: string;
  total_amount: number | string;
};

export async function getPayrollPayoutCashflows(runId: string): Promise<PayrollPayoutCashflow[]> {
  const response = await api.get<PayrollPayoutCashflow[]>(
    `/payroll/runs/${runId}/payout-cashflows`,
  );
  return response.data;
}

export async function correctPayrollPayoutWallet(
  runId: string,
  payload: { transaction_ids: string[]; target_wallet_code: string; reason: string },
): Promise<PayrollPayoutWalletCorrection> {
  const response = await api.post<PayrollPayoutWalletCorrection>(
    `/payroll/runs/${runId}/payout-cashflows/correct-wallet`,
    payload,
  );
  return response.data;
}

export async function createPayoutDrafts(runId: string): Promise<PayrollPayoutDraftsResponse> {
  const response = await api.post<PayrollPayoutDraftsResponse>(
    `/payroll/runs/${runId}/payouts/drafts`,
  );
  return response.data;
}

export async function createRunBankDraft(
  runId: string,
  bankProvider: "tbank" | "sber" = "tbank",
): Promise<PayrollBankDraft | null> {
  const response = await api.post<PayrollBankDraft | null>(
    `/payroll/runs/${runId}/bank-draft?bank_provider=${bankProvider}`,
  );
  return response.data;
}

export async function getRunBankDraft(runId: string): Promise<PayrollBankDraft> {
  const response = await api.get<PayrollBankDraft>(`/payroll/runs/${runId}/bank-draft`);
  return response.data;
}

export async function getRunPayoutDelta(runId: string): Promise<RunPayoutDelta> {
  const response = await api.get<RunPayoutDelta>(`/payroll/runs/${runId}/bank-draft/delta`);
  return response.data;
}

export async function applyRunPayoutDelta(
  runId: string,
): Promise<PayrollPayoutApplyDeltasResponse> {
  const response = await api.post<PayrollPayoutApplyDeltasResponse>(
    `/payroll/runs/${runId}/bank-draft/apply-delta`,
  );
  return response.data;
}

export type PayoutBucket = {
  article_code: string;
  article_name: string;
  total: number;
  cash: number;
  bank: number;
};

export type PayrollPayoutAllocation = {
  run_id: string;
  total_payable: number;
  cash_total: number;
  bank_total: number;
  cash_wallet_id: string | null;
  buckets: PayoutBucket[];
};

export type CashWallet = {
  id: string;
  code: string;
  name: string;
};

export type PayrollFundingSource = CashWallet & {
  kind: "cash" | "bank";
  provider: "tbank" | "sber" | null;
  balance: number;
  reserved_other: number;
  available: number;
  reserved_for_run: number;
  payroll_available: number;
  is_configured: boolean;
};

export type PayrollRunFunding = {
  run_id: string;
  cash_sources: PayrollFundingSource[];
  bank_sources: PayrollFundingSource[];
};

export async function getRunPayoutAllocation(runId: string): Promise<PayrollPayoutAllocation> {
  const response = await api.get<PayrollPayoutAllocation>(
    `/payroll/runs/${runId}/payout-allocation`,
  );
  return response.data;
}

export async function getCashWallets(): Promise<CashWallet[]> {
  const response = await api.get<CashWallet[]>(`/payroll/cash-wallets`);
  return response.data;
}

export async function getRunFundingSources(runId: string): Promise<PayrollRunFunding> {
  const response = await api.get<PayrollRunFunding>(`/payroll/runs/${runId}/funding-sources`);
  return response.data;
}

export type EmployeePayoutKind = "owner_salary" | "salary" | "other";

export type EmployeePayoutCreate = {
  employee_id: string;
  amount: number;
  wallet_id: string;
  payout_date: string; // YYYY-MM-DD
  kind?: EmployeePayoutKind;
  article_id?: string | null;
  note?: string | null;
};

export type EmployeePayout = {
  id: string;
  employee_id: string;
  kind: string;
  amount: number;
  payout_date: string;
  wallet_id: string | null;
  article_id: string | null;
  cashflow_transaction_id: string | null;
  status: string;
  note: string | null;
  provider_ref: string | null;
  bank_operation_id: string | null;
  safe_allocation_id: string | null;
  created_at: string;
};

export async function createEmployeePayout(payload: EmployeePayoutCreate): Promise<EmployeePayout> {
  const response = await api.post<EmployeePayout>("/payroll/employee-payouts", payload);
  return response.data;
}

export type OnDemandEmployee = {
  id: string;
  full_name: string;
  position: string | null;
  // Долг «по востребованию»: начислено / выплачено / остаток (может быть < 0 при переплате).
  accrued: number;
  paid: number;
  debt: number;
};

export async function getOnDemandEmployees(): Promise<OnDemandEmployee[]> {
  const response = await api.get<OnDemandEmployee[]>("/payroll/admin/on-demand-employees");
  return response.data;
}

export async function includeOnDemandPayout(
  runId: string,
  payload: { employee_id: string; amount: number; note?: string | null },
): Promise<EmployeePayout> {
  const response = await api.post<EmployeePayout>(
    `/payroll/admin/runs/${runId}/on-demand-include`,
    payload,
  );
  return response.data;
}

export async function getPayoutDeltas(runId: string): Promise<PayrollPayoutDelta[]> {
  const response = await api.get<PayrollPayoutDelta[]>(`/payroll/runs/${runId}/payouts/deltas`);
  return response.data;
}

export async function applyPayoutDeltas(runId: string): Promise<PayrollPayoutApplyDeltasResponse> {
  const response = await api.post<PayrollPayoutApplyDeltasResponse>(
    `/payroll/runs/${runId}/payouts/apply-deltas`,
  );
  return response.data;
}

export async function finalizePayrollRun(id: string): Promise<PayrollRun> {
  const response = await api.post<PayrollRun>(`/payroll/runs/${id}/finalize`);
  return response.data;
}

export async function unfinalizePayrollRun(id: string, reason: string): Promise<PayrollRun> {
  const response = await api.post<PayrollRun>(`/payroll/runs/${id}/unfinalize`, { reason });
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

export type UpcomingPayslip = {
  period_start: string;
  period_end: string;
  payout_date: string;
};

export async function getUpcomingPayslips(
  employeeId?: string,
  count = 2,
): Promise<UpcomingPayslip[]> {
  const response = await api.get<UpcomingPayslip[]>("/payroll/advances/upcoming-payslips", {
    params: { employee_id: employeeId || undefined, count },
  });
  return response.data;
}

export async function getPayrollAdvanceAvailability(
  employeeId: string,
  asOf?: string,
  // applyPayoutGate=false — для реконсиляции уже прошедшей операции: дата операции в
  // прошлом не должна обнулять исторический аванс отсечкой «день выплаты».
  applyPayoutGate = true,
): Promise<PayrollAdvanceAvailability> {
  const response = await api.get<PayrollAdvanceAvailability>("/payroll/advances/availability", {
    params: {
      employee_id: employeeId,
      as_of: asOf || undefined,
      apply_payout_gate: applyPayoutGate ? undefined : false,
    },
  });
  return response.data;
}

export async function getPayrollAdvances(
  employeeId?: string,
  status?: string,
): Promise<PayrollAdvance[]> {
  const response = await api.get<PayrollAdvance[]>("/payroll/advances", {
    params: { employee_id: employeeId || undefined, status: status || undefined },
  });
  return response.data;
}

export type AdvanceIssueWallet = {
  id: string;
  code: string;
  name: string;
  channel: "cash" | "bank";
};

// Счета выдачи аванса/займа: наличные (ТК Черникова/Сейф) + банковский расчётный.
// Банк-выдача создаёт черновик платежа в Т-Банке; ДДС из выписки, изъятие в iiko по «исполнен».
export async function getAdvanceIssueWallets(): Promise<AdvanceIssueWallet[]> {
  const response = await api.get<AdvanceIssueWallet[]>("/payroll/advances/issue-wallets");
  return response.data;
}

export async function createPayrollAdvance(
  payload: PayrollAdvancePayload,
): Promise<PayrollAdvance> {
  const response = await api.post<PayrollAdvance>("/payroll/advances", payload);
  return response.data;
}

export async function markPayrollAdvancePaid(id: string): Promise<PayrollAdvance> {
  const response = await api.post<PayrollAdvance>(`/payroll/advances/${id}/mark-paid`);
  return response.data;
}

export async function cancelPayrollAdvance(id: string): Promise<PayrollAdvance> {
  const response = await api.post<PayrollAdvance>(`/payroll/advances/${id}/cancel`);
  return response.data;
}

/** Отозвать разрешение на выдачу через кассу (пока админ не исполнил; после — 409). */
export async function revokeKassaPayrollAdvance(id: string): Promise<PayrollAdvance> {
  const response = await api.post<PayrollAdvance>(`/payroll/advances/${id}/revoke-kassa`);
  return response.data;
}

export async function writeOffPayrollAdvance(id: string, reason?: string): Promise<PayrollAdvance> {
  const response = await api.post<PayrollAdvance>(`/payroll/advances/${id}/write-off`, { reason });
  return response.data;
}

export async function deferPayrollAdvanceRecovery(
  runId: string,
  advanceId: string,
  defer: boolean,
  reason?: string,
): Promise<PayrollRun> {
  const response = await api.post<PayrollRun>(
    `/payroll/runs/${runId}/advances/${advanceId}/defer-recovery`,
    { defer, reason },
  );
  return response.data;
}

export type RecoveryLine = {
  advance_id: string;
  kind: string;
  issued_on: string;
  amount: number;
  recovered_prior: number;
  outstanding: number;
  default_installment: number;
  current_recovery: number;
  override_amount: number | null;
  max_amount: number;
};

export type EmployeeRecoveryDetail = {
  run_id: string;
  employee_id: string;
  employee_name: string;
  role: string | null;
  period_start: string;
  period_end: string;
  payroll_date: string;
  accrued: number;
  net: number;
  total_recovered: number;
  items: RecoveryLine[];
};

export async function getEmployeeRecoveries(
  runId: string,
  employeeId: string,
): Promise<EmployeeRecoveryDetail> {
  const response = await api.get<EmployeeRecoveryDetail>(
    `/payroll/runs/${runId}/employees/${employeeId}/recoveries`,
  );
  return response.data;
}

export async function setPayrollRecoveryOverrides(
  runId: string,
  items: { advance_id: string; amount: number }[],
  reason?: string,
): Promise<PayrollRun> {
  const response = await api.put<PayrollRun>(`/payroll/runs/${runId}/recoveries`, {
    items,
    reason,
  });
  return response.data;
}

export async function getPayrollAdvanceConfig(): Promise<PayrollAdvanceConfig> {
  const response = await api.get<PayrollAdvanceConfig>("/payroll/advances/config");
  return response.data;
}

export async function putPayrollAdvanceConfig(loanMax: number): Promise<PayrollAdvanceConfig> {
  const response = await api.put<PayrollAdvanceConfig>("/payroll/advances/config", {
    loan_max: loanMax,
  });
  return response.data;
}

export async function createDeferredCharge(
  payload: DeferredChargeCreatePayload,
): Promise<DeferredCharge> {
  const response = await api.post<DeferredCharge>("/payroll/deferred-charges", payload);
  return response.data;
}

export async function getDeferredChargePayoutOptions(): Promise<InventoryPayoutOption[]> {
  const response = await api.get<InventoryPayoutOption[]>(
    "/payroll/deferred-charges/payout-options",
  );
  return response.data;
}

export async function listDeferredCharges(
  filters: {
    status?: DeferredChargeStatus;
    audit_id?: string;
  } = {},
): Promise<DeferredCharge[]> {
  const response = await api.get<DeferredCharge[]>("/payroll/deferred-charges", {
    params: filters,
  });
  return response.data;
}

export async function cancelDeferredCharge(chargeId: string): Promise<DeferredCharge> {
  const response = await api.post<DeferredCharge>(`/payroll/deferred-charges/${chargeId}/cancel`);
  return response.data;
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

// Единый «живой» график: один график с состоянием draft (редактируемый) /
// published (зафиксирован). Создаётся/расширяется на бэке при необходимости.
export async function getLivingSchedule(): Promise<ScheduleRead> {
  const response = await api.get<ScheduleRead>("/schedule/living");
  return response.data;
}

export async function getScheduleLedger(params: {
  date_from: string;
  date_to: string;
}): Promise<ScheduleLedgerEntryRead[]> {
  const response = await api.get<ScheduleLedgerEntryRead[]>("/schedule/ledger", { params });
  return response.data;
}

export async function getVacationRoster(year: number): Promise<VacationRosterRow[]> {
  const response = await api.get<VacationRosterRow[]>("/vacations/roster", { params: { year } });
  return response.data;
}

export async function getVacationPayoutDates(): Promise<string[]> {
  const response = await api.get<string[]>("/vacations/payout-dates");
  return response.data;
}

export async function createVacationPeriod(
  payload: VacationPeriodPayload,
): Promise<VacationPeriodRead> {
  const response = await api.post<VacationPeriodRead>("/vacations", payload);
  return response.data;
}

export async function patchVacationPeriod(
  id: string,
  payload: VacationPeriodPatchPayload,
): Promise<VacationPeriodRead> {
  const response = await api.patch<VacationPeriodRead>(`/vacations/${id}`, payload);
  return response.data;
}

export async function cancelVacationPeriod(id: string): Promise<VacationPeriodRead> {
  const response = await api.post<VacationPeriodRead>(`/vacations/${id}/cancel`);
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

// «Редактировать график»: зафиксированный (published) снова делаем редактируемым (draft).
export async function reopenSchedule(id: string): Promise<ScheduleRead> {
  const response = await api.post<ScheduleRead>(`/schedule/${id}/reopen`);
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

export async function listCashierAllowanceOverrides(
  scheduleId: string,
  params?: { business_date?: string },
): Promise<ShiftAllowanceOverrideRead[]> {
  const response = await api.get<ShiftAllowanceOverrideRead[]>(
    `/schedule/${scheduleId}/cashier-allowance-overrides`,
    { params },
  );
  return response.data;
}

export async function upsertCashierAllowanceOverride(
  scheduleId: string,
  payload: CashierAllowanceOverridePayload,
  overrideId?: string | null,
): Promise<ShiftAllowanceOverrideRead> {
  const path = overrideId
    ? `/schedule/${scheduleId}/cashier-allowance-overrides/${overrideId}`
    : `/schedule/${scheduleId}/cashier-allowance-overrides`;
  const response = overrideId
    ? await api.patch<ShiftAllowanceOverrideRead>(path, payload)
    : await api.post<ShiftAllowanceOverrideRead>(path, payload);
  return response.data;
}

export async function deleteCashierAllowanceOverride(
  scheduleId: string,
  overrideId: string,
): Promise<void> {
  await api.delete(`/schedule/${scheduleId}/cashier-allowance-overrides/${overrideId}`);
}

export async function resolveCashierAllowance(
  scheduleId: string,
  params: { business_date: string },
): Promise<AllowanceAssignmentRead> {
  const response = await api.get<AllowanceAssignmentRead>(
    `/schedule/${scheduleId}/cashier-allowance-resolve`,
    { params },
  );
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

export async function removeForecastOverride(businessDate: string): Promise<RevenueForecastRead> {
  const response = await api.delete<RevenueForecastRead>(
    `/schedule/forecast/${businessDate}/override`,
  );
  return response.data;
}

export async function runCostForecast(scheduleId: string): Promise<PayrollForecastRunRead> {
  const response = await api.post<PayrollForecastRunRead>(`/schedule/${scheduleId}/cost-forecast`);
  return response.data;
}

export async function getLatestRun(scheduleId: string): Promise<PayrollForecastRunRead | null> {
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

export async function getRun(scheduleId: string, runId: string): Promise<PayrollForecastRunRead> {
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

export async function getInventoryPositions(includeInactive = false): Promise<InventoryPosition[]> {
  const response = await api.get<InventoryPosition[]>("/inventory/positions", {
    params: { include_inactive: includeInactive || undefined },
  });
  return response.data;
}

export async function createInventoryPosition(
  payload: InventoryPositionPayload,
): Promise<InventoryPosition> {
  const response = await api.post<InventoryPosition>("/inventory/positions", payload);
  return response.data;
}

export async function patchInventoryPosition(
  id: string,
  payload: InventoryPositionPatch,
): Promise<InventoryPosition> {
  const response = await api.patch<InventoryPosition>(`/inventory/positions/${id}`, payload);
  return response.data;
}

export async function syncInventoryPositionsFromIiko(): Promise<InventoryPositionsSyncResult> {
  const response = await api.post<InventoryPositionsSyncResult>("/inventory/positions/sync-iiko");
  return response.data;
}

export async function getInventoryIikoProducts(params: {
  search?: string;
  refresh?: boolean;
}): Promise<IikoProduct[]> {
  const response = await api.get<IikoProduct[]>("/inventory/iiko-products", { params });
  return response.data;
}

export async function getInventoryAudits(
  filters: {
    dateFrom?: string;
    dateTo?: string;
    status?: InventoryAuditStatus | "all";
  } = {},
): Promise<InventoryAudit[]> {
  const response = await api.get<InventoryAudit[]>("/inventory/audits", {
    params: {
      date_from: filters.dateFrom || undefined,
      date_to: filters.dateTo || undefined,
      status: filters.status && filters.status !== "all" ? filters.status : undefined,
    },
  });
  return response.data;
}

export async function getAllInventoryAuditExclusions(
  filters: {
    audit_date_from?: string;
    audit_date_to?: string;
    employee_id?: string;
  } = {},
): Promise<InventoryAuditAllExclusions> {
  const response = await api.get<InventoryAuditAllExclusions>("/inventory/audit-exclusions", {
    params: filters,
  });
  return response.data;
}

export async function getInventoryAudit(id: string): Promise<InventoryAudit> {
  const response = await api.get<InventoryAudit>(`/inventory/audits/${id}/preview`);
  return response.data;
}

export async function patchInventoryAudit(
  auditId: string,
  payload: { notes?: string | null; penalty_work_date_override?: string | null },
): Promise<InventoryAudit> {
  const response = await api.patch<InventoryAudit>(`/inventory/audits/${auditId}`, payload);
  return response.data;
}

export async function getInventoryAuditPayoutOptions(
  auditId: string,
): Promise<InventoryPayoutOption[]> {
  const response = await api.get<InventoryPayoutOption[]>(
    `/inventory/audits/${auditId}/payout-options`,
  );
  return response.data;
}

export type InventoryDeferredOnPayoutCharge = {
  charge_id: string;
  source_audit_date: string | null;
  source_item_name: string | null;
  allocation_group: InventoryAllocationGroup;
  split_index: number;
  splits_count: number;
  recipient_count: number;
  total_amount: string;
  applied: boolean;
  reason: string;
};

export type InventoryDeferredOnPayoutItem = {
  source_audit_date: string | null;
  source_item_name: string | null;
  split_index: number;
  splits_count: number;
  amount: string;
};

export type InventoryDeferredOnPayoutEmployee = {
  employee_id: string;
  total: string;
  items: InventoryDeferredOnPayoutItem[];
};

export type InventoryDeferredOnPayout = {
  total: string;
  charges: InventoryDeferredOnPayoutCharge[];
  by_employee: InventoryDeferredOnPayoutEmployee[];
};

export async function getInventoryAuditDeferredOnPayout(
  auditId: string,
): Promise<InventoryDeferredOnPayout> {
  const response = await api.get<InventoryDeferredOnPayout>(
    `/inventory/audits/${auditId}/deferred-on-payout`,
  );
  return response.data;
}

export async function getIikoCandidates(businessDate: string): Promise<IikoCandidate[]> {
  const response = await api.get<IikoCandidate[]>("/inventory/audits/iiko-candidates", {
    params: { business_date: businessDate },
  });
  return response.data;
}

export async function importInventoryAuditFromIiko(payload: {
  business_date: string;
  document_id?: string | null;
}): Promise<InventoryAudit> {
  const response = await api.post<InventoryAudit>("/inventory/audits/import-iiko", {
    business_date: payload.business_date,
    document_id: payload.document_id,
  });
  return response.data;
}

export async function createManualInventoryAudit(
  payload: InventoryManualAuditPayload,
): Promise<InventoryAudit> {
  const response = await api.post<InventoryAudit>("/inventory/audits", payload);
  return response.data;
}

export async function addInventoryAuditItem(
  auditId: string,
  payload: InventoryAuditItemPayload,
): Promise<InventoryAudit> {
  const response = await api.post<InventoryAudit>(`/inventory/audits/${auditId}/items`, payload);
  return response.data;
}

export async function patchInventoryAuditItem(
  auditId: string,
  itemId: string,
  payload: Partial<InventoryAuditItemPayload>,
): Promise<InventoryAudit> {
  const response = await api.patch<InventoryAudit>(
    `/inventory/audits/${auditId}/items/${itemId}`,
    payload,
  );
  return response.data;
}

export async function patchInventoryAuditEmployeeExclusion(
  auditId: string,
  employeeId: string,
  payload: { excluded: boolean; reason?: string | null },
): Promise<InventoryAudit> {
  const response = await api.patch<InventoryAudit>(
    `/inventory/audits/${auditId}/employee-exclusions/${employeeId}`,
    payload,
  );
  return response.data;
}

export async function patchInventoryAuditItemExclusion(
  auditId: string,
  itemId: string,
  payload: { excluded: boolean; reason?: string | null },
): Promise<InventoryAudit> {
  const response = await api.patch<InventoryAudit>(
    `/inventory/audits/${auditId}/items/${itemId}/exclusion`,
    payload,
  );
  return response.data;
}

export async function patchInventoryAuditItemAdjustment(
  auditId: string,
  itemId: string,
  payload: { amount: string | null; reason?: string | null },
): Promise<InventoryAudit> {
  const response = await api.patch<InventoryAudit>(
    `/inventory/audits/${auditId}/items/${itemId}/adjustment`,
    payload,
  );
  return response.data;
}

export async function getInventoryAuditCarryoverSuggestions(
  auditId: string,
): Promise<InventoryAuditCarryoverSuggestion[]> {
  const response = await api.get<InventoryAuditCarryoverSuggestion[]>(
    `/inventory/audits/${auditId}/carryover-suggestions`,
  );
  return response.data;
}

export async function deleteInventoryAuditItem(auditId: string, itemId: string): Promise<void> {
  await api.delete(`/inventory/audits/${auditId}/items/${itemId}`);
}

export async function computeInventoryAudit(id: string): Promise<InventoryComputation> {
  const response = await api.post<InventoryComputation>(`/inventory/audits/${id}/compute`);
  return response.data;
}

export async function applyInventoryAudit(id: string): Promise<InventoryAudit> {
  const response = await api.post<InventoryAudit>(`/inventory/audits/${id}/apply`);
  return response.data;
}

export async function cancelInventoryAudit(id: string): Promise<InventoryAudit> {
  const response = await api.post<InventoryAudit>(`/inventory/audits/${id}/cancel`);
  return response.data;
}

export async function restoreInventoryAuditDraft(id: string): Promise<InventoryAudit> {
  const response = await api.post<InventoryAudit>(`/inventory/audits/${id}/restore-draft`);
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

export async function getScheduledPayoutEnabled(): Promise<boolean> {
  const response = await api.get<{ enabled: boolean }>("/deposits/scheduled-payout/settings");
  return response.data.enabled;
}

export async function getDepositPayoutPeriods(): Promise<PayrollPeriod[]> {
  const response = await api.get<PayrollPeriod[]>("/deposits/payout-periods");
  return response.data;
}

export async function scheduleDepositPayout(
  employeeId: string,
  payload: DepositSchedulePayoutPayload,
): Promise<void> {
  await api.post(`/deposits/${employeeId}/schedule-payout`, payload);
}

export async function cancelScheduledDepositPayout(employeeId: string): Promise<void> {
  await api.delete(`/deposits/${employeeId}/schedule-payout`);
}

export async function getCourierDeposits(
  params: {
    status?: CourierDepositStatusFilter;
    category?: CourierDepositCategoryFilter;
  } = {},
): Promise<CourierDepositRow[]> {
  const response = await api.get<CourierDepositRow[]>("/couriers/deposits", { params });
  return response.data;
}

export async function getCourierList(
  params: {
    status?: CourierDepositStatusFilter;
    work_status?: CourierListWorkStatusFilter;
    month?: string;
  } = {},
): Promise<CourierListResponse> {
  const response = await api.get<CourierListResponse>("/couriers/list", {
    params: cleanDdsParams(params),
  });
  return response.data;
}

export async function getCourierStatistics(params: {
  month: string;
}): Promise<CourierStatisticsRow[]> {
  const response = await api.get<CourierStatisticsRow[]>("/couriers/statistics", {
    params: cleanDdsParams(params),
  });
  return response.data;
}

export async function getCourierSchedule(
  from: string,
  to: string,
): Promise<CourierScheduleEntry[]> {
  const response = await api.get<CourierScheduleEntry[]>("/couriers/schedule", {
    params: { from, to },
  });
  return response.data;
}

export async function getCourierScheduleMatched(
  from: string,
  to: string,
): Promise<CourierScheduleMatchedEntry[]> {
  const response = await api.get<CourierScheduleMatchedEntry[]>("/couriers/schedule/matched", {
    params: { from, to },
  });
  return response.data;
}

export async function syncCourierAttendance(
  from: string,
  to: string,
): Promise<CourierAttendanceSyncResult> {
  const response = await api.post<CourierAttendanceSyncResult>(
    "/couriers/iiko/sync-attendance",
    null,
    {
      params: { from, to },
    },
  );
  return response.data;
}

export async function upsertCourierShift(
  employeeId: string,
  workDate: string,
  payload: CourierScheduleUpsertPayload,
): Promise<CourierScheduleEntry> {
  const response = await api.put<CourierScheduleEntry>(
    `/couriers/${employeeId}/schedule/${workDate}`,
    payload,
  );
  return response.data;
}

export async function deleteCourierShift(employeeId: string, workDate: string): Promise<void> {
  await api.delete(`/couriers/${employeeId}/schedule/${workDate}`);
}

export type CourierIikoShiftRow = {
  id: number;
  iiko_employee_id: string;
  employee_id: string | null;
  employee_name: string | null;
  opened_at: string;
  closed_at: string | null;
  worked_minutes: number | null;
  is_open: boolean;
  attendance_type: string;
  match_status: string | null;
  match_category: "primary" | "secondary" | null;
};

export type CourierIikoShiftsResponse = {
  items: CourierIikoShiftRow[];
  limit: number;
  offset: number;
};

export async function getCourierIikoShifts(params: {
  from: string;
  to: string;
  employee_id?: string;
  is_open?: boolean;
  limit?: number;
  offset?: number;
}): Promise<CourierIikoShiftsResponse> {
  const response = await api.get<CourierIikoShiftsResponse>("/couriers/iiko-shifts", {
    params: cleanDdsParams(params),
  });
  return response.data;
}

export type CourierIikoDeliveryRow = {
  id: string;
  iiko_order_id: string;
  order_number: string | null;
  work_date: string;
  status: string | null;
  service_type: string | null;
  courier_iiko_id: string | null;
  courier_employee_name: string | null;
  opened_at: string | null;
  taken_at: string | null;
  delivered_at: string | null;
  way_duration_minutes: string | null;
  revenue: string | null;
};

export type CourierIikoDeliveriesResponse = {
  items: CourierIikoDeliveryRow[];
  summary: {
    count: number;
    avg_minutes: number | null;
    revenue_total: string;
  };
  limit: number;
  offset: number;
};

export async function getCourierIikoDeliveries(params: {
  from: string;
  to: string;
  employee_id?: string;
  service_type?: string;
  include_cancelled?: boolean;
  limit?: number;
  offset?: number;
}): Promise<CourierIikoDeliveriesResponse> {
  const response = await api.get<CourierIikoDeliveriesResponse>("/couriers/iiko-deliveries", {
    params: cleanDdsParams(params),
  });
  return response.data;
}

export async function syncCourierIikoDeliveries(
  params: {
    from?: string;
    to?: string;
  } = {},
): Promise<{ created: number; updated: number; deleted: number }> {
  const response = await api.post<{ created: number; updated: number; deleted: number }>(
    "/couriers/iiko/sync-deliveries",
    null,
    { params: cleanDdsParams(params) },
  );
  return response.data;
}

export type CourierShiftDayShift = {
  opened_at: string;
  closed_at: string | null;
  is_open: boolean;
  worked_minutes: number | null;
};

export type CourierShiftSubstitutionInfo = {
  real_employee_id: string;
  real_full_name: string;
};

export type CourierShiftDayCourier = {
  employee_id: string;
  action_employee_id: string | null;
  full_name: string;
  is_placeholder: boolean;
  substitution: CourierShiftSubstitutionInfo | null;
  category: CourierScheduleCategory | null;
  shifts: CourierShiftDayShift[];
  eval_present: boolean;
  eval_skipped: boolean;
  eval_count: number;
  latest_eval_label: string | null;
  latest_eval_score: number | null;
  deposit_balance_cents: number;
  deposit_target_cents: number;
  deposit_collected: boolean;
  deposit_present: boolean;
  deposit_skipped: boolean;
  deposit_skip_comment: string | null;
  ready: boolean;
};

export type CourierShiftDayUnmatched = {
  iiko_employee_id: string;
  opened_at: string;
  closed_at: string | null;
  is_open: boolean;
};

export type CourierShiftDayResponse = {
  work_date: string;
  status: "draft" | "confirmed";
  confirmed_by: string | null;
  confirmed_by_name: string | null;
  confirmed_at: string | null;
  couriers: CourierShiftDayCourier[];
  unmatched: CourierShiftDayUnmatched[];
  summary: { total: number; ready_count: number; can_confirm: boolean };
};

export type CourierShiftReviewPayload = {
  eval_skipped?: boolean;
  deposit_skipped?: boolean;
  deposit_skip_comment?: string | null;
};

export async function getCourierShiftDay(date: string): Promise<CourierShiftDayResponse> {
  const response = await api.get<CourierShiftDayResponse>("/couriers/shift-day", {
    params: { date },
  });
  return response.data;
}

export async function putCourierShiftReview(
  workDate: string,
  employeeId: string,
  payload: CourierShiftReviewPayload,
): Promise<CourierShiftDayResponse> {
  const response = await api.put<CourierShiftDayResponse>(
    `/couriers/shift-day/${workDate}/courier/${employeeId}/review`,
    payload,
  );
  return response.data;
}

export async function confirmCourierShiftDay(workDate: string): Promise<CourierShiftDayResponse> {
  const response = await api.post<CourierShiftDayResponse>(
    `/couriers/shift-day/${workDate}/confirm`,
    null,
  );
  return response.data;
}

export async function unconfirmCourierShiftDay(workDate: string): Promise<CourierShiftDayResponse> {
  const response = await api.post<CourierShiftDayResponse>(
    `/couriers/shift-day/${workDate}/unconfirm`,
    null,
  );
  return response.data;
}

export async function putCourierSubstitution(
  workDate: string,
  placeholderId: string,
  realEmployeeId: string,
): Promise<CourierShiftDayResponse> {
  const response = await api.put<CourierShiftDayResponse>(
    `/couriers/shift-day/${workDate}/courier/${placeholderId}/substitute`,
    { real_employee_id: realEmployeeId },
  );
  return response.data;
}

export async function deleteCourierSubstitution(
  workDate: string,
  placeholderId: string,
): Promise<CourierShiftDayResponse> {
  const response = await api.delete<CourierShiftDayResponse>(
    `/couriers/shift-day/${workDate}/courier/${placeholderId}/substitute`,
  );
  return response.data;
}

export async function getCourierStatisticsDetail(
  employeeId: string,
  month: string,
): Promise<CourierStatisticsDetail> {
  const response = await api.get<CourierStatisticsDetail>(`/couriers/${employeeId}/statistics`, {
    params: { month },
  });
  return response.data;
}

export async function getCourierDepositSettings(): Promise<CourierDepositSettings> {
  const response = await api.get<CourierDepositSettings>("/couriers/deposits/settings");
  return response.data;
}

export async function putCourierDepositSettings(
  payload: CourierDepositSettingsUpdate,
): Promise<CourierDepositSettings> {
  const response = await api.put<CourierDepositSettings>("/couriers/deposits/settings", payload);
  return response.data;
}

export async function getCourierDepositCard(employeeId: string): Promise<CourierDepositCard> {
  const response = await api.get<CourierDepositCard>(`/couriers/${employeeId}/deposit`);
  return response.data;
}

export async function putCourierDepositOpening(
  employeeId: string,
  payload: CourierDepositOpeningPayload,
): Promise<CourierDepositCard> {
  const response = await api.put<CourierDepositCard>(
    `/couriers/${employeeId}/deposit/opening`,
    payload,
  );
  return response.data;
}

export async function postCourierDepositTransaction(
  employeeId: string,
  payload: CourierDepositTransactionPayload,
): Promise<CourierDepositTransaction> {
  const response = await api.post<CourierDepositTransaction>(
    `/couriers/${employeeId}/deposit/transactions`,
    payload,
  );
  return response.data;
}

// Удаление доступно только для пополнений (top_up) — бэкенд сносит и проводку ДДС, и запись.
// Требует права couriers.deposits.delete. Возвращает обновлённую карточку депозита.
export async function deleteCourierDepositTransaction(
  employeeId: string,
  transactionId: number,
): Promise<CourierDepositCard> {
  const response = await api.delete<CourierDepositCard>(
    `/couriers/${employeeId}/deposit/transactions/${transactionId}`,
  );
  return response.data;
}

export async function getCourierEvaluationCriteria(): Promise<CourierEvaluationCriterion[]> {
  const response = await api.get<CourierEvaluationCriterion[]>("/couriers/evaluation-criteria");
  return response.data;
}

export async function getCourierEvaluations(
  params: CourierEvaluationListParams = {},
): Promise<CourierEvaluation[]> {
  const response = await api.get<CourierEvaluation[]>("/couriers/evaluations", {
    params: cleanDdsParams(params),
  });
  return response.data;
}

export async function createCourierEvaluation(
  payload: CourierEvaluationPayload,
): Promise<CourierEvaluation> {
  const response = await api.post<CourierEvaluation>("/couriers/evaluations", payload);
  return response.data;
}

export async function patchCourierEvaluation(
  id: number,
  payload: CourierEvaluationPatch,
): Promise<CourierEvaluation> {
  const response = await api.patch<CourierEvaluation>(`/couriers/evaluations/${id}`, payload);
  return response.data;
}

export async function deleteCourierEvaluation(id: number): Promise<void> {
  await api.delete(`/couriers/evaluations/${id}`);
}

export async function getCourierEvaluationMonthlyAggregate(
  employeeId: string,
  month: string,
): Promise<CourierEvaluationMonthlyAggregate> {
  const response = await api.get<CourierEvaluationMonthlyAggregate>(
    `/couriers/${employeeId}/evaluations/monthly`,
    { params: { month } },
  );
  return response.data;
}

function cleanDdsParams(params: Record<string, unknown>) {
  return Object.fromEntries(
    Object.entries(params).filter(([, value]) => {
      if (value === undefined || value === null || value === "" || value === "all") {
        return false;
      }
      return true;
    }),
  );
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

export function apiErrorDetail(error: unknown) {
  if (!axios.isAxiosError(error)) {
    return undefined;
  }
  return (error.response?.data as { detail?: unknown } | undefined)?.detail;
}

export function apiErrorStatus(error: unknown) {
  return axios.isAxiosError(error) ? error.response?.status : undefined;
}

// ---------------------------------------------------------------------------
// КДС: очередь упаковки + дашборд скорости кухни
// ---------------------------------------------------------------------------

export type KdsStage = "pending" | "ready" | "waiting_courier" | "handed";
export type KdsTapType = "ready" | "waiting_courier" | "handed";

export interface KdsQueueOrder {
  id: string;
  iiko_order_id: string;
  order_number: string | null;
  status: string | null;
  is_preorder: boolean;
  has_hot_item: boolean;
  complete_before: string | null;
  when_created: string | null;
  ready_at: string | null;
  waiting_courier_at: string | null;
  handed_to_courier_at: string | null;
  stage: KdsStage;
}

export interface KdsQueueResponse {
  server_time: string;
  nudge: { ready_minutes: number; waiting_courier_minutes: number };
  orders: KdsQueueOrder[];
}

export interface KdsTapEvent {
  client_event_id: string;
  iiko_order_id: string;
  event_type: KdsTapType;
  effective_at: string;
  action?: "set" | "rollback" | "edit";
}

export interface KdsEventResult {
  client_event_id: string;
  status: "applied" | "duplicate" | "rejected";
  reason: string | null;
  event_id: string | null;
}

export interface KdsStatBlock {
  median: number | null;
  p90: number | null;
  n: number;
}

export interface KdsDashboardHourRow {
  hour: number;
  segment: "asap" | "preorder";
  count: number;
  queue_cook: KdsStatBlock;
  packing: KdsStatBlock;
  courier_wait: KdsStatBlock;
  road: KdsStatBlock;
}

export interface KdsDashboard {
  date_from: string;
  date_to: string;
  segments: ("asap" | "preorder")[];
  intervals: ("queue_cook" | "packing" | "courier_wait" | "road")[];
  by_hour: KdsDashboardHourRow[];
  peak: { hours: string; count: number } & Record<string, KdsStatBlock | string | number>;
  kpi: {
    median_packing_min: number | null;
    median_courier_wait_min: number | null;
    hot_late_pct: number | null;
    hot_late_n: number;
    rolls_late_pct: number | null;
    rolls_late_n: number;
    orders_total: number;
    orders_with_taps: number;
  };
  preorder_punctuality: {
    ready_on_time_pct: number | null;
    ready_n: number;
    delivered_on_time_pct: number | null;
    delivered_n: number;
  };
}

export async function getKdsQueue(): Promise<KdsQueueResponse> {
  const response = await api.get<KdsQueueResponse>("/kds/queue");
  return response.data;
}

export async function recordKdsEvents(
  events: KdsTapEvent[],
): Promise<{ results: KdsEventResult[] }> {
  const response = await api.post<{ results: KdsEventResult[] }>("/kds/events", { events });
  return response.data;
}

export async function rollbackKdsEvent(eventId: string): Promise<KdsQueueOrder> {
  const response = await api.post<KdsQueueOrder>(
    `/kds/events/${encodeURIComponent(eventId)}/rollback`,
  );
  return response.data;
}

export async function getKdsDashboard(params?: {
  date_from?: string;
  date_to?: string;
}): Promise<KdsDashboard> {
  const response = await api.get<KdsDashboard>("/kds/dashboard", { params });
  return response.data;
}
