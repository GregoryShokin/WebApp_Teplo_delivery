import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, RefreshCw, Search } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
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
  apiErrorMessage,
  getInventoryPositions,
  patchInventoryPosition,
  syncInventoryPositionsFromIiko,
  type InventoryAllocationGroup,
  type InventoryPosition,
} from "@/lib/api";
import { cn } from "@/lib/utils";

type InventoryPositionsSectionProps = {
  canWrite: boolean;
};

type ActivityFilter = "all" | "active" | "inactive";
type GroupFilter = InventoryAllocationGroup | "all" | "none";

const groupLabels: Record<InventoryAllocationGroup, string> = {
  chefs: "Кухня",
  common: "Общая",
  admins: "Бар админов",
};

const groupOrder: InventoryAllocationGroup[] = ["chefs", "common", "admins"];

export function InventoryPositionsSection({ canWrite }: InventoryPositionsSectionProps) {
  const queryClient = useQueryClient();
  const [activityFilter, setActivityFilter] = useState<ActivityFilter>("all");
  const [groupFilter, setGroupFilter] = useState<GroupFilter>("all");
  const [search, setSearch] = useState("");

  const positionsQuery = useQuery({
    queryKey: ["inventory-positions", "all"],
    queryFn: () => getInventoryPositions(true),
  });

  const syncMutation = useMutation({
    mutationFn: syncInventoryPositionsFromIiko,
    onSuccess: async (result) => {
      toast.success(`Добавлено ${result.added}, обновлено ${result.updated}`);
      await queryClient.invalidateQueries({ queryKey: ["inventory-positions"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось обновить каталог iiko")),
  });

  const patchMutation = useMutation({
    mutationFn: ({
      position,
      payload,
    }: {
      position: InventoryPosition;
      payload: { allocation_group?: InventoryAllocationGroup | null; is_active?: boolean };
    }) => patchInventoryPosition(position.id, payload),
    onSuccess: async () => {
      toast.success("Позиция сохранена");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["inventory-positions"] }),
        queryClient.invalidateQueries({ queryKey: ["inventory-audits"] }),
        queryClient.invalidateQueries({ queryKey: ["inventory-audit"] }),
      ]);
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить позицию")),
  });

  const positions = positionsQuery.data ?? [];
  const activeCount = positions.filter((position) => position.is_active).length;
  const visiblePositions = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase("ru-RU");
    return positions.filter((position) => {
      if (activityFilter === "active" && !position.is_active) {
        return false;
      }
      if (activityFilter === "inactive" && position.is_active) {
        return false;
      }
      if (groupFilter !== "all") {
        if (groupFilter === "none" && position.allocation_group !== null) {
          return false;
        }
        if (groupFilter !== "none" && position.allocation_group !== groupFilter) {
          return false;
        }
      }
      if (needle && !position.display_name.toLocaleLowerCase("ru-RU").includes(needle)) {
        return false;
      }
      return true;
    });
  }, [activityFilter, groupFilter, positions, search]);

  const isBusy = syncMutation.isPending || patchMutation.isPending;

  return (
    <section className="space-y-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <h2 className="text-lg font-semibold tracking-normal">Исходные данные → Ревизии</h2>
        <Button
          disabled={!canWrite || syncMutation.isPending}
          onClick={() => syncMutation.mutate()}
          type="button"
          variant="outline"
        >
          {syncMutation.isPending ? (
            <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
          ) : (
            <RefreshCw size={16} aria-hidden="true" />
          )}
          Обновить из iiko
        </Button>
      </div>

      {positionsQuery.isLoading ? (
        <div className="rounded-md border px-4 py-8 text-sm text-muted-foreground">
          Загрузка каталога...
        </div>
      ) : positions.length === 0 ? (
        <div className="grid min-h-[280px] place-items-center rounded-md border border-dashed px-4 py-10 text-center">
          <div className="space-y-3">
            <div>
              <h3 className="text-base font-semibold tracking-normal">Каталог не загружен</h3>
              <p className="mt-1 text-sm text-muted-foreground">
                Загрузите позиции из iiko, чтобы настроить активный whitelist.
              </p>
            </div>
            <Button
              disabled={!canWrite || syncMutation.isPending}
              onClick={() => syncMutation.mutate()}
              type="button"
            >
              {syncMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <RefreshCw size={16} aria-hidden="true" />
              )}
              Загрузить из iiko
            </Button>
          </div>
        </div>
      ) : (
        <>
          <div className="grid gap-3 rounded-md border p-3 lg:grid-cols-[auto_180px_minmax(220px,1fr)_auto] lg:items-end">
            <div className="grid gap-2">
              <span className="text-sm font-medium">Фильтр</span>
              <div className="flex rounded-md border p-1">
                <FilterButton
                  active={activityFilter === "all"}
                  label="Все"
                  onClick={() => setActivityFilter("all")}
                />
                <FilterButton
                  active={activityFilter === "active"}
                  label="Активные"
                  onClick={() => setActivityFilter("active")}
                />
                <FilterButton
                  active={activityFilter === "inactive"}
                  label="Неактивные"
                  onClick={() => setActivityFilter("inactive")}
                />
              </div>
            </div>
            <label className="grid gap-2">
              <span className="text-sm font-medium">Группа</span>
              <Select onValueChange={(value) => setGroupFilter(value as GroupFilter)} value={groupFilter}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">Все</SelectItem>
                  {groupOrder.map((group) => (
                    <SelectItem key={group} value={group}>
                      {groupLabels[group]}
                    </SelectItem>
                  ))}
                  <SelectItem value="none">—</SelectItem>
                </SelectContent>
              </Select>
            </label>
            <label className="grid gap-2">
              <span className="text-sm font-medium">Поиск</span>
              <div className="relative">
                <Search
                  aria-hidden="true"
                  className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground"
                  size={16}
                />
                <Input
                  className="pl-9"
                  onChange={(event) => setSearch(event.target.value)}
                  value={search}
                />
              </div>
            </label>
            <div className="pb-2 text-sm text-muted-foreground lg:text-right">
              {visiblePositions.length} из {positions.length} (активных: {activeCount})
            </div>
          </div>

          <div className="overflow-x-auto rounded-md border">
            <table className="w-full min-w-[720px] text-sm">
              <thead>
                <tr className="border-b bg-muted/35">
                  <HeaderCell>Название (из iiko)</HeaderCell>
                  <HeaderCell className="w-[190px]">Группа</HeaderCell>
                  <HeaderCell className="w-[120px] text-center">Активна</HeaderCell>
                </tr>
              </thead>
              <tbody>
                {visiblePositions.map((position) => (
                  <tr
                    className={cn(
                      "border-b last:border-b-0",
                      position.is_active && "bg-emerald-50/70",
                      !position.allocation_group && "bg-muted/35 text-muted-foreground",
                    )}
                    key={position.id}
                  >
                    <BodyCell>
                      <div className="font-medium text-foreground">{position.display_name}</div>
                    </BodyCell>
                    <BodyCell>
                      <GroupSelect
                        disabled={!canWrite || isBusy}
                        onChange={(allocation_group) => {
                          patchMutation.mutate({
                            position,
                            payload: {
                              allocation_group,
                              is_active: allocation_group ? position.is_active : false,
                            },
                          });
                        }}
                        value={position.allocation_group}
                      />
                    </BodyCell>
                    <BodyCell className="text-center">
                      <span
                        title={
                          position.allocation_group
                            ? undefined
                            : "Выберите группу перед активацией"
                        }
                      >
                        <Switch
                          checked={position.is_active}
                          disabled={!canWrite || isBusy || !position.allocation_group}
                          onCheckedChange={(is_active) =>
                            patchMutation.mutate({ position, payload: { is_active } })
                          }
                        />
                      </span>
                    </BodyCell>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}

function FilterButton({
  active,
  label,
  onClick,
}: {
  active: boolean;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      className={cn(
        "h-8 rounded-sm px-3 text-sm transition-colors",
        active ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted",
      )}
      onClick={onClick}
      type="button"
    >
      {label}
    </button>
  );
}

function GroupSelect({
  disabled = false,
  onChange,
  value,
}: {
  disabled?: boolean;
  onChange: (value: InventoryAllocationGroup | null) => void;
  value: InventoryAllocationGroup | null;
}) {
  return (
    <Select
      disabled={disabled}
      onValueChange={(next) => onChange(next === "none" ? null : (next as InventoryAllocationGroup))}
      value={value ?? "none"}
    >
      <SelectTrigger>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="none">—</SelectItem>
        {groupOrder.map((group) => (
          <SelectItem key={group} value={group}>
            {groupLabels[group]}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

function HeaderCell({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <th className={cn("p-3 text-left font-medium", className)}>{children}</th>;
}

function BodyCell({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <td className={cn("p-3 align-middle", className)}>{children}</td>;
}

export function InventoryGroupBadge({ group }: { group: InventoryAllocationGroup | null }) {
  if (!group) {
    return <Badge variant="secondary">—</Badge>;
  }
  return (
    <Badge
      className={cn(
        "border",
        group === "chefs" && "border-blue-200 bg-blue-50 text-blue-700 hover:bg-blue-50",
        group === "common" && "border-violet-200 bg-violet-50 text-violet-700 hover:bg-violet-50",
        group === "admins" && "border-orange-200 bg-orange-50 text-orange-700 hover:bg-orange-50",
      )}
      variant="outline"
    >
      {groupLabels[group]}
    </Badge>
  );
}
