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

export function weekdayPremiumAmounts(value: unknown): Record<WeekdayPremiumKey, number> {
  const source = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  return WEEKDAY_PREMIUM_DAYS.reduce(
    (acc, day) => {
      const rawValue = (source as Record<string, unknown>)[day.key];
      acc[day.key] = typeof rawValue === "number" && Number.isFinite(rawValue) ? rawValue : 0;
      return acc;
    },
    {} as Record<WeekdayPremiumKey, number>,
  );
}
