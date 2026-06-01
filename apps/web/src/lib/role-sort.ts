export const PAYROLL_ROLE_SORT_ORDER: Record<string, number> = {
  Сушист: 1,
  Пиццерист: 2,
  Шаурмист: 3,
  Заготовщик: 4,
  Касса: 5,
  Администратор: 5,
};

const FALLBACK_RANK = 999;

export function payrollRoleRank(payrollRole: string | null | undefined): number {
  if (!payrollRole) {
    return FALLBACK_RANK;
  }
  return PAYROLL_ROLE_SORT_ORDER[payrollRole] ?? FALLBACK_RANK;
}

export function sortEmployeesByRoleAndName<
  T extends {
    full_name: string;
    available_roles?: Array<{ payroll_role: string; is_primary: boolean }>;
  },
>(employees: T[]): T[] {
  return [...employees].sort((a, b) => {
    const aPrimary = a.available_roles?.find((role) => role.is_primary)?.payroll_role ?? null;
    const bPrimary = b.available_roles?.find((role) => role.is_primary)?.payroll_role ?? null;
    const rankDiff = payrollRoleRank(aPrimary) - payrollRoleRank(bPrimary);
    if (rankDiff !== 0) {
      return rankDiff;
    }
    return a.full_name.localeCompare(b.full_name, "ru");
  });
}
