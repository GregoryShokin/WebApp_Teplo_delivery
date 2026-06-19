import { useEffect, useMemo, useState } from "react";

import { getAuthSnapshot, subscribeAuth } from "@/lib/auth";

type AuthSnapshot = ReturnType<typeof getAuthSnapshot>;

export type PermissionCode = string;

export type AppSection =
  | "dashboard"
  | "staff"
  | "couriers"
  | "couriers.shift"
  | "couriers.deposits"
  | "couriers.evaluations"
  | "couriers.statistics"
  | "couriers.schedule"
  | "payroll"
  | "payroll.runs"
  | "payroll.fund"
  | "payroll.adjustments"
  | "payroll.audits"
  | "payroll.accruals"
  | "payroll.personal"
  | "payroll.advances"
  | "payroll.deposits"
  | "schedule"
  | "source-data"
  | "finance"
  | "finance.dds"
  | "finance.payment-calendar"
  | "finance.balance"
  | "counterparties"
  | "kassa"
  | "accounting"
  | "accounting.fixed-assets"
  | "accounting.dz-kz"
  | "settings"
  | "access-control";

export type AppAction =
  | "staff.create"
  | "staff.import"
  | "staff.administration.edit"
  | "couriers.deposits.edit"
  | "couriers.evaluations.edit"
  | "couriers.schedule.edit"
  | "couriers.shifts.sync"
  | "payroll.runs.start"
  | "payroll.runs.recalculate"
  | "payroll.runs.admin.start"
  | "payroll.runs.admin.recalculate"
  | "payroll.runs.finalize"
  | "payroll.runs.reopen"
  | "payroll.runs.mark_paid"
  | "payroll.runs.bank_draft"
  | "payroll.advances.issue"
  | "payroll.loans.issue"
  | "payroll.fund.edit"
  | "payroll.adjustments.edit"
  | "payroll.source-data.edit"
  | "settings.edit"
  | "settings.integrations.configure"
  | "settings.users.create"
  | "settings.users.edit"
  | "settings.access.assign"
  | "settings.roles.edit"
  | "finance.cashflow.edit"
  | "finance.cashflow.classify"
  | "finance.cashflow.sync"
  | "finance.rules.manage"
  | "finance.counterparties.edit"
  | "counterparties.operate"
  | "counterparties.admin"
  | "invoices.normal.create"
  | "invoices.normal.edit"
  | "invoices.normal.pay"
  | "invoices.barter.create"
  | "invoices.barter.edit"
  | "invoices.barter.pay"
  | "kassa.shifts.sync"
  | "kassa.shifts.post"
  | "kassa.cheques.create"
  | "kassa.invoices.create"
  | "kassa.adjustments.create"
  | "kassa.penalty.waive";

const SOURCE_DATA_TAB_READ_PERMISSIONS = [
  "source.rates.read",
  "source.revenue_percent.read",
  "source.deductions.read",
  "source.dismissal_reasons.read",
  "source.allowances.read",
  "source.fund_settings.read",
  "source.deposit_settings.read",
  "source.revision_settings.read",
] as const;

const SOURCE_DATA_TAB_WRITE_PERMISSIONS = [
  "source.rates.edit",
  "source.revenue_percent.edit",
  "source.deductions.edit",
  "source.dismissal_reasons.edit",
  "source.allowances.edit",
  "source.fund_settings.edit",
  "source.deposit_settings.edit",
  "source.revision_settings.edit",
] as const;

