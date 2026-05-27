import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

import type { SettingWidgetProps } from "./types";
import { stableOptionValue } from "./widget-utils";

export function SelectWidget({ value, onChange, disabled, options }: SettingWidgetProps) {
  const selectOptions = Array.isArray(options?.options) ? options.options : [];
  const selected = stableOptionValue(value);

  function handleChange(nextValue: string) {
    const option = selectOptions.find((item) => stableOptionValue(item.value) === nextValue);
    if (option) {
      onChange(option.value);
    }
  }

  return (
    <Select disabled={disabled} onValueChange={handleChange} value={selected}>
      <SelectTrigger aria-label="Выбор значения">
        <SelectValue placeholder="Выберите значение" />
      </SelectTrigger>
      <SelectContent>
        {selectOptions.map((option) => (
          <SelectItem key={stableOptionValue(option.value)} value={stableOptionValue(option.value)}>
            {option.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
