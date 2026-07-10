import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Eye } from "lucide-react";

import { Button } from "@/components/ui/button";
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
import { usePermissions } from "@/lib/permissions";
import { OperationClassifyDialog } from "@/routes/dds/OperationClassifyDialog";
import {
  getDdsBankOperations,
  type BankOperationRead,
  type BankOperationsQuery,
  type JournalRow,
} from "@/lib/api";
import {
  BankSyncButton,
  DdsStatusBadge,
  DirectionBadge,
  PaginationControls,
  ProviderBadge,
  compactText,
  formatDate,
  formatDdsMoney,
  isoDateDaysAgo,
  statusLabels,
  toIsoDate,
} from "@/routes/dds/shared";

const LIMIT = 50;

export function OperationsTab() {
  const initialFilters = readOperationFilters();
  const [dateFrom, setDateFrom] = useState(initialFilters.from);
  const [dateTo, setDateTo] = useState(initialFilters.to);
  const [provider, setProvider] = useState(initialFilters.provider);
  const [status, setStatus] = useState(initialFilters.classification_status);
  const [offset, setOffset] = useState(0);
  const [selectedOperation, setSelectedOperation] = useState<BankOperationRead | null>(null);
  const permissions = usePermissions();
  const canClassify = permissions.canPerformAction("finance.cashflow.classify");

  const queryParams: BankOperationsQuery = useMemo(
    () => ({
      from: dateFrom,
      to: dateTo,
      provider,
      classification_status: status,
      limit: LIMIT,
      offset,
    }),
    [dateFrom, dateTo, offset, provider, status],
  );

  const operationsQuery = useQuery({
    queryKey: ["dds", "operations", queryParams],
    queryFn: () => getDdsBankOperations(queryParams),
  });

  function updateFilter(key: string, value: string) {
    setOffset(0);
    if (key === "from") {
      setDateFrom(value);
    }
    if (key === "to") {
      setDateTo(value);
    }
    if (key === "provider") {
      setProvider(value as BankOperationsQuery["provider"]);
    }
    if (key === "classification_status") {
      setStatus(value as BankOperationsQuery["classification_status"]);
    }
    updateSearchParam(key, value);
  }

  const columns: Array<DataTableColumn<BankOperationRead>> = [
    {
      key: "date",
      header: "Дата",
      cell: (operation) => formatDate(operation.operation_date),
      className: "whitespace-nowrap",
    },
    {
      key: "provider",
      header: "Провайдер",
      cell: (operation) => <ProviderBadge provider={operation.provider} />,
    },
    {
      key: "amount",
      header: "Сумма",
      cell: (operation) => formatDdsMoney(operation.amount),
      className: "font-medium tabular-nums",
    },
    {
      key: "direction",
      header: "Направление",
      cell: (operation) => <DirectionBadge direction={operation.direction} />,
    },
    {
      key: "counterparty",
      header: "Контрагент",
      cell: (operation) => compactText(operation.counterparty_name_raw),
      className: "min-w-[180px]",
    },
    {
      key: "purpose",
      header: "Назначение",
      cell: (operation) => (
        <div className="max-w-[360px] truncate">{compactText(operation.payment_purpose)}</div>
      ),
    },
    {
      key: "status",
      header: "Статус",
      cell: (operation) => <DdsStatusBadge status={operation.classification_status} />,
    },
    {
      key: "open",
      header: "",
      className: "text-right",
      cell: () => (
        <Button size="icon" variant="ghost" title="Открыть операцию">
          <Eye size={16} aria-hidden="true" />
        </Button>
      ),
    },
  ];

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 xl:flex-row xl:items-end xl:justify-between">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <div className="grid gap-2">
            <Label htmlFor="dds-ops-from">Дата с</Label>
            <Input
              id="dds-ops-from"
              type="date"
              value={dateFrom}
              onChange={(event) => updateFilter("from", event.target.value)}
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="dds-ops-to">Дата по</Label>
            <Input
              id="dds-ops-to"
              type="date"
              value={dateTo}
              onChange={(event) => updateFilter("to", event.target.value)}
            />
          </div>
          <div className="grid gap-2">
            <Label>Провайдер</Label>
            <Select value={provider} onValueChange={(value) => updateFilter("provider", value)}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">Все</SelectItem>
                <SelectItem value="sber">Сбер</SelectItem>
                <SelectItem value="tbank">T-Bank</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="grid gap-2">
            <Label>Статус</Label>
            <Select
              value={status}
              onValueChange={(value) => updateFilter("classification_status", value)}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">Все</SelectItem>
                {["pending", "classified", "internal_transfer", "needs_review", "excluded"].map(
                  (item) => (
                    <SelectItem key={item} value={item}>
                      {statusLabels[item]}
                    </SelectItem>
                  ),
                )}
              </SelectContent>
            </Select>
          </div>
        </div>
        <BankSyncButton />
      </div>

      <DataTable
        columns={columns}
        rows={operationsQuery.data?.items ?? []}
        isLoading={operationsQuery.isLoading}
        getRowKey={(operation) => operation.id}
        onRowClick={setSelectedOperation}
        emptyMessage="Операции не найдены"
      />

      <PaginationControls
        limit={LIMIT}
        offset={offset}
        total={operationsQuery.data?.total ?? 0}
        onOffsetChange={setOffset}
      />

      <OperationClassifyDialog
        row={selectedOperation ? operationToJournalRow(selectedOperation) : null}
        canClassify={canClassify}
        onClose={() => setSelectedOperation(null)}
      />
    </div>
  );
}

// Единая модалка разбора принимает JournalRow — операцию выписки подаём в её форме.
function operationToJournalRow(operation: BankOperationRead): JournalRow {
  return {
    kind: "operation",
    id: operation.id,
    bank_operation_id: operation.id,
    status: operation.classification_status,
    operation_date: operation.operation_date,
    occurred_at: operation.posted_at ?? operation.operation_date,
    direction: operation.direction,
    amount: operation.amount,
    article_id: null,
    counterparty_id: null,
    wallet_id: null,
    provider: operation.provider,
    payment_purpose: operation.payment_purpose,
    counterparty_name_raw: operation.counterparty_name_raw,
    counterparty_inn_raw: operation.counterparty_inn_raw,
    is_card: operation.is_card,
  };
}

function readOperationFilters() {
  const params = new URLSearchParams(window.location.search);
  return {
    from: params.get("from") ?? isoDateDaysAgo(30),
    to: params.get("to") ?? toIsoDate(new Date()),
    provider: (params.get("provider") ?? "all") as BankOperationsQuery["provider"],
    classification_status: (params.get("classification_status") ??
      "all") as BankOperationsQuery["classification_status"],
  };
}

function updateSearchParam(key: string, value: string) {
  const params = new URLSearchParams(window.location.search);
  if (!value || value === "all") {
    params.delete(key);
  } else {
    params.set(key, value);
  }
  const search = params.toString();
  window.history.replaceState({}, "", `${window.location.pathname}${search ? `?${search}` : ""}`);
}