export const PERMISSION_GROUPS = {
  staffRead: [
    "staff.administration.read",
    "staff.cooks.read",
    "staff.cashiers.read",
    "staff.auxiliary.read",
    "staff.couriers.read",
  ],
  staffCreate: [
    "staff.administration.create",
    "staff.cooks.create",
    "staff.cashiers.create",
    "staff.couriers.create",
  ],
  staffEdit: [
    "staff.administration.edit",
    "staff.cooks.edit",
    "staff.cashiers.edit",
    "staff.auxiliary.edit",
    "staff.couriers.edit",
  ],
  staffDismiss: [
    "staff.administration.dismiss",
    "staff.cooks.dismiss",
    "staff.cashiers.dismiss",
    "staff.auxiliary.dismiss",
    "staff.couriers.dismiss",
  ],
  staffReinstate: [
    "staff.administration.reinstate",
    "staff.cooks.reinstate",
    "staff.cashiers.reinstate",
    "staff.auxiliary.reinstate",
    "staff.couriers.reinstate",
  ],
  staffHistoryRead: [
    "staff.administration.history.read",
    "staff.cooks.history.read",
    "staff.cashiers.history.read",
    "staff.auxiliary.history.read",
    "staff.couriers.history.read",
  ],
  couriersRead: [
    "couriers.list.read",
    "couriers.statistics.read",
    "couriers.schedule.read",
    "couriers.evaluations.read",
    "couriers.deposits.read",
  ],
  payrollRead: [
    "payroll.runs.read",
    "payroll.accruals.read",
    "payroll.personal_reports.read",
    "revisions.deferrals.read",
    "payroll.vacations.read",
    "payroll.fund.read",
    "payroll.production_deposits.read",
    "payroll.advances.read",
  ],
  payrollWrite: [
    "payroll.runs.start",
    "payroll.runs.recalculate",
    "payroll.runs.finalize",
    "payroll.runs.reopen",
    "payroll.runs.mark_paid",
    "payroll.runs.bank_draft",
    "payroll.adjustments.edit",
    "payroll.bonuses.add",
    "payroll.penalties.add",
    "revisions.deferrals.manage",
    "payroll.vacations.edit",
    "payroll.fund.edit",
    "payroll.production_deposits.edit",
  ],
  sourceDataRead: [
    ...SOURCE_DATA_TAB_READ_PERMISSIONS,
    "source.schedule.read",
    "source.shift_ledger.read",
    "source.revenue.read",
  ],
  sourceDataWrite: [
    ...SOURCE_DATA_TAB_WRITE_PERMISSIONS,
    "source.schedule.edit",
    "source.shift_ledger.input",
    "source.shift_ledger.correct",
    "source.revenue.edit",
    "source.import.run",
    "source.import_errors.review",
  ],
  financeRead: [
    "finance.cashflow.read",
    "finance.balance.read",
    "finance.wallets.read",
    "finance.store_cash.read",
    "finance.payment_calendar.read",
    "finance.counterparties.read",
  ],
  financeWrite: [
    "finance.cashflow.edit",
    "finance.cashflow.classify",
    "finance.classification_rules.manage",
    "finance.cashflow.integrations.manage",
    "finance.balance.edit",
    "finance.wallets.edit",
    "finance.store_cash.enter",
    "finance.payment_calendar.edit",
    "finance.counterparties.edit",
    "finance.owner_review.prepare",
  ],
  accountingRead: [
    "accounting.fixed_assets.read",
    "accounting.suppliers.read",
  ],
  accountingWrite: [
    "accounting.fixed_assets.edit",
    "accounting.suppliers.edit",
    "accounting.periods.close",
  ],
  settingsRead: [
    "settings.general.read",
    "settings.positions.read",
    "settings.users.read",
    "settings.access_audit.read",
    "settings.action_audit.read",
  ],
  settingsWrite: [
    "settings.general.edit",
    "settings.positions.edit",
    "settings.integrations.configure",
    "settings.users.create",
    "settings.users.edit",
    "settings.access.assign",
    "settings.roles.edit",
  ],
} as const;

