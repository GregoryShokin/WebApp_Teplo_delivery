import { useEffect, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, MoreHorizontal, Pencil, Plus, RefreshCw, RotateCcw, Trash2, X } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import {
  apiErrorMessage,
  createPosition,
  deletePosition,
  excludePosition,
  getPositions,
  restorePosition,
  syncPositionsWithIiko,
  updatePosition,
  type Position,
  type PositionArchetype,
  type PositionPayload,
  type PositionPermissionGroup,
  type PositionScheduleType,
} from "@/lib/api";
import { cn } from "@/lib/utils";

const ARCHETYPE_LABELS: Record<PositionArchetype, string> = {
  okladnik: "Оклад (полумесячная ведомость)",
  production_percent: "Производство (% с выручки, неделя)",
  shift_pool: "Посменно из пула",
  courier: "Курьерская (модуль Курьеры)",
  none: "Без оплаты в приложении",
};

const PERMISSION_GROUP_LABELS: Record<PositionPermissionGroup, string> = {
  administration: "Администрация",
  cooks: "Повара",
  cashiers: "Кассиры",
  auxiliary: "Вспомогательный персонал",
  couriers: "Курьеры",
  none: "Без группы",
};

const SCHEDULE_TYPE_LABELS: Record<PositionScheduleType, string> = {
  SESSION: "По сменам",
  FIXED: "Фиксированный",
  HOURS: "Почасовой",
};

type PositionFormState = {
  name: string;
  archetype: PositionArchetype;
  permission_group: PositionPermissionGroup;
  participates_in_access: boolean;
  schedule_type: PositionScheduleType;
};

type PositionDialogState =
  | { mode: "create" }
  | { mode: "edit"; position: Position }
  | null;

function defaultScheduleType(archetype: PositionArchetype): PositionScheduleType {
  return archetype === "okladnik" ? "FIXED" : "SESSION";
}

function emptyFormState(): PositionFormState {
  return {
    name: "",
    archetype: "none",
    permission_group: "none",
    participates_in_access: false,
    schedule_type: defaultScheduleType("none"),
  };
}

function formStateFromPosition(position: Position): PositionFormState {
  return {
    name: position.name,
    archetype: position.archetype,
    permission_group: position.permission_group,
    participates_in_access: position.participates_in_access,
    schedule_type: position.schedule_type,
  };
}

