import { Input } from "@/components/ui/input";

import type { SettingWidgetProps } from "./types";
import { numberFromUnknown } from "./widget-utils";

export function NumberWidget({ value, onChange, disabled, unit, options }: SettingWidgetProps) {
  return (
    <div className="relative">
      <Input
        aria-label="Число"
        className={unit ? "pr-16 text-right" : "text-right"}
        disabled={disabled}
        max={options?.max}
        min={options?.min}
        onChange={(event) => {
          const nextValue = event.target.value;
          onChange(nextValue === "" ? "" : Number(nextValue));
        }}
        step={options?.step ?? 1}
        type="number"
        value={value === "" ? "" : numberFromUnknown(value)}
      />
      {unit ? (
        <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-sm text-muted-foreground">
          {unit}
        </span>
      ) : null}
    </div>
  );
}