const SECTION_PERMISSIONS: Record<AppSection, readonly PermissionCode[]> = {
  dashboard: [],
  staff: PERMISSION_GROUPS.staffRead,
  couriers: PERMISSION_GROUPS.couriersRead,
  "couriers.shift": ["couriers.deposits.read", "couriers.evaluations.read"],
  "couriers.deposits": ["couriers.deposits.read"],
  "couriers.evaluations": ["couriers.evaluations.read"],
  "couriers.statistics": ["couriers.statistics.read"],
  "couriers.schedule": ["couriers.schedule.read", "couriers.list.read"],
  payroll: PERMISSION_GROUPS.payrollRead,
  "payroll.runs": ["payroll.runs.read"],
  "payroll.fund": ["payroll.fund.read"],
  "payroll.adjustments": [
    "payroll.adjustments.edit",
    "payroll.bonuses.add",
    "payroll.penalties.add",
  ],
  "payroll.audits": ["revisions.read", "revisions.deferrals.read"],
  "payroll.accruals": ["payroll.accruals.read"],
  "payroll.personal": ["payroll.personal_reports.read"],
  "payroll.advances": ["payroll.advances.read"],
  "payroll.deposits": ["payroll.production_deposits.read"],
  schedule: ["source.schedule.read", "source.shift_ledger.read", "source.schedule.edit"],
  "source-data": [...PERMISSION_GROUPS.sourceDataRead, ...PERMISSION_GROUPS.sourceDataWrite],
  finance: [...PERMISSION_GROUPS.financeRead, ...PERMISSION_GROUPS.financeWrite],
  "finance.dds": [...PERMISSION_GROUPS.financeRead, ...PERMISSION_GROUPS.financeWrite],
  "finance.payment-calendar": ["finance.payment_calendar.read", "finance.payment_calendar.edit"],
  "finance.balance": ["finance.balance.read", "finance.balance.edit"],
  counterparties: ["counterparties.read"],
  kassa: ["kassa.shifts.read", "kassa.cheques.read", "kassa.refs.read", "kassa.invoices.create"],
  accounting: [...PERMISSION_GROUPS.accountingRead, ...PERMISSION_GROUPS.accountingWrite],
  "accounting.fixed-assets": ["accounting.fixed_assets.read", "accounting.fixed_assets.edit"],
  "accounting.dz-kz": ["accounting.suppliers.read", "accounting.suppliers.edit"],
  settings: [...PERMISSION_GROUPS.settingsRead, ...PERMISSION_GROUPS.settingsWrite],
  "access-control": [
    "settings.users.read",
    "settings.roles.edit",
    "settings.access_audit.read",
  ],
};

const ACTION_PERMISSIONS: Record<AppAction, readonly PermissionCode[]> = {
  "staff.create": PERMISSION_GROUPS.staffCreate,
  "staff.import": ["source.import.run"],
  "staff.administration.edit": ["staff.administration.edit"],
  "couriers.deposits.edit": ["couriers.deposits.edit"],
  "couriers.evaluations.edit": ["couriers.evaluations.edit"],
  "couriers.schedule.edit": ["couriers.schedule.edit"],
  "couriers.shifts.sync": ["couriers.shifts.sync"],
  "payroll.runs.start": ["payroll.runs.start"],
  "payroll.runs.recalculate": ["payroll.runs.recalculate"],
  "payroll.runs.admin.start": ["payroll.runs.admin.start"],
  "payroll.runs.admin.recalculate": ["payroll.runs.admin.recalculate"],
  "payroll.runs.finalize": ["payroll.runs.finalize"],
  "payroll.runs.reopen": ["payroll.runs.reopen"],
  "payroll.runs.mark_paid": ["payroll.runs.mark_paid"],
  "payroll.runs.bank_draft": ["payroll.runs.bank_draft"],
  "payroll.advances.issue": [
    "payroll.advances.production.issue",
    "payroll.advances.admin.issue",
    "payroll.loans.issue",
  ],
  "payroll.loans.issue": ["payroll.loans.issue"],
  "payroll.fund.edit": ["payroll.fund.edit"],
  "payroll.adjustments.edit": [
    "payroll.adjustments.edit",
    "payroll.bonuses.add",
    "payroll.penalties.add",
  ],
  "payroll.source-data.edit": SOURCE_DATA_TAB_WRITE_PERMISSIONS,
  "settings.edit": ["settings.general.edit"],
  "settings.integrations.configure": ["settings.integrations.configure"],
  "settings.users.create": ["settings.users.create"],
  "settings.users.edit": ["settings.users.edit"],
  "settings.access.assign": ["settings.access.assign"],
  "settings.roles.edit": ["settings.roles.edit"],
  "finance.cashflow.edit": ["finance.cashflow.edit"],
  "finance.cashflow.classify": ["finance.cashflow.classify", "finance.owner_review.prepare"],
  "finance.cashflow.sync": ["finance.cashflow.integrations.manage"],
  "finance.rules.manage": ["finance.classification_rules.manage"],
  "finance.counterparties.edit": ["finance.counterparties.edit"],
  "counterparties.operate": ["counterparties.operate"],
  "counterparties.admin": ["counterparties.admin"],
  "invoices.normal.create": ["invoices.normal.create"],
  "invoices.normal.edit": ["invoices.normal.edit"],
  "invoices.normal.pay": ["invoices.normal.pay"],
  "invoices.barter.create": ["invoices.barter.create"],
  "invoices.barter.edit": ["invoices.barter.edit"],
  "invoices.barter.pay": ["invoices.barter.pay"],
  "kassa.shifts.sync": ["kassa.shifts.sync"],
  "kassa.shifts.post": ["kassa.shifts.post"],
  "kassa.cheques.create": ["kassa.cheques.create"],
  "kassa.invoices.create": ["kassa.invoices.create"],
  "kassa.adjustments.create": ["kassa.adjustments.create"],
  "kassa.penalty.waive": ["kassa.penalty.waive"],
};

