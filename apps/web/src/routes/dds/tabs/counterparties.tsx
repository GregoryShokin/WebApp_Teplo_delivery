import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";

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
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import {
  apiErrorMessage,
  createDdsCounterparty,
  createDdsCounterpartyAlias,
  deleteDdsCounterparty,
  deleteDdsCounterpartyAlias,
  getDdsCounterparties,
  patchDdsCounterparty,
  type CounterpartyCreate,
  type CounterpartyRead,
} from "@/lib/api";
import { badgeMutedClass, compactText } from "@/routes/dds/shared";

type CounterpartyType = NonNullable<CounterpartyCreate["type"]>;

export function CounterpartiesTab({ canEdit }: { canEdit: boolean }) {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [isCreateOpen, setIsCreateOpen] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<CounterpartyRead | null>(null);

  const counterpartiesQuery = useQuery({
    queryKey: ["dds", "counterparties", search],
    queryFn: () => getDdsCounterparties({ search }),
  });
  const selected = useMemo(
    () => (counterpartiesQuery.data ?? []).find((item) => item.id === selectedId) ?? null,
    [counterpartiesQuery.data, selectedId],
  );

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteDdsCounterparty(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["dds", "counterparties"] });
      setDeleteTarget(null);
      setSelectedId(null);
      toast.success("Контрагент удалён");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось удалить контрагента")),
  });

  const columns: Array<DataTableColumn<CounterpartyRead>> = [
    {
      key: "name",
      header: "Название",
      cell: (counterparty) => <div className="font-medium">{counterparty.name}</div>,
      className: "min-w-[220px]",
    },
    { key: "inn", header: "ИНН", cell: (counterparty) => compactText(counterparty.inn) },
    { key: "type", header: "Тип", cell: (counterparty) => typeLabel(counterparty.type) },
    {
      key: "status",
      header: "Статус",
      cell: (counterparty) => (
        <Badge className={badgeMutedClass(counterparty.status === "active")}>
          {counterparty.status === "active" ? "Активен" : "Неактивен"}
        </Badge>
      ),
    },
    {
      key: "aliases",
      header: "Aliases",
      cell: (counterparty) => counterparty.aliases.length,
      className: "tabular-nums",
    },
  ];

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div className="grid w-full max-w-md gap-2">
          <Label htmlFor="dds-counterparty-search">Поиск</Label>
          <Input
            id="dds-counterparty-search"
            placeholder="Название или ИНН"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </div>
        {canEdit ? (
          <Button onClick={() => setIsCreateOpen(true)}>
            <Plus size={16} aria-hidden="true" />
            Добавить
          </Button>
        ) : null}
      </div>

      <DataTable
        columns={columns}
        rows={counterpartiesQuery.data ?? []}
        isLoading={counterpartiesQuery.isLoading}
        getRowKey={(counterparty) => counterparty.id}
        onRowClick={(counterparty) => setSelectedId(counterparty.id)}
        emptyMessage="Контрагенты не найдены"
      />

      <CounterpartyDialog open={canEdit && isCreateOpen} onOpenChange={setIsCreateOpen} />
      <CounterpartySheet
        canEdit={canEdit}
        counterparty={selected}
        onClose={() => setSelectedId(null)}
        onDelete={(counterparty) => setDeleteTarget(counterparty)}
      />

      <AlertDialog open={Boolean(deleteTarget)} onOpenChange={() => setDeleteTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Удалить контрагента?</AlertDialogTitle>
            <AlertDialogDescription>
              Контрагент станет неактивным и исчезнет из активной работы.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Отмена</AlertDialogCancel>
            <AlertDialogAction
              disabled={deleteMutation.isPending}
              onClick={() => deleteTarget && deleteMutation.mutate(deleteTarget.id)}
            >
              Удалить
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function CounterpartyDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [inn, setInn] = useState("");
  const [type, setType] = useState<CounterpartyType>("legal_entity");

  const createMutation = useMutation({
    mutationFn: () => createDdsCounterparty({ name, inn: inn || null, type }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["dds", "counterparties"] });
      setName("");
      setInn("");
      setType("legal_entity");
      onOpenChange(false);
      toast.success("Контрагент добавлен");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось добавить контрагента")),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Новый контрагент</DialogTitle>
        </DialogHeader>
        <CounterpartyForm
          inn={inn}
          name={name}
          onInnChange={setInn}
          onNameChange={setName}
          onTypeChange={setType}
          type={type}
        />
        <DialogFooter>
          <Button
            disabled={!name.trim() || createMutation.isPending}
            onClick={() => createMutation.mutate()}
          >
            {createMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : null}
            Сохранить
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function CounterpartySheet({
  canEdit,
  counterparty,
  onClose,
  onDelete,
}: {
  canEdit: boolean;
  counterparty: CounterpartyRead | null;
  onClose: () => void;
  onDelete: (counterparty: CounterpartyRead) => void;
}) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [inn, setInn] = useState("");
  const [type, setType] = useState<CounterpartyType>("legal_entity");
  const [alias, setAlias] = useState("");

  useEffect(() => {
    if (!counterparty) {
      return;
    }
    setName(counterparty.name);
    setInn(counterparty.inn ?? "");
    setType((counterparty.type as CounterpartyType) || "legal_entity");
  }, [counterparty]);

  const patchMutation = useMutation({
    mutationFn: () =>
      counterparty
        ? patchDdsCounterparty(counterparty.id, { name, inn: inn || null, type })
        : Promise.reject(new Error("Контрагент не выбран")),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["dds", "counterparties"] });
      toast.success("Контрагент сохранён");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить контрагента")),
  });

  const aliasMutation = useMutation({
    mutationFn: () =>
      counterparty
        ? createDdsCounterpartyAlias(counterparty.id, { alias })
        : Promise.reject(new Error("Контрагент не выбран")),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["dds", "counterparties"] });
      setAlias("");
      toast.success("Alias добавлен");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось добавить alias")),
  });

  const deleteAliasMutation = useMutation({
    mutationFn: deleteDdsCounterpartyAlias,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["dds", "counterparties"] });
      toast.success("Alias удалён");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось удалить alias")),
  });

  return (
    <Sheet open={Boolean(counterparty)} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-full overflow-y-auto sm:max-w-xl">
        <SheetHeader>
          <SheetTitle>Контрагент</SheetTitle>
          <SheetDescription>{counterparty?.name}</SheetDescription>
        </SheetHeader>
        {counterparty ? (
          <div className="mt-5 space-y-5">
            <CounterpartyForm
              disabled={!canEdit}
              inn={inn}
              name={name}
              onInnChange={setInn}
              onNameChange={setName}
              onTypeChange={setType}
              type={type}
            />
            {canEdit ? (
              <div className="flex flex-wrap gap-2">
                <Button
                  disabled={!name.trim() || patchMutation.isPending}
                  onClick={() => patchMutation.mutate()}
                >
                  {patchMutation.isPending ? (
                    <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                  ) : null}
                  Сохранить
                </Button>
                <Button onClick={() => onDelete(counterparty)} variant="outline">
                  <Trash2 size={16} aria-hidden="true" />
                  Удалить
                </Button>
              </div>
            ) : null}

            {canEdit || counterparty.aliases.length > 0 ? (
            <div className="space-y-3 border-t pt-5">
              <h3 className="text-sm font-semibold">Aliases</h3>
              {canEdit ? (
                <div className="flex gap-2">
                  <Input
                    placeholder="Новое имя или паттерн"
                    value={alias}
                    onChange={(event) => setAlias(event.target.value)}
                  />
                  <Button
                    disabled={!alias.trim() || aliasMutation.isPending}
                    onClick={() => aliasMutation.mutate()}
                    variant="outline"
                  >
                    Добавить
                  </Button>
                </div>
              ) : null}
              <div className="grid gap-2">
                {counterparty.aliases.map((item) => (
                  <div
                    className="flex items-center justify-between gap-2 rounded-md border p-2 text-sm"
                    key={item.id}
                  >
                    <span className="min-w-0 truncate">{item.alias}</span>
                    {canEdit ? (
                      <Button
                        onClick={() => deleteAliasMutation.mutate(item.id)}
                        size="icon"
                        title="Удалить alias"
                        variant="ghost"
                      >
                        <Trash2 size={15} aria-hidden="true" />
                      </Button>
                    ) : null}
                  </div>
                ))}
              </div>
            </div>
            ) : null}
          </div>
        ) : null}
      </SheetContent>
    </Sheet>
  );
}

