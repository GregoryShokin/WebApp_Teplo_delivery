import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Plus } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
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
  getLedgerCategories,
  getNeedsSetup,
  getRegistry,
  setKassaEnabled,
  type RegistryItem,
} from "../api";
import { CreateCounterpartyDialog } from "../CreateCounterpartyDialog";
import { formatRub, RelationshipBadge } from "../shared";

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

  const queryClient = useQueryClient();
  const kassaMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => setKassaEnabled(id, enabled),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["cp", "registry"] });
    },
    onError: (error) =>
      toast.error(apiErrorMessage(error, "Не удалось переключить «Активен в Кассе»")),
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
      key: "origin",
      header: "Происхождение",
      cell: (item) => {
        // Откуда карточка появилась: различение по просьбе владельца (iiko / вручную / ЭДО / почта).
        const origin = item.origin ?? (item.has_iiko_guid ? "iiko" : null);
        if (origin === "iiko") {
          return <Badge className="border-sky-200 bg-sky-50 text-sky-700">iiko</Badge>;
        }
        if (origin === "sbis") {
          return <Badge className="border-violet-200 bg-violet-50 text-violet-700">СБИС (ЭДО)</Badge>;
        }
        if (origin === "email") {
          return <Badge className="border-teal-200 bg-teal-50 text-teal-700">Почта</Badge>;
        }
        return <Badge variant="outline">Вручную</Badge>;
      },
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
    {
      key: "prepayment_balance",
      header: "Предоплата",
      className: "text-right tabular-nums",
      headerClassName: "text-right",
      cell: (item) =>
        item.prepayment_balance > 0 ? (
          <span className="text-sky-700">{formatRub(item.prepayment_balance)}</span>
        ) : (
          "—"
        ),
    },
    {
      key: "kassa_enabled",
      header: "Касса",
      headerClassName: "text-center",
      className: "text-center",
      cell: (item) => (
        <Switch
          checked={item.kassa_enabled}
          disabled={!canOperate}
          onCheckedChange={(value) =>
            kassaMutation.mutate({ id: item.counterparty_id, enabled: value })
          }
          aria-label="Активен в Кассе"
        />
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
