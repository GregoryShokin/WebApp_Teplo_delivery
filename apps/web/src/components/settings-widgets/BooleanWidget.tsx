import type { SettingWidgetProps } from "./types";

export function BooleanWidget({ value, onChange, disabled }: SettingWidgetProps) {
  const checked = value === true;

  return (
    <button
      aria-checked={checked}
      aria-label="Переключатель"
      className="inline-flex h-9 w-16 items-center rounded-full border bg-muted p-1 transition-colors data-[state=checked]:bg-primary disabled:cursor-not-allowed disabled:opacity-50"
      data-state={checked ? "checked" : "unchecked"}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      role="switch"
      type="button"
    >
      <span
        className="h-7 w-7 rounded-full bg-background shadow-sm transition-transform data-[state=checked]:translate-x-7"
        data-state={checked ? "checked" : "unchecked"}
      />
    </button>
  );
}
