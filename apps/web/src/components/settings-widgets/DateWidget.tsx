import { Input } from "@/components/ui/input";

import type { SettingWidgetProps } from "./types";

export function DateWidget({ value, onChange, disabled, options }: SettingWidgetProps) {
  const monthDay = options?.format === "MM-DD" || options?.fixed_year === false;
  const stringValue = typeof value === "string" ? value : "";

  return (
    <Input
      aria-label="Дата"
      disabled={disabled}
      maxLength={monthDay ? 5 : undefined}
      onChange={(event) => onChange(event.target.value)}
      pattern={monthDay ? "\\d{2}-\\d{2}" : undefined}
      placeholder={monthDay ? "MM-DD" : undefined}
      type={monthDay ? "text" : "date"}
      value={stringValue}
    />
  );
}
