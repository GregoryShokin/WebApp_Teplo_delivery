import { Input } from "@/components/ui/input";

import type { SettingWidgetProps } from "./types";

export function TimeWidget({ value, onChange, disabled }: SettingWidgetProps) {
  return (
    <Input
      aria-label="Время"
      disabled={disabled}
      onChange={(event) => onChange(event.target.value)}
      type="time"
      value={typeof value === "string" ? value : ""}
    />
  );
}
