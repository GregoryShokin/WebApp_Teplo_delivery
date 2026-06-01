import { Input } from "@/components/ui/input";

import type { SettingWidgetProps } from "./types";
import { weekdayPremiumConfig } from "./weekday-premium";

export function WeekdayPremiumWidget({
  value,
  onChange,
  disabled,
  unit,
  options,
}: SettingWidgetProps) {
  const config = weekdayPremiumConfig(value, options);

  const updateConfig = (patch: Partial<typeof config>) => {
    onChange({
      amount: config.amount,
      threshold_hours: config.threshold_hours,
      ...patch,
    });
  };

  return (
    <div className="grid gap-3">
      <label className="grid grid-cols-[minmax(12rem,1fr)_minmax(7rem,10rem)] items-center gap-3 text-sm">
        <span className="min-w-0 font-medium">Сумма надбавки за смену в пт/сб, ₽</span>
        <span className="relative">
          <Input
            aria-label="Сумма надбавки за смену в пт/сб, ₽"
            className={unit ? "pr-10 text-right" : "text-right"}
            disabled={disabled}
            min={0}
            onChange={(event) => {
              const nextValue = event.target.value === "" ? 0 : Number(event.target.value);
              updateConfig({
                amount: Number.isFinite(nextValue) ? Math.max(0, nextValue) : 0,
              });
            }}
            step={50}
            type="number"
            value={config.amount}
          />
          {unit ? (
            <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-sm text-muted-foreground">
              {unit}
            </span>
          ) : null}
        </span>
      </label>
      <label className="grid grid-cols-[minmax(12rem,1fr)_minmax(7rem,10rem)] items-center gap-3 text-sm">
        <span className="min-w-0 font-medium">Минимальная длительность для начисления, ч</span>
        <Input
          aria-label="Минимальная длительность для начисления, ч"
          className="text-right"
          disabled={disabled}
          min={0}
          onChange={(event) => {
            const nextValue = event.target.value === "" ? 0 : Number(event.target.value);
            updateConfig({
              threshold_hours: Number.isFinite(nextValue) ? Math.max(0, nextValue) : 0,
            });
          }}
          step={0.25}
          type="number"
          value={config.threshold_hours}
        />
      </label>
    </div>
  );
}
