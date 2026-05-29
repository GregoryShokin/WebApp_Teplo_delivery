import { Input } from "@/components/ui/input";

import type { SettingWidgetProps } from "./types";
import { WEEKDAY_PREMIUM_DAYS, weekdayPremiumAmounts } from "./weekday-premium";

export function WeekdayPremiumWidget({
  value,
  onChange,
  disabled,
  unit,
}: SettingWidgetProps) {
  const amounts = weekdayPremiumAmounts(value);

  return (
    <div className="grid gap-2">
      {WEEKDAY_PREMIUM_DAYS.map((day) => (
        <label
          className="grid grid-cols-[minmax(7rem,1fr)_minmax(6rem,10rem)] items-center gap-3 text-sm"
          key={day.key}
        >
          <span className="min-w-0 font-medium">{day.label}</span>
          <span className="relative">
            <Input
              aria-label={day.label}
              className={unit ? "pr-10 text-right" : "text-right"}
              disabled={disabled}
              min={0}
              onChange={(event) => {
                const nextValue = event.target.value === "" ? 0 : Number(event.target.value);
                onChange({
                  ...amounts,
                  [day.key]: Number.isFinite(nextValue) ? Math.max(0, nextValue) : 0,
                });
              }}
              step={50}
              type="number"
              value={amounts[day.key]}
            />
            {unit ? (
              <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-sm text-muted-foreground">
                {unit}
              </span>
            ) : null}
          </span>
        </label>
      ))}
    </div>
  );
}
