import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye, LoaderCircle, Lock, Plus, RefreshCw, Save, Trash2 } from "lucide-react";
import { toast } from "sonner";

import {
  BooleanWidget,
  DateArrayWidget,
  DateWidget,
  JsonWidget,
  NumberWidget,
  PercentWidget,
  SelectWidget,
  TimeWidget,
  WEEKDAY_PREMIUM_DAYS,
  WeekdayPremiumWidget,
  findSelectLabel,
  type SettingWidgetOptions,
  weekdayPremiumConfig,
  weekdayPremiumAmounts,
} from "@/components/settings-widgets";
import { formatJson, getPath } from "@/components/settings-widgets/widget-utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { PageHeader } from "@/components/ui-app/PageHeader";
import { getAuthSnapshot, subscribeAuth } from "@/lib/auth";
import {
  getSettingHistory,
  getSettings,
  getSubstitutePairs,
  updateSetting,
  updateSubstitutePairs,
  apiErrorMessage,
  type AppSetting,
  type SubstitutePair,
} from "@/lib/api";
import { cn } from "@/lib/utils";

type CategoryMeta = {
  label: string;
  description: string;
};

const CATEGORY_ORDER = [
  "schedule",
  "payroll",
  "payment_calendar",
  "balance",
  "fixed_assets",
  "dz_kz",
];

const CATEGORY_META: Record<string, CategoryMeta> = {
  schedule: {
    label: "График сотрудников",
    description: "Планирование смен, целевой ФОТ и нетиповые дни.",
  },
  payroll: {
    label: "Зарплата",
    description: "Payroll-недели, смены и правила расчёта.",
  },
  payment_calendar: {
    label: "Платёжный календарь",
    description: "Сопоставление план/факт и параметры прогноза выручки.",
  },
  balance: {
    label: "Баланс",
    description: "Сроки закрытия и правила фиксации финансового состояния.",
  },
  fixed_assets: {
    label: "Учёт ОС",
    description: "Пороги признания, ремонт и модернизация основных средств.",
  },
  dz_kz: {
    label: "Учёт ДЗ/КЗ",
    description: "Правила контроля дебиторской и кредиторской задолженности.",
  },
};

const HIDDEN_SETTING_KEYS = new Set([
  "payroll.fund_rates_by_tenure",
  "payroll.substitute_pairs",
]);

const SUBSTITUTE_FROM_POSITIONS = [
  "Управляющий",
  "Менеджер",
  "Системный администратор",
  "Курьер",
  "Уборщица",
  "Посудомойка",
  "Кассир",
  "Повар",
];
const SUBSTITUTE_TARGET_POSITIONS: SubstitutePair["to_position"][] = ["Повар", "Кассир"];

const LEGACY_CATEGORY_TO_SLUG: Record<string, string> = {
  График: "schedule",
  "График сотрудников": "schedule",
  Зарплата: "payroll",
  "Платёжный календарь": "payment_calendar",
  "Финансы/Баланс": "balance",
  "Финансы / Баланс": "balance",
  Баланс: "balance",
  "Учёт ОС": "fixed_assets",
  "Учёт ДЗ/КЗ": "dz_kz",
};

