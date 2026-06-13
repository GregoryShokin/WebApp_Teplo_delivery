import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, LoaderCircle, Plus } from "lucide-react";
import { toast } from "sonner";

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
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import { apiErrorMessage } from "@/lib/api";

import {
  createCounterparty,
  getLedgerCategories,
  getNeedsSetup,
  getRegistry,
  type RegistryItem,
} from "../api";
import { COUNTERPARTY_TYPE_LABELS, formatRub, RelationshipBadge } from "../shared";

const ALL = "all";

export function RegistryTab({
  canOperate,
  canAdmin,
  onOpenCounterparty,
}: {
  canOperate: boolean;
  canAdmin: boolean;
  onOpenCounterparty: (id: string) => void;
}) {
  const [categoryId, setCategoryId] = useState<string>(ALL);
  const [isCreateOpen, setIsCreateOpen] = useState(false);

  const categoriesQuery = useQuery({ queryKey: ["cp", "categories"], queryFn: getLedgerCategories });
  const needsSetupQuery = useQuery({ queryKey: ["cp", "needs-setup"], queryFn: getNeedsSetup });
  const registryQuery = useQuery({
    queryKey: ["cp", "registry", categoryId],
    queryFn: () => getRegistry({ category_id: categoryId === ALL ? undefined : categoryId }),
  });

  const needsSetup = needsSetupQuery.data?.count ?? 0;

  const columns: Array<DataTableColumn<RegistryItem>> = [
    {
      key: "name",
      header: "Контрагент",
      className: "min-w-[220px]",
      cell: (item) => (
        <button
          className="text-left font-medium hover:underline"
          onClick={() => onOpenCounterparty(item.counterparty_id)}
          type="button"
        >
          {item.name}
          {item.status === "requires_setup" ? (
            <Badge className="ml-2 border-amber-200 bg-amber-50 text-amber-700">новый</Badge>
          ) : null}
          {item.status === "archived" ? (
            <Badge className="ml-2 border-muted bg-muted text-muted-foreground">архив</Badge>
          ) : null}
        </button>
      ),
    },
    { key: "inn", header: "ИНН", cell: (item) => item.inn ?? "—" },
    {
      key: "relationship",
      header: "Тип",
      cell: (item) => <RelationshipBadge relationship={item.relationship} />,
    },
    {
      key: "requisites",
      header: "Реквизиты",
      cell: (item) =>
        item.requisites_verified ? (
          <Badge className="border-emerald-200 bg-emerald-50 text-emerald-700">Подтверждены</Badge>
        ) : (
          <Badge className="border-amber-200 bg-amber-50 text-amber-700">Не заполнены</Badge>
        ),
    },
    {
      key: "unpaid_count",
      header: "Накладных",
      className: "text-right tabular-nums",
      headerClassName: "text-right",
      cell: (item) => item.unpaid_count,
    },
    {
      key: "unpaid_remaining",
      header: "К оплате",
      className: "text-right tabular-nums",
      headerClassName: "text-right",
      cell: (item) => formatRub(item.unpaid_remaining),
    },
    {
      key: "receivable_remaining",
      header: "Нам должны",
      className: "text-right tabular-nums",
      headerClassName: "text-right",
      cell: (item) =>
        item.receivable_remaining > 0 ? (
          <span className="text-emerald-700">{formatRub(item.receivable_remaining)}</span>
        ) : (
          "—"
        ),
    },
  ];

  return (
    <div className="space-y-4">
      {needsSetup > 0 ? (
        <div className="flex items-start gap-3 rounded-md border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800">
          <AlertTriangle size={18} aria-hidden="true" className="mt-0.5 shrink-0" />
          <div>
            <span className="font-medium">{needsSetup} новых поставщиков из iiko.</span> Откройте
            карточку и заполните реквизиты, источники сбора и условия оплаты.
          </div>
        </div>
      ) : null}

      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div className="grid w-full max-w-xs gap-2">
          <Label>Категория (леджер)</Label>
          <Select value={categoryId} onValueChange={setCategoryId}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>Все категории</SelectItem>
              {(categoriesQuery.data ?? []).map((category) => (
                <SelectItem key={category.id} value={category.id}>
                  {category.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        {canAdmin ? (
          <Button onClick={() => setIsCreateOpen(true)}>
            <Plus size={16} aria-hidden="true" />
            Добавить контрагента
          </Button>
        ) : null}
      </div>

      <DataTable
        columns={columns}
        rows={registryQuery.data ?? []}
        isLoading={registryQuery.isLoading}
        getRowKey={(item) => item.counterparty_id}
        emptyMessage="Контрагенты не найдены"
      />

      <CreateCounterpartyDialog open={isCreateOpen} onOpenChange={setIsCreateOpen} />
    </div>
  );
}

function CreateCounterpartyDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const categoriesQuery = useQuery({
    queryKey: ["cp", "categories"],
    queryFn: getLedgerCategories,
    enabled: open,
  });
  const [name, setName] = useState("");
  const [inn, setInn] = useState("");
  const [type, setType] = useState("legal_entity");
  const [categoryId, setCategoryId] = useState<string>("");
  const [managerName, setManagerName] = useState("");
  const [managerPhone, setManagerPhone] = useState("");

  const createMutation = useMutation({
    mutationFn: () =>
      createCounterparty({
        name,
        inn: inn || null,
        type,
        ledger_category_id: categoryId || null,
        manager_name: managerName || null,
        manager_phone: managerPhone || null,
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      setName("");
      setInn("");
      setManagerName("");
      setManagerPhone("");
      onOpenChange(false);
      toast.success("Контрагент создан");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось создать контрагента")),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Новый контрагент</DialogTitle>
        </DialogHeader>
        <div className="grid gap-4">
          <div className="grid gap-2">
            <Label>Официальное название</Label>
            <Input value={name} onChange={(event) => setName(event.target.value)} />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div className="grid gap-2">
              <Label>ИНН</Label>
              <Input value={inn} onChange={(event) => setInn(event.target.value)} />
            </div>
            <div className="grid gap-2">
              <Label>Тип</Label>
              <Select value={type} onValueChange={setType}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {Object.entries(COUNTERPARTY_TYPE_LABELS).map(([value, label]) => (
                    <SelectItem key={value} value={value}>
                      {label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
          <div className="grid gap-2">
            <Label>Категория (леджер)</Label>
            <Select value={categoryId} onValueChange={setCategoryId}>
              <SelectTrigger>
                <SelectValue placeholder="Не выбрана" />
              </SelectTrigger>
              <SelectContent>
                {(categoriesQuery.data ?? []).map((category) => (
                  <SelectItem key={category.id} value={category.id}>
                    {category.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div className="grid gap-2">
              <Label>Менеджер</Label>
              <Input value={managerName} onChange={(event) => setManagerName(event.target.value)} />
            </div>
            <div className="grid gap-2">
              <Label>Телефон менеджера</Label>
              <Input
                value={managerPhone}
                onChange={(event) => setManagerPhone(event.target.value)}
              />
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button
            disabled={!name.trim() || createMutation.isPending}
            onClick={() => createMutation.mutate()}
          >
            {createMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : null}
            Создать
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
