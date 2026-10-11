import { expect, test } from "@playwright/test";
import { calendarServicePeriod } from "../src/routes/payment-page/service-period";

test("шесть месяцев с октября заканчиваются 31 марта следующего года", () => {
  expect(calendarServicePeriod("2026-10", 6)).toEqual({ start: "2026-10-01", end: "2027-03-31" });
});

test("календарный период учитывает високосный февраль", () => {
  expect(calendarServicePeriod("2028-01", 2)).toEqual({ start: "2028-01-01", end: "2028-02-29" });
  expect(calendarServicePeriod("2027-02", 1)).toEqual({ start: "2027-02-01", end: "2027-02-28" });
});

test("неполный месяц и некорректная длительность не превращаются в период", () => {
  for (const [month, duration] of [
    ["", 6],
    ["2026-13", 6],
    ["2026-10", 0],
    ["2026-10", 1.5],
    ["2026-10", 121],
  ] as const) {
    expect(calendarServicePeriod(month, duration)).toBeNull();
  }
});