const LEGACY_PERMISSION_ALIASES: Record<PermissionCode, readonly PermissionCode[]> = {
  "dds.read": [
    "finance.cashflow.read",
    "finance.wallets.read",
    "finance.counterparties.read",
    "finance.owner_review.prepare",
  ],
  "dds.classify": ["finance.cashflow.classify"],
  "dds.write": [
    "finance.cashflow.edit",
    "finance.counterparties.edit",
    "finance.owner_review.prepare",
    "finance.cashflow.integrations.manage",
  ],
  "dds.rules.write": ["finance.classification_rules.manage"],
  "payroll.read": [
    "payroll.runs.read",
    "payroll.accruals.read",
    "payroll.personal_reports.read",
    "revisions.deferrals.read",
    "payroll.production_deposits.read",
    ...SOURCE_DATA_TAB_READ_PERMISSIONS,
  ],
  "payroll.source_data.read": SOURCE_DATA_TAB_READ_PERMISSIONS,
  "payroll.source_data.edit": SOURCE_DATA_TAB_WRITE_PERMISSIONS,
  "payroll.rules.configure": SOURCE_DATA_TAB_WRITE_PERMISSIONS,
  "payroll.revisions.read": ["revisions.deferrals.read"],
  "payroll.revision_penalties.add": ["revisions.deferrals.manage"],
  "payroll.runs.write": ["payroll.runs.start", "payroll.runs.recalculate"],
  "payroll.runs.finalize": ["payroll.runs.finalize", "payroll.runs.mark_paid"],
  "payroll.adjustments.write": [
    "payroll.adjustments.edit",
    "payroll.bonuses.add",
    "payroll.penalties.add",
    "source.deductions.edit",
  ],
  "schedule.read": ["source.schedule.read"],
  "schedule.write": ["source.schedule.edit"],
  "shifts.read": ["source.shift_ledger.read"],
  "shifts.write": ["source.shift_ledger.input", "source.shift_ledger.correct"],
  "inventory.read": ["revisions.read"],
  "inventory.write": [
    "revisions.create",
    "revisions.drafts.edit",
    "revisions.import",
  ],
  "inventory.audits.write": [
    "revisions.create",
    "revisions.drafts.edit",
    "revisions.import",
    "revisions.items.exclude",
    "revisions.employees.exclude",
    "revisions.finalize",
  ],
  "staff.read": ["staff.administration.read", "staff.administration.history.read"],
  "staff.write": ["staff.administration.edit"],
  "staff.dismiss": ["staff.administration.dismiss"],
  "staff.reinstate": ["staff.administration.reinstate"],
  "couriers.read": [
    "couriers.list.read",
    "couriers.statistics.read",
    "couriers.schedule.read",
    "couriers.evaluations.read",
    "couriers.deposits.read",
  ],
  "couriers.metrics.read": ["couriers.statistics.read"],
  "couriers.schedule.write": ["couriers.schedule.edit", "couriers.shifts.sync"],
  "couriers.assess.write": ["couriers.evaluations.edit"],
  "couriers.deposits.withhold": ["couriers.deposits.edit"],
  "couriers.deposits.return": ["couriers.deposits.edit"],
  "couriers.deposits.write": ["couriers.deposits.edit", "couriers.deposits.configure"],
  "deposits.read": ["payroll.production_deposits.read"],
  "deposits.write": ["payroll.production_deposits.edit"],
  "vacations.read": ["payroll.vacations.read"],
  "vacations.write": ["payroll.vacations.edit"],
  "accumulation_fund.read": ["payroll.fund.read"],
  "accumulation_fund.write": ["payroll.fund.edit"],
  "settings.read": ["settings.general.read"],
  "settings.write": ["settings.general.edit"],
  "access_control.users.write": [
    "settings.users.read",
    "settings.users.create",
    "settings.users.edit",
    "settings.access.assign",
  ],
  "access_control.roles.write": ["settings.roles.edit", "settings.access_audit.read"],
};

