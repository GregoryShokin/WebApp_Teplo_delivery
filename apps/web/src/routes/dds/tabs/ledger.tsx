import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Combobox, type ComboboxOption } from "@/components/ui/combobox";
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
  getDdsJournal,
  getDdsWallets,
  type JournalQuery,
  type JournalRow,
} from "@/lib/api";
import { getCounterpartyDirectory } from "@/routes/counterparties/api";
import { OperationClassifyDialog } from "@/routes/dds/OperationClassifyDialog";
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
type StatusFilter = "all" | "marked" | "unmarked" | "transfers";

export function LedgerTab() {
  const [status, setStatus] = useState<StatusFilter>("all");
  const [dateFrom, setDateFrom] = useState(isoDateDaysAgo(30));
  const [dateTo, setDateTo] = useState(toIsoDate(new Date()));
  const [direction, setDirection] = useState<"all" | "in" | "out">("all");
  const [walletId, setWalletId] = useState("all");
  const [articleId, setArticleId] = useState("all");
  const [counterpartyId, setCounterpartyId] = useState("all");
  const [offset, setOffset] = useState(0);
  const [selectedRow, setSelectedRow] = useState<JournalRow | null>(null);

  const permissions = usePermissions();
  const canClassify = permissions.canPerformAction("finance.cashflow.classify");

  const walletsQuery = useQuery({ queryKey: ["dds", "wallets"], queryFn: getDdsWallets });
  const articlesQuery = useQuery({ queryKey: ["dds", "articles"], queryFn: getDdsArticles });
  const counterpartiesQuery = useQuery({
    queryKey: ["cp", "directory"],
    queryFn: getCounterpartyDirectory,
  });

  const params: JournalQuery = useMemo(
    () => ({
      status,
      from: dateFrom,
      to: dateTo,
      direction,
      wallet_id: walletId,
      article_id: articleId,
      counterparty_id: counterpartyId,
      limit: LIMIT,
      offset,
    }),
    [articleId, counterpartyId, dateFrom, dateTo, direction, offset, status, walletId],
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
  const walletOptions = useMemo<ComboboxOption[]>(
    () => [
      { value: "all", label: "Все счета" },
      ...(walletsQuery.data ?? []).map((wallet) => ({
        value: wallet.id,
        label: wallet.name,
        keywords: `${wallet.code} ${wallet.bank_code ?? ""}`,
      })),
    ],
    [walletsQuery.data],
  );
  const articleOptions = useMemo<ComboboxOption[]>(
    () => [
      { value: "all", label: "Все статьи" },
      ...(articlesQuery.data ?? []).map((article) => ({
        value: article.id,
        label: article.name,
        keywords: article.code,
      })),
    ],
    [articlesQuery.data],
  );
  const counterpartyOptions = useMemo<ComboboxOption[]>(
    () => [
      { value: "all", label: "Все контрагенты" },
      ...(counterpartiesQuery.data ?? []).map((counterparty) => ({
        value: counterparty.id,
        label: counterparty.name,
        keywords: counterparty.inn ?? "",
      })),
    ],
    [counterpartiesQuery.data],
  );

  function resetPage() {
    setOffset(0);
  }

  function clearFilters() {
    setStatus("all");
    setDateFrom(isoDateDaysAgo(30));
    setDateTo(toIsoDate(new Date()));
    setDirection("all");
    setWalletId("all");
    setArticleId("all");
    setCounterpartyId("all");
    resetPage();
  }

  const markedTotal = journalQuery.data?.marked_total ?? 0;
  const unmarkedTotal = journalQuery.data?.unmarked_total ?? 0;
  const transferTotal = journalQuery.data?.transfer_total ?? 0;

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

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-2">
        <StatusTab
          active={status === "all"}
          label="Все"
          count={markedTotal + unmarkedTotal + transferTotal}
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
        <StatusTab
          active={status === "transfers"}
          label="Внутренние переводы"
          count={transferTotal}
          onClick={() => {
            setStatus("transfers");
            resetPage();
          }}
        />
      </div>

      <div className="flex flex-col gap-3 xl:flex-row xl:items-end xl:justify-between">
        <div className="grid flex-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
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
          <div className="grid gap-2">
            <Label htmlFor="dds-journal-wallet">Счёт</Label>
            <Combobox
              id="dds-journal-wallet"
              options={walletOptions}
              value={walletId}
              onChange={(value) => {
                setWalletId(value);
                resetPage();
              }}
              placeholder="Все счета"
              searchPlaceholder="Поиск счёта"
              emptyMessage="Счета не найдены"
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="dds-journal-article">Статья</Label>
            <Combobox
              id="dds-journal-article"
              options={articleOptions}
              value={articleId}
              onChange={(value) => {
                setArticleId(value);
                resetPage();
              }}
              placeholder="Все статьи"
              searchPlaceholder="Поиск статьи"
              emptyMessage="Статьи не найдены"
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="dds-journal-counterparty">Контрагент</Label>
            <Combobox
              id="dds-journal-counterparty"
              options={counterpartyOptions}
              value={counterpartyId}
              onChange={(value) => {
                setCounterpartyId(value);
                resetPage();
              }}
              placeholder="Все контрагенты"
              searchPlaceholder="Поиск по названию или ИНН"
              emptyMessage="Контрагенты не найдены"
            />
          </div>
        </div>
        <Button className="w-fit gap-2" onClick={clearFilters} type="button" variant="outline">
          <X size={16} aria-hidden="true" />
          Сбросить
        </Button>
      </div>

      <DataTable
        columns={columns}
        rows={journalQuery.data?.items ?? []}
        isLoading={journalQuery.isLoading}
        getRowKey={(row) => `${row.kind}:${row.id}`}
        onRowClick={(row) => setSelectedRow(row)}
        emptyMessage="Записей не найдено"
      />

      <PaginationControls
        limit={LIMIT}
        offset={offset}
        total={journalQuery.data?.total ?? 0}
        onOffsetChange={setOffset}
      />

      <OperationClassifyDialog
        row={selectedRow}
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
