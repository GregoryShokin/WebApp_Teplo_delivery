import type { AppSetting, DepositListItem, EmployeeCategory } from "@/lib/api";

export type DepositRule = {
  coeff?: unknown;
  deposit_target?: unknown;
  deposit_withholding?: unknown;
  [key: string]: unknown;
};

export type DepositRulesByKey = Record<string, DepositRule>;

export const depositCategoryOrder: EmployeeCategory[] = [
  "category_1",
  "category_2",
  "category_3",
  "category_4",
  "intern",
];

export const depositRuleKeyByCategory: Record<EmployeeCategory, string> = {
  category_1: "1",
  category_2: "2",
  category_3: "3",
  category_4: "4",
  intern: "4",
  freelancer: "6",
};

export function categoryRuleKey(category: EmployeeCategory | null | undefined) {
  return category ? depositRuleKeyByCategory[category] : null;
}

export function extractDepositSettings(settings: AppSetting[] | undefined) {
  const rows = settings ?? [];
  return {
    autoEnabled:
      rows.find((setting) => setting.key === "payroll.deposit_auto_withholding_enabled")?.value ===
      true,
    rules: parseDepositRules(
      rows.find((setting) => setting.key === "payroll.category_rules")?.value,
    ),
  };
}

export function parseDepositRules(value: unknown): DepositRulesByKey {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return {};
  }
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>).map(([key, rule]) => [
      key,
      rule && typeof rule === "object" && !Array.isArray(rule) ? (rule as DepositRule) : {},
    ]),
  );
}

export function depositRuleForCategory(
  rules: DepositRulesByKey,
  category: EmployeeCategory | null | undefined,
) {
  const key = categoryRuleKey(category);
  return key ? (rules[key] ?? null) : null;
}

export function depositRuleValue(
  rules: DepositRulesByKey,
  category: EmployeeCategory | null | undefined,
  field: "deposit_target" | "deposit_withholding",
) {
  const value = depositRuleForCategory(rules, category)?.[field];
  if (value === null || value === undefined || value === "") {
    return null;
  }
  return String(value);
}

export function formatMoney(value: string | number | null | undefined, emptyValue = "—") {
  const amount = Number(value);
  if (!Number.isFinite(amount)) {
    return emptyValue;
  }
  return new Intl.NumberFormat("ru-RU", {
    currency: "RUB",
    maximumFractionDigits: 0,
    style: "currency",
  }).format(amount);
}

/**
 * Деньги с копейками — но только когда они есть.
 *
 * `formatMoney` округляет до рубля, и остаток депозита 719,91 ₽ выглядит как «720 ₽».
 * Пользователь вводит увиденное число в списание и получает отказ «сумма больше
 * текущего баланса (720 ₽)» — сообщение противоречит вводу. Поэтому там, где по числу
 * принимают решение (остаток в таблице, диалоги операций, суммы транзакций), показываем
 * точное значение; целые суммы при этом остаются без хвоста «,00».
 */
export function formatMoneyPrecise(value: string | number | null | undefined, emptyValue = "—") {
  const amount = Number(value);
  if (!Number.isFinite(amount)) {
    return emptyValue;
  }
  const fractionDigits = Math.abs(amount - Math.round(amount)) > 1e-9 ? 2 : 0;
  return new Intl.NumberFormat("ru-RU", {
    currency: "RUB",
    maximumFractionDigits: fractionDigits,
    minimumFractionDigits: fractionDigits,
    style: "currency",
  }).format(amount);
}

export function formatPercentValue(value: string | number | null | undefined) {
  const amount = Number(value);
  if (!Number.isFinite(amount)) {
    return "0%";
  }
  return new Intl.NumberFormat("ru-RU", {
    maximumFractionDigits: 1,
  }).format(amount);
}

export function progressValue(value: string | number | null | undefined) {
  const amount = Number(value);
  if (!Number.isFinite(amount)) {
    return 0;
  }
  return Math.min(Math.max(amount, 0), 100);
}

export function formatDate(value: string | null | undefined) {
  if (!value) {
    return "бессрочно";
  }
  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "short",
    timeZone: "Europe/Moscow",
  }).format(new Date(`${value}T00:00:00+03:00`));
}

export function formatDateTime(value: string | null | undefined) {
  if (!value) {
    return "Не указана";
  }
  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "short",
    timeStyle: "short",
    timeZone: "Europe/Moscow",
  }).format(new Date(value));
}

export function transactionTypeLabel(type: string) {
  if (type === "payout") {
    return "Выплата";
  }
  if (type === "write_off" || type === "writeoff") {
    return "Списание";
  }
  return "Накопление";
}

export function isDepositTargetPosition(position: string | null | undefined) {
  return position === "Повар" || position === "Кассир";
}

export function depositSourceLabel(
  deposit: DepositListItem | null | undefined,
  rules: DepositRulesByKey,
) {
  if (!deposit?.category) {
    return "Категория не задана";
  }
  const targetDefault = depositRuleValue(rules, deposit.category, "deposit_target");
  const withholdingDefault = depositRuleValue(rules, deposit.category, "deposit_withholding");
  const hasTargetOverride = decimalValuesDiffer(deposit.target, targetDefault);
  const hasWithholdingOverride = decimalValuesDiffer(deposit.withholding, withholdingDefault);
  return hasTargetOverride || hasWithholdingOverride ? "Индивидуально" : "Категория-default";
}

export function inferredOverrideValue(
  effectiveValue: string | null | undefined,
  defaultValue: string | null | undefined,
  explicitOverride?: string | null,
) {
  if (explicitOverride !== undefined) {
    return explicitOverride ?? "";
  }
  return decimalValuesDiffer(effectiveValue, defaultValue) ? (effectiveValue ?? "") : "";
}

export function normalizeDecimalInput(value: string) {
  return value.trim().replace(",", ".");
}

export function validNonNegativeDecimalInput(value: string) {
  const normalized = normalizeDecimalInput(value);
  if (!normalized) {
    return true;
  }
  const amount = Number(normalized);
  return Number.isFinite(amount) && amount >= 0;
}

function decimalValuesDiffer(left: string | null | undefined, right: string | null | undefined) {
  if (
    (left === null || left === undefined || left === "") &&
    (right === null || right === undefined || right === "")
  ) {
    return false;
  }
  const leftNumber = Number(left);
  const rightNumber = Number(right);
  if (Number.isFinite(leftNumber) && Number.isFinite(rightNumber)) {
    return leftNumber !== rightNumber;
  }
  return String(left ?? "") !== String(right ?? "");
}
