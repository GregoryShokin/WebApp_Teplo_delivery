import { useMemo, useState } from "react";
import { Plus, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

import type { SettingWidgetProps } from "./types";
import { isRecord } from "./widget-utils";

type DateArrayValue = {
  dates: string[];
  ranges: Array<{ start: string; end: string }>;
};

export function DateArrayWidget({ value, onChange, disabled, options }: SettingWidgetProps) {
  const [draftDate, setDraftDate] = useState("");
  const monthDay = options?.format === "MM-DD" || options?.fixed_year === false;
  const normalized = useMemo(() => normalizeDateArray(value), [value]);

  function commitDates(dates: string[]) {
    if (isRecord(value)) {
      onChange({ ...value, dates });
      return;
    }
    onChange(dates);
  }

  function addDate() {
    const nextDate = draftDate.trim();
    if (!nextDate || normalized.dates.includes(nextDate)) {
      return;
    }
    commitDates([...normalized.dates, nextDate].sort());
    setDraftDate("");
  }

  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap gap-2">
        {normalized.dates.map((date) => (
          <span
            className="inline-flex h-8 items-center gap-1 rounded-md border bg-background pl-2 pr-1 text-sm"
            key={date}
          >
            {date}
            <Button
              disabled={disabled}
              onClick={() => commitDates(normalized.dates.filter((item) => item !== date))}
              size="icon"
              title="Удалить дату"
              type="button"
              variant="ghost"
              className="h-6 w-6"
            >
              <X aria-hidden="true" className="h-3.5 w-3.5" />
            </Button>
          </span>
        ))}
        {normalized.dates.length === 0 ? (
          <span className="text-sm text-muted-foreground">Даты не заданы</span>
        ) : null}
      </div>

      {normalized.ranges.length > 0 ? (
        <div className="flex flex-wrap gap-2">
          {normalized.ranges.map((range) => (
            <span
              className="rounded-md bg-muted px-2 py-1 text-xs text-muted-foreground"
              key={`${range.start}-${range.end}`}
            >
              {range.start} - {range.end}
            </span>
          ))}
        </div>
      ) : null}

      <div className="flex gap-2">
        <Input
          aria-label="Новая дата"
          disabled={disabled}
          maxLength={monthDay ? 5 : undefined}
          onChange={(event) => setDraftDate(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              addDate();
            }
          }}
          pattern={monthDay ? "\\d{2}-\\d{2}" : undefined}
          placeholder={monthDay ? "MM-DD" : undefined}
          type={monthDay ? "text" : "date"}
          value={draftDate}
        />
        <Button disabled={disabled || !draftDate.trim()} onClick={addDate} type="button" variant="outline">
          <Plus aria-hidden="true" />
          Добавить дату
        </Button>
      </div>
    </div>
  );
}

function normalizeDateArray(value: unknown): DateArrayValue {
  if (Array.isArray(value)) {
    return { dates: value.filter((item): item is string => typeof item === "string"), ranges: [] };
  }
  if (!isRecord(value)) {
    return { dates: [], ranges: [] };
  }

  const dates = Array.isArray(value.dates)
    ? value.dates.filter((item): item is string => typeof item === "string")
    : [];
  const ranges = Array.isArray(value.ranges)
    ? value.ranges.filter(
        (item): item is { start: string; end: string } =>
          isRecord(item) && typeof item.start === "string" && typeof item.end === "string",
      )
    : [];

  return { dates, ranges };
}