const FULL_ACCESS_ROLES = new Set(["owner", "admin"]);

export function useAuthSnapshot() {
  const [snapshot, setSnapshot] = useState(getAuthSnapshot);

  useEffect(() => {
    const unsubscribe = subscribeAuth(setSnapshot);
    return () => {
      unsubscribe();
    };
  }, []);

  return snapshot;
}

export function usePermissions() {
  const snapshot = useAuthSnapshot();

  return useMemo(
    () => ({
      snapshot,
      hasPermission: (permission: PermissionCode) => hasPermission(permission, snapshot),
      hasAnyPermission: (permissions: readonly PermissionCode[]) =>
        hasAnyPermission(permissions, snapshot),
      canOpenSection: (section: AppSection) => canOpenSection(section, snapshot),
      canPerformAction: (action: AppAction) => canPerformAction(action, snapshot),
    }),
    [snapshot],
  );
}

export function hasPermission(permission: PermissionCode, snapshot: AuthSnapshot = getAuthSnapshot()) {
  if (!snapshot.user) {
    return false;
  }
  const expanded = expandPermissions(snapshot.permissions);
  if (expanded.has(permission)) {
    return true;
  }
  return snapshot.user.roles.some((role) => FULL_ACCESS_ROLES.has(role));
}

export function hasAnyPermission(
  permissions: readonly PermissionCode[],
  snapshot: AuthSnapshot = getAuthSnapshot(),
) {
  if (permissions.length === 0) {
    return Boolean(snapshot.user);
  }
  return permissions.some((permission) => hasPermission(permission, snapshot));
}

export function canOpenSection(section: AppSection, snapshot: AuthSnapshot = getAuthSnapshot()) {
  return hasAnyPermission(SECTION_PERMISSIONS[section], snapshot);
}

export function canPerformAction(action: AppAction, snapshot: AuthSnapshot = getAuthSnapshot()) {
  return hasAnyPermission(ACTION_PERMISSIONS[action], snapshot);
}

function expandPermissions(permissions: readonly PermissionCode[]) {
  const expanded = new Set(permissions);
  Object.entries(LEGACY_PERMISSION_ALIASES).forEach(([legacyCode, replacementCodes]) => {
    if (expanded.has(legacyCode)) {
      replacementCodes.forEach((code) => expanded.add(code));
    }
    if (replacementCodes.some((code) => expanded.has(code))) {
      expanded.add(legacyCode);
    }
  });
  return expanded;
}
