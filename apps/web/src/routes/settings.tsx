import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, History, LogOut, Pencil, RefreshCw, X } from "lucide-react";

import { Button } from "../components/ui/button";
import {
  getSettingHistory,
  getSettings,
  logout,
  updateSetting,
  type AppSetting,
} from "../lib/api";
import { getCurrentUser } from "../lib/auth";
import { cn } from "../lib/utils";

const CRITICAL_SETTING_KEYS = new Set([
  "balance_close_deadline",
  "fixed_asset_threshold",
  "repair_vs_modernization_pct",
  "balance.close_day",
  "balance.close_deadline",
  "fixed_assets.capitalization_threshold_rub",
  "fixed_assets.threshold_rub",
  "fixed_assets.repair_modernization_threshold_ratio",
  "fixed_assets.repair_vs_modernization_pct",
]);

type SettingsRouteProps = {
  onNavigate: (path: string) => void;
};

type EditingState = {
  key: string;
  draft: string;
};

export function SettingsRoute({ onNavigate }: SettingsRouteProps) {
  const queryClient = useQueryClient();
  const [selectedCategory, setSelectedCategory] = useState<string | undefined>();
  const [editing, setEditing] = useState<EditingState | null>(null);
  const [historyKey, setHistoryKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const user = getCurrentUser();

  const settingsQuery = useQuery({
    queryKey: ["settings", selectedCategory],
    queryFn: () => getSettings(selectedCategory),
  });

  const allSettingsQuery = useQuery({
    queryKey: ["settings", "all-categories"],
    queryFn: () => getSettings(),
  });

  const historyQuery = useQuery({
    queryKey: ["settings-history", historyKey],
    queryFn: () => getSettingHistory(historyKey ?? ""),
    enabled: Boolean(historyKey),
  });

  const updateMutation = useMutation({
    mutationFn: ({ key, value }: { key: string; value: unknown }) => updateSetting(key, value),
    onSuccess: async (_, variables) => {
      setEditing(null);
      setError(null);
      await queryClient.invalidateQueries({ queryKey: ["settings"] });
      await queryClient.invalidateQueries({ queryKey: ["settings-history", variables.key] });
    },
    onError: () => setError("Не удалось сохранить настройку"),
  });

  const categories = useMemo(() => {
    const names = new Set((allSettingsQuery.data ?? []).map((setting) => setting.category));
    return Array.from(names).sort((a, b) => a.localeCompare(b, "ru"));
  }, [allSettingsQuery.data]);

  async function handleLogout() {
    await logout();
    onNavigate("/login");
  }

  function startEditing(setting: AppSetting) {
    setError(null);
    setEditing({ key: setting.key, draft: stringifyValue(setting.value) });
  }

  function saveSetting(setting: AppSetting) {
    if (!editing || editing.key !== setting.key) {
      return;
    }
    let nextValue: unknown;
    try {
      nextValue = parseDraft(setting, editing.draft);
    } catch {
      setError("Проверьте формат значения");
      return;
    }
    if (!window.confirm("Сохранить изменение настройки?")) {
      return;
    }
    updateMutation.mutate({ key: setting.key, value: nextValue });
  }

  return (
    <main className="min-h-screen bg-background">
      <div className="mx-auto flex min-h-screen max-w-7xl flex-col px-4 py-4 sm:px-6">
        <header className="flex flex-col gap-4 border-b border-border pb-4 md:flex-row md:items-center md:justify-between">
          <div>
            <h1 className="text-2xl font-semibold tracking-normal">Настройки</h1>
            <div className="mt-1 text-sm text-muted-foreground">
              {user ? `${user.full_name} · ${user.roles.join(", ")}` : "Тепло"}
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button
              onClick={() => {
                void queryClient.invalidateQueries({ queryKey: ["settings"] });
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
            <Button onClick={handleLogout} title="Выйти" variant="outline">
              <LogOut size={16} aria-hidden="true" />
              Выйти
            </Button>
          </div>
        </header>

        <div className="mt-4 flex flex-wrap gap-2">
          <CategoryButton active={!selectedCategory} onClick={() => setSelectedCategory(undefined)}>
            Все
          </CategoryButton>
          {categories.map((category) => (
            <CategoryButton
              active={selectedCategory === category}
              key={category}
              onClick={() => setSelectedCategory(category)}
            >
              {category}
            </CategoryButton>
          ))}
        </div>

        {error ? <div className="mt-4 rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</div> : null}

        <div className="mt-4 grid flex-1 gap-4 xl:grid-cols-[minmax(0,1fr)_380px]">
          <section className="overflow-hidden rounded-lg border border-border bg-white">
            <div className="grid grid-cols-[minmax(180px,1fr)_minmax(220px,1.2fr)_96px] gap-3 border-b border-border px-4 py-3 text-xs font-semibold uppercase text-muted-foreground">
              <div>Ключ</div>
              <div>Значение</div>
              <div className="text-right">Действия</div>
            </div>
            <div className="divide-y divide-border">
              {(settingsQuery.data ?? []).map((setting) => {
                const isEditing = editing?.key === setting.key;
                return (
                  <div className="grid gap-3 px-4 py-4 lg:grid-cols-[minmax(180px,1fr)_minmax(220px,1.2fr)_96px]" key={setting.key}>
                    <div className="min-w-0">
                      <div className="break-all text-sm font-semibold">{setting.key}</div>
                      <div className="mt-1 flex flex-wrap gap-2 text-xs text-muted-foreground">
                        <span>{setting.category}</span>
                        {CRITICAL_SETTING_KEYS.has(setting.key) ? <span>owner</span> : null}
                      </div>
                      {setting.description ? (
                        <div className="mt-2 text-sm text-muted-foreground">{setting.description}</div>
                      ) : null}
                    </div>

                    <div className="min-w-0">
                      {isEditing ? (
                        <textarea
                          className="min-h-24 w-full resize-y rounded-md border border-border bg-white px-3 py-2 font-mono text-sm outline-none focus:border-primary focus:ring-2 focus:ring-primary/20"
                          onChange={(event) => setEditing({ key: setting.key, draft: event.target.value })}
                          value={editing.draft}
                        />
                      ) : (
                        <pre className="max-h-40 overflow-auto rounded-md bg-muted px-3 py-2 text-sm leading-6">
                          {stringifyValue(setting.value)}
                        </pre>
                      )}
                      <div className="mt-2 text-xs text-muted-foreground">
                        {setting.updated_by_user_name ?? "system"} · {formatDate(setting.updated_at)}
                      </div>
                    </div>

                    <div className="flex justify-end gap-2">
                      {isEditing ? (
                        <>
                          <Button
                            disabled={updateMutation.isPending}
                            onClick={() => saveSetting(setting)}
                            size="icon"
                            title="Сохранить"
                          >
                            <Check size={16} aria-hidden="true" />
                          </Button>
                          <Button
                            onClick={() => setEditing(null)}
                            size="icon"
                            title="Отмена"
                            variant="outline"
                          >
                            <X size={16} aria-hidden="true" />
                          </Button>
                        </>
                      ) : (
                        <>
                          <Button onClick={() => startEditing(setting)} size="icon" title="Изменить" variant="outline">
                            <Pencil size={16} aria-hidden="true" />
                          </Button>
                          <Button
                            onClick={() => setHistoryKey(setting.key)}
                            size="icon"
                            title="История"
                            variant="outline"
                          >
                            <History size={16} aria-hidden="true" />
                          </Button>
                        </>
                      )}
                    </div>
                  </div>
                );
              })}

              {settingsQuery.isLoading ? (
                <div className="px-4 py-6 text-sm text-muted-foreground">Загрузка...</div>
              ) : null}
              {!settingsQuery.isLoading && (settingsQuery.data ?? []).length === 0 ? (
                <div className="px-4 py-6 text-sm text-muted-foreground">Нет настроек</div>
              ) : null}
            </div>
          </section>

          <aside className="rounded-lg border border-border bg-white">
            <div className="border-b border-border px-4 py-3">
              <h2 className="text-sm font-semibold uppercase text-muted-foreground">История</h2>
              <div className="mt-1 break-all text-sm">{historyKey ?? "Настройка не выбрана"}</div>
            </div>
            <div className="divide-y divide-border">
              {(historyQuery.data ?? []).map((item) => (
                <div className="grid gap-2 px-4 py-3" key={item.id}>
                  <div className="text-xs text-muted-foreground">
                    {item.changed_by_user_name ?? "system"} · {formatDate(item.changed_at)}
                  </div>
                  <HistoryValue label="Старое" value={item.old_value} />
                  <HistoryValue label="Новое" value={item.new_value} />
                </div>
              ))}
              {historyKey && historyQuery.isLoading ? (
                <div className="px-4 py-6 text-sm text-muted-foreground">Загрузка...</div>
              ) : null}
              {historyKey && !historyQuery.isLoading && (historyQuery.data ?? []).length === 0 ? (
                <div className="px-4 py-6 text-sm text-muted-foreground">История пуста</div>
              ) : null}
            </div>
          </aside>
        </div>
      </div>
    </main>
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
        active ? "bg-primary text-primary-foreground" : "bg-white text-foreground hover:bg-muted",
      )}
      onClick={onClick}
      type="button"
    >
      {children}
    </button>
  );
}

function HistoryValue({ label, value }: { label: string; value: unknown }) {
  return (
    <div>
      <div className="text-xs font-medium text-muted-foreground">{label}</div>
      <pre className="mt-1 max-h-32 overflow-auto rounded-md bg-muted px-2 py-1.5 text-xs leading-5">
        {stringifyValue(value)}
      </pre>
    </div>
  );
}

function stringifyValue(value: unknown) {
  if (typeof value === "string") {
    return value;
  }
  return JSON.stringify(value, null, 2);
}

function parseDraft(setting: AppSetting, draft: string) {
  if (setting.value_type === "object") {
    return JSON.parse(draft);
  }
  if (["integer", "money"].includes(setting.value_type)) {
    const value = Number.parseInt(draft, 10);
    if (Number.isNaN(value)) {
      throw new Error("Invalid integer");
    }
    return value;
  }
  if (setting.value_type === "number") {
    const value = Number.parseFloat(draft);
    if (Number.isNaN(value)) {
      throw new Error("Invalid number");
    }
    return value;
  }
  return draft;
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "short",
    timeStyle: "short",
  }).format(new Date(value));
}
