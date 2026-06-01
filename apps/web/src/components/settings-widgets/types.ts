export type SettingWidgetOption = {
  value: unknown;
  label: string;
};

export type SettingWidgetOptions = {
  options?: SettingWidgetOption[];
  format?: string;
  fixed_year?: boolean;
  min?: number;
  max?: number;
  step?: number;
  value_path?: string;
  amount?: number;
  threshold_hours?: number;
};

export type SettingWidgetProps = {
  value: unknown;
  onChange: (value: unknown) => void;
  disabled?: boolean;
  unit?: string | null;
  options?: SettingWidgetOptions | null;
};
