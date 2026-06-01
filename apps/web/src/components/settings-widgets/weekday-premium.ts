export const WEEKDAY_PREMIUM_DAYS = [
  { key: "monday", label: "Понедельник", shortLabel: "Пн" },
  { key: "tuesday", label: "Вторник", shortLabel: "Вт" },
  { key: "wednesday", label: "Среда", shortLabel: "Ср" },
  { key: "thursday", label: "Четверг", shortLabel: "Чт" },
  { key: "friday", label: "Пятница", shortLabel: "Пт" },
  { key: "saturday", label: "Суббота", shortLabel: "Сб" },
  { key: "sunday", label: "Воскресенье", shortLabel: "Вс" },
] as const;

export type WeekdayPremiumKey = (typeof WEEKDAY_PREMIUM_DAYS)[number]["key"];

export type WeekdayPremiumConfig = {
  amount: number;
  threshold_hours: number;
};

export const DEFAULT_WEEKDAY_PREMIUM_AMOUNT = 200;
export const DEFAULT_WEEKDAY_PREMIUM_THRESHOLD_HOURS = 8;

export function weekdayPremiumAmounts(value: unknown): Record<WeekdayPremiumKey, number> {
  const source = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const config = weekdayPremiumConfig(value);
  const hasNewAmount = hasFiniteNumber(source, "amount");
  return WEEKDAY_PREMIUM_DAYS.reduce(
    (acc, day) => {
      const rawValue = (source as Record<string, unknown>)[day.key];
      if (typeof rawValue === "number" && Number.isFinite(rawValue)) {
        acc[day.key] = rawValue;
      } else if (hasNewAmount && (day.key === "friday" || day.key === "saturday")) {
        acc[day.key] = config.amount;
      } else {
        acc[day.key] = 0;
      }
      return acc;
    },
    {} as Record<WeekdayPremiumKey, number>,
  );
}

export function weekdayPremiumConfig(value: unknown, options?: unknown): WeekdayPremiumConfig {
  const source = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const optionSource = options && typeof options === "object" && !Array.isArray(options) ? options : {};
  const legacyAmounts = weekdayPremiumAmountsFromLegacy(source as Record<string, unknown>);

  return {
    amount:
      finiteNumber((source as Record<string, unknown>).amount) ??
      legacyAmounts ??
      finiteNumber((optionSource as Record<string, unknown>).amount) ??
      DEFAULT_WEEKDAY_PREMIUM_AMOUNT,
    threshold_hours:
      finiteNumber((source as Record<string, unknown>).threshold_hours) ??
      finiteNumber((optionSource as Record<string, unknown>).threshold_hours) ??
      DEFAULT_WEEKDAY_PREMIUM_THRESHOLD_HOURS,
  };
}

function weekdayPremiumAmountsFromLegacy(source: Record<string, unknown>): number | null {
  const hasWeekdayValue = WEEKDAY_PREMIUM_DAYS.some((day) =>
    Object.prototype.hasOwnProperty.call(source, day.key),
  );
  if (!hasWeekdayValue) {
    return null;
  }
  return Math.max(finiteNumber(source.friday) ?? 0, finiteNumber(source.saturday) ?? 0);
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function hasFiniteNumber(source: object, key: string): boolean {
  return finiteNumber((source as Record<string, unknown>)[key]) !== null;
}