export function PositionsPanel({ canEdit }: { canEdit: boolean }) {
  const queryClient = useQueryClient();
  const [dialogState, setDialogState] = useState<PositionDialogState>(null);
  const [excludeTarget, setExcludeTarget] = useState<Position | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Position | null>(null);

  const positionsQuery = useQuery({
    queryKey: ["positions"],
    queryFn: getPositions,
  });

  const positions = [...(positionsQuery.data ?? [])].sort(
    (a, b) =>
      Number(a.status === "excluded") - Number(b.status === "excluded") ||
      a.name.localeCompare(b.name, "ru"),
  );

  async function invalidatePositions() {
    await queryClient.invalidateQueries({ queryKey: ["positions"] });
  }

  const saveMutation = useMutation({
    mutationFn: (input: { id: string | null; payload: PositionPayload }) =>
      input.id ? updatePosition(input.id, input.payload) : createPosition(input.payload),
    onSuccess: async (_, variables) => {
      toast.success(variables.id ? "Должность обновлена" : "Должность добавлена");
      setDialogState(null);
      await invalidatePositions();
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось сохранить должность"));
    },
  });

  const excludeMutation = useMutation({
    mutationFn: excludePosition,
    onSuccess: async () => {
      toast.success("Должность исключена");
      setExcludeTarget(null);
      await invalidatePositions();
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось исключить должность"));
    },
  });

  const restoreMutation = useMutation({
    mutationFn: restorePosition,
    onSuccess: async () => {
      toast.success("Должность возвращена в активные");
      await invalidatePositions();
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось вернуть должность"));
    },
  });

  const deleteMutation = useMutation({
    mutationFn: deletePosition,
    onSuccess: async () => {
      toast.success("Должность удалена");
      setDeleteTarget(null);
      await invalidatePositions();
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось удалить должность"));
    },
  });

  const syncMutation = useMutation({
    mutationFn: syncPositionsWithIiko,
    onSuccess: async (result) => {
      toast.success(
        `Связано: ${result.linked}, добавлено из iiko: ${result.imported}, ` +
          `заведено ролей в iiko: ${result.provisioned}`,
      );
      await invalidatePositions();
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось синхронизировать с iiko"));
    },
  });

  const columns: Array<DataTableColumn<Position>> = [
    {
      key: "name",
      header: "Название",
      cell: (position) => <span className="font-medium">{position.name}</span>,
    },
    {
      key: "archetype",
      header: "Оплата",
      cell: (position) => ARCHETYPE_LABELS[position.archetype],
    },
    {
      key: "access",
      header: "Доступы",
      cell: (position) =>
        position.participates_in_access ? (
          <Badge
            title={position.access_role_code ?? undefined}
            variant="outline"
          >
            Участвует
          </Badge>
        ) : (
          <span className="text-sm text-muted-foreground">—</span>
        ),
    },
    {
      key: "iiko",
      header: "iiko",
      cell: (position) =>
        position.iiko_role_code || position.iiko_role_id ? (
          <span title={position.iiko_role_id ?? undefined}>
            {position.iiko_role_code ?? position.iiko_role_id}
          </span>
        ) : (
          <span className="text-sm text-muted-foreground">—</span>
        ),
    },
    {
      key: "employee_count",
      header: "Сотрудники",
      className: "text-right tabular-nums",
      headerClassName: "text-right",
      cell: (position) => position.employee_count,
    },
    {
      key: "status",
      header: "Статус",
      cell: (position) => (
        <Badge variant={position.status === "active" ? "default" : "secondary"}>
          {position.status === "active" ? "Активна" : "Исключена"}
        </Badge>
      ),
    },
    {
      key: "actions",
      header: "",
      headerClassName: "w-12",
      className: "w-12 text-right",
      cell: (position) => (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button size="icon" title="Действия" type="button" variant="ghost">
              <MoreHorizontal size={16} aria-hidden="true" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={() => setDialogState({ mode: "edit", position })}>
              <Pencil size={15} aria-hidden="true" />
              Изменить
            </DropdownMenuItem>
            {position.status === "active" ? (
              <DropdownMenuItem onSelect={() => setExcludeTarget(position)}>
                <X size={15} aria-hidden="true" />
                Исключить
              </DropdownMenuItem>
            ) : (
              <DropdownMenuItem onSelect={() => restoreMutation.mutate(position.id)}>
                <RotateCcw size={15} aria-hidden="true" />
                Вернуть
              </DropdownMenuItem>
            )}
            <DropdownMenuItem
              className="text-destructive focus:text-destructive"
              onSelect={() => setDeleteTarget(position)}
            >
              <Trash2 size={15} aria-hidden="true" />
              Удалить
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      ),
    },
  ];

  const visibleColumns = canEdit ? columns : columns.filter((column) => column.key !== "actions");

  return (
    <section className="space-y-3">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 className="text-lg font-semibold tracking-normal">Должности</h2>
          <p className="text-sm text-muted-foreground">
            Реестр должностей: архетип оплаты, группа прав, доступы и синхронизация с iiko.
          </p>
        </div>
        {canEdit ? (
          <div className="flex flex-wrap gap-2">
            <Button
              disabled={syncMutation.isPending}
              onClick={() => syncMutation.mutate()}
              size="sm"
              type="button"
              variant="outline"
            >
              {syncMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
              ) : (
                <RefreshCw size={15} aria-hidden="true" />
              )}
              Синхронизировать с iiko
            </Button>
            <Button onClick={() => setDialogState({ mode: "create" })} size="sm" type="button">
              <Plus size={15} aria-hidden="true" />
              Добавить должность
            </Button>
          </div>
        ) : null}
      </div>

      {positionsQuery.isError ? (
        <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {apiErrorMessage(positionsQuery.error, "Не удалось загрузить реестр должностей")}
        </div>
      ) : null}

      <DataTable
        columns={visibleColumns}
        emptyMessage="Должности пока не заведены."
        getRowKey={(position) => position.id}
        isLoading={positionsQuery.isLoading}
        rowClassName={(position) =>
          cn(position.status === "excluded" && "text-muted-foreground opacity-60")
        }
        rows={positions}
      />

      <PositionDialog
        isSaving={saveMutation.isPending}
        onClose={() => setDialogState(null)}
        onSubmit={(payload) =>
          saveMutation.mutate({
            id: dialogState?.mode === "edit" ? dialogState.position.id : null,
            payload,
          })
        }
        state={dialogState}
      />

      <AlertDialog
        onOpenChange={(open) => {
          if (!open) {
            setExcludeTarget(null);
          }
        }}
        open={Boolean(excludeTarget)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              Исключить должность «{excludeTarget?.name ?? ""}»?
            </AlertDialogTitle>
            <AlertDialogDescription>
              Должность будет скрыта из выбора при создании сотрудников, но сохранится у текущих
              сотрудников. Роль в iiko не удаляется — должность можно вернуть в любой момент.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={excludeMutation.isPending}>Отмена</AlertDialogCancel>
            <AlertDialogAction
              disabled={excludeMutation.isPending}
              onClick={(event) => {
                event.preventDefault();
                if (excludeTarget) {
                  excludeMutation.mutate(excludeTarget.id);
                }
              }}
            >
              Исключить
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        onOpenChange={(open) => {
          if (!open) {
            setDeleteTarget(null);
          }
        }}
        open={Boolean(deleteTarget)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Удалить должность «{deleteTarget?.name ?? ""}»?</AlertDialogTitle>
            <AlertDialogDescription>
              Должность будет удалена безвозвратно, включая связанную роль в iiko. Если нужно лишь
              скрыть должность из выбора, используйте «Исключить».
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteMutation.isPending}>Отмена</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              disabled={deleteMutation.isPending}
              onClick={(event) => {
                event.preventDefault();
                if (deleteTarget) {
                  deleteMutation.mutate(deleteTarget.id);
                }
              }}
            >
              Удалить
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}

function PositionDialog({
  isSaving,
  onClose,
  onSubmit,
  state,
}: {
  isSaving: boolean;
  onClose: () => void;
  onSubmit: (payload: PositionPayload) => void;
  state: PositionDialogState;
}) {
  const [form, setForm] = useState<PositionFormState>(emptyFormState);
  const [scheduleTouched, setScheduleTouched] = useState(false);

  useEffect(() => {
    if (!state) {
      return;
    }
    setForm(state.mode === "edit" ? formStateFromPosition(state.position) : emptyFormState());
    setScheduleTouched(state.mode === "edit");
  }, [state]);

  const isEdit = state?.mode === "edit";
  const canSubmit = form.name.trim().length > 0 && !isSaving;

  function update(patch: Partial<PositionFormState>) {
    setForm((current) => ({ ...current, ...patch }));
  }

  function handleArchetypeChange(value: PositionArchetype) {
    update({
      archetype: value,
      ...(scheduleTouched ? {} : { schedule_type: defaultScheduleType(value) }),
    });
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmit) {
      return;
    }
    onSubmit({
      name: form.name.trim(),
      archetype: form.archetype,
      permission_group: form.permission_group,
      participates_in_access: form.participates_in_access,
      schedule_type: form.schedule_type,
    });
  }

  return (
    <Dialog
      onOpenChange={(open) => {
        if (!open) {
          onClose();
        }
      }}
      open={Boolean(state)}
    >
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-xl">
        <form className="grid gap-4" onSubmit={submit}>
          <DialogHeader>
            <DialogTitle>{isEdit ? "Изменить должность" : "Добавить должность"}</DialogTitle>
            <DialogDescription>
              {isEdit
                ? "Изменения применяются к реестру и расчётным правилам должности."
                : "Новая должность появится в выборе при создании сотрудников."}
            </DialogDescription>
          </DialogHeader>

          <Label className="grid gap-2">
            <span>Название</span>
            <Input
              autoComplete="off"
              disabled={isSaving}
              onChange={(event) => update({ name: event.target.value })}
              placeholder="Например, Кассир"
              value={form.name}
            />
          </Label>

          <Label className="grid gap-2">
            <span>Архетип оплаты</span>
            <Select
              disabled={isSaving}
              onValueChange={(value) => handleArchetypeChange(value as PositionArchetype)}
              value={form.archetype}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(Object.keys(ARCHETYPE_LABELS) as PositionArchetype[]).map((value) => (
                  <SelectItem key={value} value={value}>
                    {ARCHETYPE_LABELS[value]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Label>

          <Label className="grid gap-2">
            <span>Группа прав</span>
            <Select
              disabled={isSaving}
              onValueChange={(value) =>
                update({ permission_group: value as PositionPermissionGroup })
              }
              value={form.permission_group}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(Object.keys(PERMISSION_GROUP_LABELS) as PositionPermissionGroup[]).map(
                  (value) => (
                    <SelectItem key={value} value={value}>
                      {PERMISSION_GROUP_LABELS[value]}
                    </SelectItem>
                  ),
                )}
              </SelectContent>
            </Select>
          </Label>

          <div className="flex items-start justify-between gap-3 rounded-md border bg-muted/30 p-3">
            <div className="grid gap-1">
              <span className="text-sm font-medium">Участвует в выдаче доступов</span>
              <span className="text-xs text-muted-foreground">
                Должность появится на странице Доступы → Права должностей, где ей назначаются права.
              </span>
            </div>
            <Switch
              checked={form.participates_in_access}
              disabled={isSaving}
              onCheckedChange={(checked) => update({ participates_in_access: checked })}
            />
          </div>

          <Label className="grid gap-2">
            <span>График iiko</span>
            <Select
              disabled={isSaving}
              onValueChange={(value) => {
                setScheduleTouched(true);
                update({ schedule_type: value as PositionScheduleType });
              }}
              value={form.schedule_type}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(Object.keys(SCHEDULE_TYPE_LABELS) as PositionScheduleType[]).map((value) => (
                  <SelectItem key={value} value={value}>
                    {SCHEDULE_TYPE_LABELS[value]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Label>

          <DialogFooter>
            <Button disabled={isSaving} onClick={onClose} type="button" variant="outline">
              Отмена
            </Button>
            <Button disabled={!canSubmit} type="submit">
              {isSaving ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : null}
              {isEdit ? "Сохранить" : "Добавить"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
