import { toIsoDate } from "./date";

export type PeriodPreset = "week" | "2weeks" | "month" | "custom";

export interface PeriodRange {
  from: string;
  to: string;
}

export function nextMonday(today: Date): Date {
  const dow = today.getDay();
  let daysUntilMonday: number;

  if (dow === 1) {
    daysUntilMonday = 7;
  } else if (dow === 0) {
    daysUntilMonday = 1;
  } else {
    daysUntilMonday = (8 - dow) % 7;
  }

  return addDays(today, daysUntilMonday);
}

export function rangeForPreset(preset: PeriodPreset, today: Date): PeriodRange | null {
  switch (preset) {
    case "week": {
      const monday = nextMonday(today);
      return { from: toIsoDate(monday), to: toIsoDate(addDays(monday, 6)) };
    }
    case "2weeks": {
      const monday = nextMonday(today);
      return { from: toIsoDate(monday), to: toIsoDate(addDays(monday, 13)) };
    }
    case "month":
      return { from: toIsoDate(startOfMonth(today)), to: toIsoDate(endOfMonth(today)) };
    case "custom":
      return null;
  }
}

export function shiftRange(
  preset: PeriodPreset,
  current: PeriodRange,
  direction: -1 | 1,
): PeriodRange {
  const from = parseIsoDate(current.from);
  const to = parseIsoDate(current.to);

  switch (preset) {
    case "week":
      return {
        from: toIsoDate(addDays(from, 7 * direction)),
        to: toIsoDate(addDays(to, 7 * direction)),
      };
    case "2weeks":
      return {
        from: toIsoDate(addDays(from, 14 * direction)),
        to: toIsoDate(addDays(to, 14 * direction)),
      };
    case "month": {
      const newFrom = new Date(from.getFullYear(), from.getMonth() + direction, 1);
      const newTo = endOfMonth(newFrom);
      return { from: toIsoDate(newFrom), to: toIsoDate(newTo) };
    }
    case "custom":
      return current;
  }
}

function addDays(value: Date, days: number) {
  const date = new Date(value);
  date.setDate(date.getDate() + days);
  return date;
}

function startOfMonth(value: Date) {
  return new Date(value.getFullYear(), value.getMonth(), 1);
}

function endOfMonth(value: Date) {
  return new Date(value.getFullYear(), value.getMonth() + 1, 0);
}

function parseIsoDate(value: string) {
  const [year, month, day] = value.split("-").map(Number);
  return new Date(year, month - 1, day);
}