export function SettingsRoute() {
  const queryClient = useQueryClient();
  const auth = useAuthSnapshot();
  const [selectedCategory, setSelectedCategory] = useState<string | undefined>();
  const [historyKey, setHistoryKey] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, unknown>>({});
  const [substituteDrafts, setSubstituteDrafts] = useState<SubstitutePair[]>([]);
  const [savingKeys, setSavingKeys] = useState<Set<string>>(() => new Set());
  const saveTimers = useRef<Record<string, ReturnType<typeof window.setTimeout>>>({});
  const developerMode = useMemo(
    () => import.meta.env.DEV || window.localStorage.getItem("teplo:developer-mode") === "1",
    [],
  );

  const settingsQuery = useQuery({
    queryKey: ["settings"],
    queryFn: () => getSettings(),
  });

  const substitutePairsQuery = useQuery({
    queryKey: ["substitute-pairs"],
    queryFn: getSubstitutePairs,
  });

  const settings = useMemo(
    () => (settingsQuery.data ?? []).filter((setting) => !HIDDEN_SETTING_KEYS.has(setting.key)),
    [settingsQuery.data],
  );
  const selectedSetting = settings.find((setting) => setting.key === historyKey) ?? null;

  const historyQuery = useQuery({
    queryKey: ["settings-history", historyKey],
    queryFn: () => getSettingHistory(historyKey ?? ""),
    enabled: Boolean(historyKey),
  });

  const updateMutation = useMutation({
    mutationFn: ({ key, value }: { key: string; value: unknown }) => updateSetting(key, value),
    onSuccess: async (_, variables) => {
      setDrafts((current) => {
        if (!valuesEqual(current[variables.key], variables.value)) {
          return current;
        }
        const next = { ...current };
        delete next[variables.key];
        return next;
      });
      toast.success("Сохранено");
      await queryClient.invalidateQueries({ queryKey: ["settings"] });
      await queryClient.invalidateQueries({ queryKey: ["settings-history", variables.key] });
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось сохранить настройку"));
    },
    onSettled: (_, __, variables) => {
      if (!variables) {
        return;
      }
      setSavingKeys((current) => {
        const next = new Set(current);
        next.delete(variables.key);
        return next;
      });
    },
  });

  const substitutePairsMutation = useMutation({
    mutationFn: updateSubstitutePairs,
    onSuccess: async (result) => {
      setSubstituteDrafts(result.pairs);
      toast.success("Пары подмен сохранены");
      await queryClient.invalidateQueries({ queryKey: ["substitute-pairs"] });
      await queryClient.invalidateQueries({ queryKey: ["settings"] });
      await queryClient.invalidateQueries({ queryKey: ["employees"] });
      await queryClient.invalidateQueries({ queryKey: ["employees-roster"] });
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось сохранить пары подмен"));
    },
  });

  useEffect(() => {
    if (substitutePairsQuery.data) {
      setSubstituteDrafts(substitutePairsQuery.data.pairs);
    }
  }, [substitutePairsQuery.data]);

  useEffect(() => {
    const timers = saveTimers.current;
    return () => {
      Object.values(timers).forEach(window.clearTimeout);
    };
  }, []);

  const categories = useMemo(() => {
    const found = Array.from(
      new Set(settings.map((setting) => normalizeCategory(setting.category))),
    );
    return found.sort(
      (a, b) =>
        categoryIndex(a) - categoryIndex(b) ||
        labelForCategory(a).localeCompare(labelForCategory(b), "ru"),
    );
  }, [settings]);

  const groupedSettings = useMemo(() => {
    const groups = new Map<string, AppSetting[]>();
    settings.forEach((setting) => {
      const category = normalizeCategory(setting.category);
      if (selectedCategory && category !== selectedCategory) {
        return;
      }
      const items = groups.get(category) ?? [];
      items.push(setting);
      groups.set(category, items);
    });

    return Array.from(groups.entries())
      .sort(
        ([a], [b]) =>
          categoryIndex(a) - categoryIndex(b) ||
          labelForCategory(a).localeCompare(labelForCategory(b), "ru"),
      )
      .map(
        ([category, items]) =>
          [
            category,
            items.sort((a, b) => a.display_name.localeCompare(b.display_name, "ru")),
          ] as const,
      );
  }, [selectedCategory, settings]);

  const isOwner = Boolean(auth.user?.roles.includes("owner"));
  const showSubstitutePairsPanel = !selectedCategory || selectedCategory === "payroll";
  const substitutePairs = substitutePairsQuery.data?.pairs ?? [];
  const substitutePairsDirty = !valuesEqual(substituteDrafts, substitutePairs);
  const substitutePairsError = validateSubstitutePairs(substituteDrafts);

  function handleValueChange(setting: AppSetting, value: unknown) {
    setDrafts((current) => ({ ...current, [setting.key]: value }));
    window.clearTimeout(saveTimers.current[setting.key]);
    saveTimers.current[setting.key] = window.setTimeout(() => {
      setSavingKeys((current) => new Set(current).add(setting.key));
      updateMutation.mutate({ key: setting.key, value });
    }, 500);
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="Настройки"
        description="Рабочие параметры приложения с понятными названиями, безопасным редактированием и историей изменений."
        action={
          <Button
            onClick={() => {
              void queryClient.invalidateQueries({ queryKey: ["settings"] });
              void queryClient.invalidateQueries({ queryKey: ["substitute-pairs"] });
              if (historyKey) {
                void queryClient.invalidateQueries({ queryKey: ["settings-history", historyKey] });
              }
            }}
            title="Обновить"
            variant="outline"
          >
            <RefreshCw size={16} aria-hidden="true" />
            Обновить
          </Button>
        }
      />

      <div className="flex flex-wrap gap-2">
        <CategoryButton active={!selectedCategory} onClick={() => setSelectedCategory(undefined)}>
          Все
        </CategoryButton>
        {categories.map((category) => (
          <CategoryButton
            active={selectedCategory === category}
            key={category}
            onClick={() => setSelectedCategory(category)}
          >
            {labelForCategory(category)}
          </CategoryButton>
        ))}
      </div>

      {settingsQuery.isLoading ? (
        <div className="rounded-lg border bg-card px-4 py-8 text-sm text-muted-foreground">
          Загрузка настроек...
        </div>
      ) : null}

      {settingsQuery.isError ? (
        <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
          Не удалось загрузить настройки
        </div>
      ) : null}

      <div className="space-y-6">
        {showSubstitutePairsPanel ? (
          <SubstitutePairsPanel
            drafts={substituteDrafts}
            error={substitutePairsError}
            isDirty={substitutePairsDirty}
            isLoading={substitutePairsQuery.isLoading}
            isSaving={substitutePairsMutation.isPending}
            onAdd={() =>
              setSubstituteDrafts((current) => [
                ...current,
                {
                  from_position: "Управляющий",
                  to_position: "Повар",
                  add_to_schedule: false,
                },
              ])
            }
            onChange={(index, patch) =>
              setSubstituteDrafts((current) =>
                current.map((pair, pairIndex) =>
                  pairIndex === index ? { ...pair, ...patch } : pair,
                ),
              )
            }
            onDelete={(index) =>
              setSubstituteDrafts((current) =>
                current.filter((_pair, pairIndex) => pairIndex !== index),
              )
            }
            onReset={() => setSubstituteDrafts(substitutePairs)}
            onSave={() => substitutePairsMutation.mutate(substituteDrafts)}
          />
        ) : null}

        {groupedSettings.map(([category, items]) => (
          <section className="space-y-3" key={category}>
            <div className="flex flex-col gap-1 sm:flex-row sm:items-end sm:justify-between">
              <div>
                <h2 className="text-lg font-semibold tracking-normal">
                  {labelForCategory(category)}
                </h2>
                <p className="text-sm text-muted-foreground">{descriptionForCategory(category)}</p>
              </div>
              <div className="text-xs text-muted-foreground">{items.length} настроек</div>
            </div>

            <div className="grid gap-3">
              {items.map((setting) => {
                const value = draftValue(drafts, setting);
                const locked = setting.is_critical && !isOwner;
                return (
                  <SettingCard
                    developerMode={developerMode}
                    disabled={locked}
                    isSaving={savingKeys.has(setting.key)}
                    key={setting.key}
                    onChange={(nextValue) => handleValueChange(setting, nextValue)}
                    onOpenHistory={() => setHistoryKey(setting.key)}
                    setting={setting}
                    value={value}
                  />
                );
              })}
            </div>
          </section>
        ))}

        {!settingsQuery.isLoading && groupedSettings.length === 0 ? (
          <div className="rounded-lg border bg-card px-4 py-8 text-sm text-muted-foreground">
            Настройки в этом разделе пока не заданы.
          </div>
        ) : null}
      </div>

      <HistorySheet
        history={historyQuery.data ?? []}
        isLoading={historyQuery.isLoading}
        onOpenChange={(open) => {
          if (!open) {
            setHistoryKey(null);
          }
        }}
        open={Boolean(historyKey)}
        setting={selectedSetting}
        title={historyKey}
      />
    </div>
  );
}

