import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

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
import {
  getDdsArticles,
  getDdsCounterparties,
  getDdsJournal,
  getDdsWallets,
  type BankOperationRead,
  type JournalQuery,
  type JournalRow,
} from "@/lib/api";
import { OperationReviewDialog } from "@/routes/dds/OperationReviewDialog";
import {
  DdsStatusBadge,
  DirectionBadge,
  PaginationControls,
  ProviderBadge,
  compactText,
  formatDateTime,
  formatDdsMoney,
  isoDateDaysAgo,
  toIsoDate,
} from "@/routes/dds/shared";

const LIMIT = 50;
type StatusFilter = "all" | "marked" | "unmarked";

export function LedgerTab() {
  const [status, setStatus] = useState<StatusFilter>("all");
  const [dateFrom, setDateFrom] = useState(isoDateDaysAgo(30));
  const [dateTo, setDateTo] = useState(toIsoDate(new Date()));
  const [direction, setDirection] = useState<"all" | "in" | "out">("all");
  const [offset, setOffset] = useState(0);
  const [selectedRow, setSelectedRow] = useState<JournalRow | null>(null);

  const permissions = usePermissions();
  const canClassify = permissions.canPerformAction("finance.cashflow.classify");

  const walletsQuery = useQuery({ queryKey: ["dds", "wallets"], queryFn: getDdsWallets });
  const articlesQuery = useQuery({ queryKey: ["dds", "articles"], queryFn: getDdsArticles });
  const counterpartiesQuery = useQuery({
    queryKey: ["dds", "counterparties", "ledger"],
    queryFn: () => getDdsCounterparties(),
  });

  const params: JournalQuery = useMemo(
    () => ({ status, from: dateFrom, to: dateTo, direction, limit: LIMIT, offset }),
    [status, dateFrom, dateTo, direction, offset],
  );
  const journalQuery = useQuery({
    queryKey: ["dds", "journal", params],
    queryFn: () => getDdsJournal(params),
  });

  const walletById = new Map((walletsQuery.data ?? []).map((wallet) => [wallet.id, wallet]));
  const articleById = new Map((articlesQuery.data ?? []).map((article) => [article.id, article]));
  const counterpartyById = new Map(
    (counterpartiesQuery.data ?? []).map((counterparty) => [counterparty.id, counterparty]),
  );

  function resetPage() {
    setOffset(0);
  }

  const markedTotal = journalQuery.data?.marked_total ?? 0;
  const unmarkedTotal = journalQuery.data?.unmarked_total ?? 0;

  const columns: Array<DataTableColumn<JournalRow>> = [
    {
      key: "date",
      header: "Дата и время",
      cell: (row) => formatDateTime(row.occurred_at),
      className: "whitespace-nowrap tabular-nums",
    },
    {
      key: "source",
      header: "Счёт / банк",
      cell: (row) =>
        row.wallet_id ? (
          (walletById.get(row.wallet_id)?.name ?? "—")
        ) : (
          <ProviderBadge provider={row.provider} />
        ),
      className: "min-w-[150px]",
    },
    {
      key: "article",
      header: "Статья / назначение",
      cell: (row) =>
        row.article_id ? (
          (articleById.get(row.article_id)?.name ?? "—")
        ) : (
          <span className="text-muted-foreground">{compactText(row.payment_purpose)}</span>
        ),
      className: "min-w-[220px] max-w-[360px] truncate",
    },
    {
      key: "counterparty",
      header: "Контрагент",
      cell: (row) =>
        row.counterparty_id
          ? (counterpartyById.get(row.counterparty_id)?.name ?? "—")
          : compactText(row.counterparty_name_raw),
      className: "min-w-[150px]",
    },
    {
      key: "direction",
      header: "Направление",
      cell: (row) => <DirectionBadge direction={row.direction} />,
    },
    {
      key: "amount",
      header: "Сумма",
      cell: (row) => formatDdsMoney(row.amount),
      className: "font-medium tabular-nums",
    },
    {
      key: "status",
      header: "Статус",
      cell: (row) => <DdsStatusBadge status={row.status} />,
    },
  ];

  // The review modal works on a bank operation — build a partial one from the row.
  const operationForReview: BankOperationRead | null = selectedRow?.bank_operation_id
    ? {
        id: selectedRow.bank_operation_id,
        provider: selectedRow.provider ?? "tbank",
        provider_operation_id: "",
        account_id: null,
        operation_date: selectedRow.operation_date,
        posted_at: null,
        direction: selectedRow.direction,
        amount: selectedRow.amount,
        currency: "RUB",
        counterparty_name_raw: selectedRow.counterparty_name_raw,
        counterparty_inn_raw: selectedRow.counterparty_inn_raw,
        counterparty_account_raw: null,
        payment_purpose: selectedRow.payment_purpose,
        document_number: null,
        classification_status: selectedRow.status === "classified" ? "classified" : "needs_review",
        cashflow_transaction_id: null,
        transfer_group_id: null,
        raw_payload: null,
      }
    : null;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-2">
        <StatusTab
          active={status === "all"}
          label="Все"
          count={markedTotal + unmarkedTotal}
          onClick={() => {
            setStatus("all");
            resetPage();
          }}
        />
        <StatusTab
          active={status === "marked"}
          label="Размеченные"
          count={markedTotal}
          onClick={() => {
            setStatus("marked");
            resetPage();
          }}
        />
        <StatusTab
          active={status === "unmarked"}
          label="Требуют проверки"
          count={unmarkedTotal}
          tone="warning"
          onClick={() => {
            setStatus("unmarked");
            resetPage();
          }}
        />
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <div className="grid gap-2">
          <Label htmlFor="dds-journal-from">Дата с</Label>
          <Input
            id="dds-journal-from"
            type="date"
            value={dateFrom}
            onChange={(event) => {
              setDateFrom(event.target.value);
              resetPage();
            }}
          />
        </div>
        <div className="grid gap-2">
          <Label htmlFor="dds-journal-to">Дата по</Label>
          <Input
            id="dds-journal-to"
            type="date"
            value={dateTo}
            onChange={(event) => {
              setDateTo(event.target.value);
              resetPage();
            }}
          />
        </div>
        <div className="grid gap-2">
          <Label>Направление</Label>
          <Select
            value={direction}
            onValueChange={(value) => {
              setDirection(value as "all" | "in" | "out");
              resetPage();
            }}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Любое</SelectItem>
              <SelectItem value="in">Поступление</SelectItem>
              <SelectItem value="out">Списание</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>

      <DataTable
        columns={columns}
        rows={journalQuery.data?.items ?? []}
        isLoading={journalQuery.isLoading}
        getRowKey={(row) => `${row.kind}:${row.id}`}
        onRowClick={(row) => {
          if (row.bank_operation_id) {
            setSelectedRow(row);
          }
        }}
        emptyMessage="Записей не найдено"
      />

      <PaginationControls
        limit={LIMIT}
        offset={offset}
        total={journalQuery.data?.total ?? 0}
        onOffsetChange={setOffset}
      />

      <OperationReviewDialog
        operation={operationForReview}
        canClassify={canClassify}
        onClose={() => setSelectedRow(null)}
      />
    </div>
  );
}

function StatusTab({
  active,
  label,
  count,
  tone,
  onClick,
}: {
  active: boolean;
  label: string;
  count: number;
  tone?: "warning";
  onClick: () => void;
}) {
  return (
    <Button className="gap-2" onClick={onClick} size="sm" variant={active ? "default" : "outline"}>
      {label}
      <span
        className={`rounded-full px-1.5 text-xs tabular-nums ${
          active
            ? "bg-white/20"
            : tone === "warning"
              ? "bg-amber-100 text-amber-700"
              : "bg-muted text-muted-foreground"
        }`}
      >
        {count}
      </span>
    </Button>
  );
}
