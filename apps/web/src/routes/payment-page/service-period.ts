/** Октябрь + 6 календарных месяцев → 1 октября — 31 марта. Независимо от часового пояса. */
export function calendarServicePeriod(
  month: string,
  duration: number,
): { start: string; end: string } | null {
  if (!/^\d{4}-\d{2}$/.test(month) || !Number.isInteger(duration) || duration < 1 || duration > 120)
    return null;
  const [year, startMonth] = month.split("-").map(Number);
  if (year < 2015 || year > 2100 || startMonth < 1 || startMonth > 12) return null;
  const end = new Date(Date.UTC(year, startMonth - 1 + duration, 0));
  return { start: `${month}-01`, end: end.toISOString().slice(0, 10) };
}