function SubstitutePairsPanel({
  drafts,
  error,
  isDirty,
  isLoading,
  isSaving,
  onAdd,
  onChange,
  onDelete,
  onReset,
  onSave,
}: {
  drafts: SubstitutePair[];
  error: string | null;
  isDirty: boolean;
  isLoading: boolean;
  isSaving: boolean;
  onAdd: () => void;
  onChange: (index: number, patch: Partial<SubstitutePair>) => void;
  onDelete: (index: number) => void;
  onReset: () => void;
  onSave: () => void;
}) {
  return (
    <section className="space-y-3">
      <div className="flex flex-col gap-1 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 className="text-lg font-semibold tracking-normal">Подменные смены</h2>
          <p className="text-sm text-muted-foreground">
            Какие должности могут выходить в график как повар или кассир.
          </p>
        </div>
        <Button disabled={isLoading || isSaving} onClick={onAdd} size="sm" type="button" variant="outline">
          <Plus size={15} aria-hidden="true" />
          Добавить пару
        </Button>
      </div>

      <div className="grid gap-3 rounded-lg border bg-card p-4 shadow-sm">
        {isLoading ? (
          <div className="text-sm text-muted-foreground">Загрузка пар...</div>
        ) : null}

        {!isLoading && drafts.length === 0 ? (
          <div className="rounded-md border bg-muted/30 px-3 py-2 text-sm text-muted-foreground">
            Пары подмен не настроены.
          </div>
        ) : null}

        {drafts.map((pair, index) => (
          <div
            className="grid gap-2 rounded-md border bg-background p-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_180px_auto] sm:items-end"
            key={`${pair.from_position}-${pair.to_position}-${index}`}
          >
            <div className="grid gap-2">
              <span className="text-sm font-medium">Кто выходит</span>
              <Select
                disabled={isSaving}
                onValueChange={(value) => onChange(index, { from_position: value })}
                value={pair.from_position}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {SUBSTITUTE_FROM_POSITIONS.map((position) => (
                    <SelectItem key={position} value={position}>
                      {position}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="grid gap-2">
              <span className="text-sm font-medium">Кого подменяет</span>
              <Select
                disabled={isSaving}
                onValueChange={(value) =>
                  onChange(index, { to_position: value as SubstitutePair["to_position"] })
                }
                value={pair.to_position}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {SUBSTITUTE_TARGET_POSITIONS.map((position) => (
                    <SelectItem key={position} value={position}>
                      {position}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="grid gap-2">
              <span className="text-sm font-medium">Добавить в график</span>
              <div className="flex h-10 items-center gap-2 rounded-md border border-input bg-background px-3">
                <Switch
                  checked={pair.add_to_schedule}
                  disabled={isSaving}
                  onCheckedChange={(checked) => onChange(index, { add_to_schedule: checked })}
                />
                <span className="text-sm text-muted-foreground">
                  {pair.add_to_schedule ? "Да" : "Нет"}
                </span>
              </div>
            </div>

            <Button
              disabled={isSaving}
              onClick={() => onDelete(index)}
              size="icon"
              title="Удалить пару"
              type="button"
              variant="ghost"
            >
              <Trash2 size={16} aria-hidden="true" />
            </Button>
          </div>
        ))}

        {error ? (
          <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error}
          </div>
        ) : null}

        <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <Button disabled={!isDirty || isSaving} onClick={onReset} type="button" variant="outline">
            Отменить
          </Button>
          <Button disabled={!isDirty || Boolean(error) || isSaving} onClick={onSave} type="button">
            {isSaving ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : (
              <Save size={16} aria-hidden="true" />
            )}
            Сохранить пары
          </Button>
        </div>
      </div>
    </section>
  );
}

function SettingCard({
  developerMode,
  disabled,
  isSaving,
  onChange,
  onOpenHistory,
  setting,
  value,
}: {
  developerMode: boolean;
  disabled: boolean;
  isSaving: boolean;
  onChange: (value: unknown) => void;
  onOpenHistory: () => void;
  setting: AppSetting;
  value: unknown;
}) {
  return (
    <article className="grid gap-4 rounded-lg border bg-card p-4 shadow-sm lg:grid-cols-[minmax(0,1fr)_minmax(300px,430px)]">
      <div className="min-w-0">
        <div className="flex items-start gap-2">
          <h3 className="min-w-0 text-base font-semibold leading-6">{setting.display_name}</h3>
          {setting.is_critical ? (
            <span
              className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground"
              title="Изменяет только владелец"
            >
              <Lock aria-hidden="true" className="h-3.5 w-3.5" />
            </span>
          ) : null}
        </div>
        {setting.description ? (
          <p className="mt-1 max-w-3xl text-sm leading-6 text-muted-foreground">
            {setting.description}
          </p>
        ) : null}
        <div className="mt-3 text-xs text-muted-foreground">
          {setting.updated_by_user_name ?? "system"} · {formatDateTime(setting.updated_at)}
          {isSaving ? <span> · сохранение...</span> : null}
        </div>
      </div>

      <div className="min-w-0">
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <RenderSettingWidget
              disabled={disabled}
              onChange={onChange}
              setting={setting}
              value={value}
            />
            {developerMode ? (
              <div className="mt-2 break-all font-mono text-[11px] leading-4 text-muted-foreground">
                {setting.key}
              </div>
            ) : null}
          </div>
          <Button
            onClick={onOpenHistory}
            size="icon"
            title="История"
            type="button"
            variant="outline"
          >
            <Eye aria-hidden="true" />
          </Button>
        </div>
      </div>
    </article>
  );
}

function RenderSettingWidget({
  disabled,
  onChange,
  setting,
  value,
}: {
  disabled: boolean;
  onChange: (value: unknown) => void;
  setting: AppSetting;
  value: unknown;
}) {
  const options = setting.widget_options as SettingWidgetOptions | null;
  const props = {
    disabled,
    onChange,
    options,
    unit: setting.unit,
    value,
  };

  switch (setting.widget_type) {
    case "percent":
      return <PercentWidget {...props} />;
    case "number":
      return <NumberWidget {...props} />;
    case "date":
      return <DateWidget {...props} />;
    case "date_array":
      return <DateArrayWidget {...props} />;
    case "select":
      return <SelectWidget {...props} />;
    case "boolean":
      return <BooleanWidget {...props} />;
    case "time":
      return <TimeWidget {...props} />;
    case "weekday_premium":
      return <WeekdayPremiumWidget {...props} />;
    case "text":
      return (
        <Input
          disabled={disabled}
          onChange={(event) => onChange(event.target.value)}
          value={typeof value === "string" ? value : ""}
        />
      );
    case "json":
    default:
      return <JsonWidget {...props} />;
  }
}

function HistorySheet({
  history,
  isLoading,
  onOpenChange,
  open,
  setting,
  title,
}: {
  history: Array<{
    id: string;
    old_value: unknown;
    new_value: unknown;
    changed_at: string;
    changed_by_user_name: string | null;
  }>;
  isLoading: boolean;
  onOpenChange: (open: boolean) => void;
  open: boolean;
  setting: AppSetting | null;
  title: string | null;
}) {
  return (
    <Sheet onOpenChange={onOpenChange} open={open}>
      <SheetContent className="w-full overflow-y-auto sm:max-w-xl">
        <SheetHeader>
          <SheetTitle>{setting?.display_name ?? "История настройки"}</SheetTitle>
          <SheetDescription>{title ?? "Настройка не выбрана"}</SheetDescription>
        </SheetHeader>

        <div className="mt-6 space-y-4">
          {history.map((item) => (
            <div className="relative border-l pl-4" key={item.id}>
              <span className="absolute -left-[5px] top-1.5 h-2.5 w-2.5 rounded-full bg-primary" />
              <div className="text-sm font-medium">{item.changed_by_user_name ?? "system"}</div>
              <div className="text-xs text-muted-foreground">{formatDateTime(item.changed_at)}</div>
              <div className="mt-3 grid gap-2 text-sm">
                <HistoryValue label="Было" setting={setting} value={item.old_value} />
                <HistoryValue label="Стало" setting={setting} value={item.new_value} />
              </div>
            </div>
          ))}

          {isLoading ? (
            <div className="text-sm text-muted-foreground">Загрузка истории...</div>
          ) : null}
          {!isLoading && open && history.length === 0 ? (
            <div className="text-sm text-muted-foreground">История изменений пуста.</div>
          ) : null}
        </div>
      </SheetContent>
    </Sheet>
  );
}

function HistoryValue({
  label,
  setting,
  value,
}: {
  label: string;
  setting: AppSetting | null;
  value: unknown;
}) {
  return (
    <div className="rounded-md bg-muted px-3 py-2">
      <div className="text-xs font-medium text-muted-foreground">{label}</div>
      <div className="mt-1 whitespace-pre-wrap break-words text-sm">
        {setting ? formatSettingValue(value, setting) : formatJson(value)}
      </div>
    </div>
  );
}

function CategoryButton({
  active,
  children,
  onClick,
}: {
  active: boolean;
  children: string;
  onClick: () => void;
}) {
  return (
    <button
      className={cn(
        "h-9 rounded-md border border-border px-3 text-sm font-medium transition-colors",
        active ? "bg-primary text-primary-foreground" : "bg-card text-foreground hover:bg-muted",
      )}
      onClick={onClick}
      type="button"
    >
      {children}
    </button>
  );
}

function useAuthSnapshot() {
  const [auth, setAuth] = useState(getAuthSnapshot);

  useEffect(() => {
    const unsubscribe = subscribeAuth(setAuth);
    return () => {
      unsubscribe();
    };
  }, []);

  return auth;
}

function normalizeCategory(category: string) {
  return LEGACY_CATEGORY_TO_SLUG[category] ?? category;
}

function categoryIndex(category: string) {
  const index = CATEGORY_ORDER.indexOf(category);
  return index === -1 ? CATEGORY_ORDER.length : index;
}

function labelForCategory(category: string) {
  return CATEGORY_META[category]?.label ?? category;
}

function descriptionForCategory(category: string) {
  return CATEGORY_META[category]?.description ?? "Параметры раздела.";
}

function draftValue(drafts: Record<string, unknown>, setting: AppSetting) {
  return Object.prototype.hasOwnProperty.call(drafts, setting.key)
    ? drafts[setting.key]
    : setting.value;
}

function valuesEqual(left: unknown, right: unknown) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function validateSubstitutePairs(pairs: SubstitutePair[]) {
  const seen = new Set<string>();
  for (const pair of pairs) {
    if (!pair.from_position || !pair.to_position) {
      return "Заполните обе должности в каждой паре.";
    }
    if (pair.from_position === pair.to_position) {
      return "Должность не может подменять саму себя.";
    }
    const key = `${pair.from_position}→${pair.to_position}`;
    if (seen.has(key)) {
      return "Одинаковые пары не должны повторяться.";
    }
    seen.add(key);
  }
  return null;
}

function formatSettingValue(value: unknown, setting: AppSetting) {
  const options = setting.widget_options as SettingWidgetOptions | null;
  if (value === null || value === undefined) {
    return "Пусто";
  }

  if (setting.widget_type === "percent") {
    const raw = getPath(value, options?.value_path);
    return `${formatNumber(numberValue(raw) * 100)}%`;
  }
  if (setting.widget_type === "number") {
    return [formatNumber(numberValue(value)), setting.unit].filter(Boolean).join(" ");
  }
  if (setting.widget_type === "boolean") {
    return value === true ? "Да" : "Нет";
  }
  if (setting.widget_type === "select") {
    return findSelectLabel(value, options?.options) ?? formatJson(value);
  }
  if (setting.widget_type === "date_array") {
    return formatDateArray(value);
  }
  if (setting.widget_type === "weekday_premium") {
    return formatWeekdayPremium(value, setting.unit, options);
  }
  if (
    setting.widget_type === "date" ||
    setting.widget_type === "time" ||
    setting.widget_type === "text"
  ) {
    return String(value);
  }
  return formatJson(value);
}

function formatDateArray(value: unknown) {
  if (Array.isArray(value)) {
    return value.join(", ");
  }
  if (!value || typeof value !== "object") {
    return "Пусто";
  }
  const record = value as { dates?: unknown; ranges?: unknown };
  const dates = Array.isArray(record.dates) ? record.dates.join(", ") : "";
  const ranges = Array.isArray(record.ranges)
    ? record.ranges
        .map((range) => {
          if (!range || typeof range !== "object") {
            return "";
          }
          const item = range as { start?: unknown; end?: unknown };
          return `${String(item.start ?? "")}-${String(item.end ?? "")}`;
        })
        .filter(Boolean)
        .join(", ")
    : "";
  return [dates, ranges].filter(Boolean).join("; ");
}

function formatWeekdayPremium(
  value: unknown,
  unit: string | null,
  options?: SettingWidgetOptions | null,
) {
  const config = weekdayPremiumConfig(value, options);
  const amounts = weekdayPremiumAmounts(value);
  const activeDays = WEEKDAY_PREMIUM_DAYS.map((day) => ({
    ...day,
    amount: amounts[day.key],
  })).filter((day) => day.amount > 0);

  if (activeDays.length === 0) {
    return ["0", unit].filter(Boolean).join(" ");
  }

  const daySummary = activeDays
    .map((day) => `${day.shortLabel}: ${formatNumber(day.amount)} ${unit ?? ""}`.trim())
    .join(", ");
  return `${daySummary}, от ${formatNumber(config.threshold_hours)} ч`;
}

function numberValue(value: unknown) {
  return typeof value === "number" ? value : Number(value) || 0;
}

function formatNumber(value: number) {
  return new Intl.NumberFormat("ru-RU", {
    maximumFractionDigits: 2,
  }).format(value);
}

function formatDateTime(value: string) {
  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "short",
    timeStyle: "short",
  }).format(new Date(value));
}