function CounterpartyForm({
  disabled = false,
  inn,
  name,
  onInnChange,
  onNameChange,
  onTypeChange,
  type,
}: {
  disabled?: boolean;
  inn: string;
  name: string;
  onInnChange: (value: string) => void;
  onNameChange: (value: string) => void;
  onTypeChange: (value: CounterpartyType) => void;
  type: CounterpartyType;
}) {
  return (
    <div className="grid gap-4">
      <div className="grid gap-2">
        <Label>Название</Label>
        <Input
          disabled={disabled}
          value={name}
          onChange={(event) => onNameChange(event.target.value)}
        />
      </div>
      <div className="grid gap-2">
        <Label>ИНН</Label>
        <Input
          disabled={disabled}
          value={inn}
          onChange={(event) => onInnChange(event.target.value)}
        />
      </div>
      <div className="grid gap-2">
        <Label>Тип</Label>
        <Select
          disabled={disabled}
          value={type}
          onValueChange={(value) => onTypeChange(value as CounterpartyType)}
        >
          <SelectTrigger>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="legal_entity">Юрлицо</SelectItem>
            <SelectItem value="individual">Физлицо</SelectItem>
            <SelectItem value="bank">Банк</SelectItem>
            <SelectItem value="tax_authority">Налоговая</SelectItem>
          </SelectContent>
        </Select>
      </div>
    </div>
  );
}

function typeLabel(type: string) {
  if (type === "legal_entity") {
    return "Юрлицо";
  }
  if (type === "individual") {
    return "Физлицо";
  }
  if (type === "bank") {
    return "Банк";
  }
  if (type === "tax_authority") {
    return "Налоговая";
  }
  return type;
}
