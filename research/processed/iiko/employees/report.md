# iiko employees research report

Generated: 2026-05-20T15:10:48. Period end: 2026-05-20.

## Main finding

`/employees/attendance` is the working iiko journal of employee attendance. The previous zero-hour result was caused by parser coverage: raw XML contains `<attendance>` items, while the old parser counted only row-like tags.

## Active contour counts

| Month | Attendance records | Fact hours | Employees |
| --- | ---: | ---: | ---: |
| 2026-02 | 335 | 3725.7 | 37 |
| 2026-03 | 330 | 3846.9 | 34 |
| 2026-04 | 287 | 3402.6 | 31 |
| 2026-05 | 203 | 2340.2 | 25 |

## Data quality

- Raw attendance rows parsed: 2194.
- Active Черникова rows after period filtering: 2187.
- Rows skipped because `dateFrom` was outside the requested month: 7.
- Open rows without `dateTo`: 9.
- Schedule rows parsed: 0; empty schedule is marked as `schedule_endpoint_returned_zero`, not as confirmed zero plan.

## Endpoint notes

- `/v2/reports/olap/presets`: status=200, records=40, note=filter staff/revenue presets.
- `/v2/reports/olap/columns`: status=404, records=0, note=error: reportType=LABOUR.
- `/v2/reports/olap/columns`: status=404, records=0, note=error: reportType=ATTENDANCE.
- `/employees/salary`: status=200, records=39, note=current salary/payment settings.
- `/employees/attendance`: status=200, records=69, note=pilot no department.
- `/employees/attendance`: status=400, records=0, note=error: pilot alternate date format.
- `/employees/schedule`: status=200, records=0, note=pilot no department.
- `/employees/schedule`: status=400, records=0, note=error: pilot alternate date format.
- `/v2/payrolls/list`: status=200, records=0, note=payroll list pilot by active department.
- `/employees/attendance`: status=200, records=333, note=target monthly attendance export.
- `/employees/schedule`: status=200, records=0, note=target monthly schedule check.
- `/employees/attendance`: status=200, records=354, note=target monthly attendance export.
- `/employees/schedule`: status=200, records=0, note=target monthly schedule check.
- `/employees/attendance`: status=200, records=348, note=target monthly attendance export.
- `/employees/schedule`: status=200, records=0, note=target monthly schedule check.
- `/employees/attendance`: status=200, records=336, note=target monthly attendance export.
- `/employees/schedule`: status=200, records=0, note=target monthly schedule check.
- `/employees/attendance`: status=200, records=331, note=target monthly attendance export.
- `/employees/schedule`: status=200, records=0, note=target monthly schedule check.
- `/employees/attendance`: status=200, records=288, note=target monthly attendance export.
- `/employees/schedule`: status=200, records=0, note=target monthly schedule check.
- `/employees/attendance`: status=200, records=204, note=target monthly attendance export.
- `/employees/schedule`: status=200, records=0, note=target monthly schedule check.
- `/v2/reports/olap/byPresetId/{id}`: status=200, records=5, note=pilot saved preset `Выручка по официантам`; raw may contain employee names.

## Staff/revenue related OLAP presets

- `8c13763a-35bf-9f27-017f-5468b1e7001b`: Выручка по дням (SALES).
- `8c13763a-35bf-9f27-017f-5468b1e7001a`: Выручка по категориям блюд (SALES).
- `8c13763a-35bf-9f27-017f-5468b1e70019`: Почасовая выручка (SALES).
- `8c13763a-35bf-9f27-017f-5468b1e7001d`: Выручка станций по дням (SALES).
- `8c13763a-35bf-9f27-017f-5468b1e7001f`: Выручка за блюда по кассам (SALES).
- `8c13763a-35bf-9f27-017f-5468b1e7001e`: Выручка по официантам (SALES).
- `73a25778-dafb-4065-a820-1e9c7da6fed6`: Отчет о выручки по направлениям (SALES).

## Files

- `attendance_monthly.csv`: anonymized employee-month-role aggregates.
- `attendance_field_schema.csv`: redacted field catalogue.
- `endpoints_inventory.csv`: tried endpoint inventory.

## Revenue by employee

The SALES preset `Выручка по официантам` returned 5 rows for 2026-04-01..2026-04-07 with fields `WaiterName`, `DishDiscountSumInt`, `DishSumInt`. This can support revenue-by-employee checks only through a protected employee-name lookup; it is not a shift-level revenue journal by itself.
