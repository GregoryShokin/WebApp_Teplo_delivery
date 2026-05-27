import { Input } from "@/components/ui/input";

import type { SettingWidgetProps } from "./types";
import { getPath, numberFromUnknown, setPath } from "./widget-utils";

export function PercentWidget({ value, onChange, disabled, options }: SettingWidgetProps) {
  const ratio = numberFromUnknown(getPath(value, options?.value_path));
  const percent = roundPercent(ratio * 100);

  function commit(nextPercent: number) {
    const nextRatio = Math.max(0, Math.min(100, nextPercent)) / 100;
    onChange(setPath(value, options?.value_path, roundRatio(nextRatio)));
  }

  return (
    <div className="grid gap-2">
      <div className="flex items-center gap-3">
        <input
          aria-label="Процент"
          className="h-2 min-w-0 flex-1 accent-primary disabled:cursor-not-allowed disabled:opacity-50"
          disabled={disabled}
          max={100}
          min={0}
          onChange={(event) => commit(Number(event.target.value))}
          step={0.5}
          type="range"
          value={percent}
        />
        <div className="relative w-24">
          <Input
            aria-label="Процент"
            className="pr-7 text-right"
            disabled={disabled}
            max={100}
            min={0}
            onChange={(event) => commit(Number(event.target.value))}
            step={0.1}
            type="number"
            value={percent}
          />
          <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-sm text-muted-foreground">
            %
          </span>
        </div>
      </div>
    </div>
  );
}

function roundPercent(value: number) {
  return Math.round(value * 10) / 10;
}

function roundRatio(value: number) {
  return Math.round(value * 10_000) / 10_000;
}
