import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Plus } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
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
  const [typeFilter, setTypeFilter] = useState<string>(ALL);
  const [search, setSearch] = useState("");
  const [isCreateOpen, setIsCreateOpen] = useState(false);

  const needsSetupQuery = useQuery({ queryKey: ["cp", "needs-setup"], queryFn: getNeedsSetup });
  const registryQuery = useQuery({
    // «full» в ключе: RoutingSection карточки грузит этот же реестр БЕЗ не-поставщиков —
    // без различия в ключе они бы делили кэш с разными данными.
    queryKey: ["cp", "registry", "full"],
    // Страница реестра — единственный UI управления ВСЕМИ карточками, включая
    // банк/налоговую (пикеры накладных и платежей получают только поставщиков).
    queryFn: () => getRegistry({ include_non_suppliers: true }),
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

  // Одно поле на два поиска: цифры — ИНН, буквы — название (правило владельца). Фильтруем на
  // клиенте: реестр уже загружен целиком, и буква за буквой сужать список быстрее, чем гонять
  // запросы. У неофициалов ищем и по внутреннему имени — в таблице видно именно его.
  // Тип — три корзины по смыслу денег: бартер (долг в товаре), товарные (гасятся складом),
  // услуги (все остальные). Складывается с поиском.
  const filteredRows = useMemo(() => {
    let rows = registryQuery.data ?? [];
    if (typeFilter === "barter") rows = rows.filter((item) => item.relationship === "barter");
    else if (typeFilter !== ALL)
      rows = rows.filter(
        (item) => item.relationship !== "barter" && item.contour === typeFilter,
      );
    const query = search.trim().toLowerCase();
    if (!query) return rows;
    const digits = query.replace(/\s/g, "");
    if (/^\d+$/.test(digits)) {
      return rows.filter((item) => (item.inn ?? "").includes(digits));
    }
    return rows.filter(
      (item) =>
        item.name.toLowerCase().includes(query) ||
        (item.internal_name ?? "").toLowerCase().includes(query),
    );
  }, [registryQuery.data, search, typeFilter]);

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
        <div className="flex w-full flex-col gap-3 sm:flex-row sm:items-end">
        <div className="grid w-full max-w-sm gap-2">
          <Label>Поиск</Label>
          <Input
            value={search}
            placeholder="Название или ИНН"
            onChange={(event) => setSearch(event.target.value)}
          />
        </div>
        <div className="grid w-full max-w-xs gap-2">
          <Label>Тип</Label>
          <Select value={typeFilter} onValueChange={setTypeFilter}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>Все</SelectItem>
              <SelectItem value="goods">Товарные</SelectItem>
              <SelectItem value="service">Услуги</SelectItem>
              <SelectItem value="barter">Бартер</SelectItem>
            </SelectContent>
          </Select>
        </div>
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
        rows={filteredRows}
        isLoading={registryQuery.isLoading}
        getRowKey={(item) => item.counterparty_id}
        emptyMessage="Контрагенты не найдены"
      />

      <CreateCounterpartyDialog open={isCreateOpen} onOpenChange={setIsCreateOpen} />
    </div>
  );
}
