import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

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
  getDdsArticles,
  getDdsCashflow,
  getDdsCounterparties,
  getDdsWallets,
  type CashflowQuery,
  type CashflowTransactionRead,
} from "@/lib/api";
import {
  DetailRow,
  DirectionBadge,
  PaginationControls,
  QualityBadge,
  compactText,
  formatDate,
  formatDdsMoney,
  isoDateDaysAgo,
  toIsoDate,
} from "@/routes/dds/shared";

const LIMIT = 50;

export function LedgerTab() {
  const [dateFrom, setDateFrom] = useState(isoDateDaysAgo(30));
  const [dateTo, setDateTo] = useState(toIsoDate(new Date()));
  const [walletId, setWalletId] = useState("all");
  const [articleId, setArticleId] = useState("all");
  const [direction, setDirection] = useState<CashflowQuery["direction"]>("all");
  const [offset, setOffset] = useState(0);
  const [selectedTransaction, setSelectedTransaction] = useState<CashflowTransactionRead | null>(
    null,
  );

  const walletsQuery = useQuery({ queryKey: ["dds", "wallets"], queryFn: getDdsWallets });
  const articlesQuery = useQuery({ queryKey: ["dds", "articles"], queryFn: getDdsArticles });
  const counterpartiesQuery = useQuery({
    queryKey: ["dds", "counterparties", "ledger"],
    queryFn: () => getDdsCounterparties(),
  });

  const queryParams: CashflowQuery = useMemo(
    () => ({
      from: dateFrom,
      to: dateTo,
      wallet_id: walletId === "all" ? undefined : walletId,
      article_id: articleId === "all" ? undefined : articleId,
      direction,
      limit: LIMIT,
      offset,
    }),
    [articleId, dateFrom, dateTo, direction, offset, walletId],
  );

  const cashflowQuery = useQuery({
    queryKey: ["dds", "cashflow", queryParams],
    queryFn: () => getDdsCashflow(queryParams),
  });

  const walletById = new Map((walletsQuery.data ?? []).map((wallet) => [wallet.id, wallet]));
  const articleById = new Map((articlesQuery.data ?? []).map((article) => [article.id, article]));
  const counterpartyById = new Map(
    (counterpartiesQuery.data ?? []).map((counterparty) => [counterparty.id, counterparty]),
  );

  function resetPage() {
    setOffset(0);
  }

  const columns: Array<DataTableColumn<CashflowTransactionRead>> = [
    {
      key: "date",
      header: "Дата",
      cell: (transaction) => formatDate(transaction.operation_date),
      className: "whitespace-nowrap",
    },
    {
      key: "wallet",
      header: "Кошелёк",
      cell: (transaction) => walletById.get(transaction.wallet_id)?.name ?? transaction.wallet_id,
      className: "min-w-[160px]",
    },
    {
      key: "article",
      header: "Статья",
      cell: (transaction) =>
        transaction.article_id
          ? articleById.get(transaction.article_id)?.name ?? transaction.article_id
          : "—",
      className: "min-w-[180px]",
    },
    {
      key: "counterparty",
      header: "Контрагент",
      cell: (transaction) =>
        transaction.counterparty_id
          ? counterpartyById.get(transaction.counterparty_id)?.name ?? transaction.counterparty_id
          : "—",
      className: "min-w-[160px]",
    },
    {
      key: "direction",
      header: "Направление",
      cell: (transaction) => <DirectionBadge direction={transaction.direction} />,
    },
    {
      key: "amount",
      header: "Сумма",
      cell: (transaction) => formatDdsMoney(transaction.amount),
      className: "font-medium tabular-nums",
    },
    {
      key: "quality",
      header: "Статус качества",
      cell: (transaction) => <QualityBadge quality={transaction.quality_status} />,
    },
  ];

  return (
    <div className="space-y-5">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        <div className="grid gap-2">
          <Label htmlFor="dds-ledger-from">Дата с</Label>
          <Input
            id="dds-ledger-from"
            type="date"
            value={dateFrom}
            onChange={(event) => {
              setDateFrom(event.target.value);
              resetPage();
            }}
          />
        </div>
        <div className="grid gap-2">
          <Label htmlFor="dds-ledger-to">Дата по</Label>
          <Input
            id="dds-ledger-to"
            type="date"
            value={dateTo}
            onChange={(event) => {
              setDateTo(event.target.value);
              resetPage();
            }}
          />
        </div>
        <div className="grid gap-2">
          <Label>Кошелёк</Label>
          <Select
            value={walletId}
            onValueChange={(value) => {
              setWalletId(value);
              resetPage();
            }}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Все</SelectItem>
              {(walletsQuery.data ?? []).map((wallet) => (
                <SelectItem key={wallet.id} value={wallet.id}>
                  {wallet.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="grid gap-2">
          <Label>Статья</Label>
          <Select
            value={articleId}
            onValueChange={(value) => {
              setArticleId(value);
              resetPage();
            }}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">Все</SelectItem>
              {(articlesQuery.data ?? []).map((article) => (
                <SelectItem key={article.id} value={article.id}>
                  {article.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="grid gap-2">
          <Label>Направление</Label>
          <Select
            value={direction}
            onValueChange={(value) => {
              setDirection(value as CashflowQuery["direction"]);
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
        rows={cashflowQuery.data?.items ?? []}
        isLoading={cashflowQuery.isLoading}
        getRowKey={(transaction) => transaction.id}
        onRowClick={setSelectedTransaction}
        emptyMessage="Транзакции не найдены"
      />

      <PaginationControls
        limit={LIMIT}
        offset={offset}
        total={cashflowQuery.data?.total ?? 0}
        onOffsetChange={setOffset}
      />

      <Sheet
        open={Boolean(selectedTransaction)}
        onOpenChange={(open) => !open && setSelectedTransaction(null)}
      >
        <SheetContent className="w-full overflow-y-auto sm:max-w-xl">
          <SheetHeader>
            <SheetTitle>Транзакция ДДС</SheetTitle>
            <SheetDescription>
              {selectedTransaction
                ? `${formatDate(selectedTransaction.operation_date)} · ${formatDdsMoney(
                    selectedTransaction.amount,
                  )}`
                : ""}
            </SheetDescription>
          </SheetHeader>
          {selectedTransaction ? (
            <div className="mt-5">
              <DetailRow
                label="Кошелёк"
                value={walletById.get(selectedTransaction.wallet_id)?.name ?? selectedTransaction.wallet_id}
              />
              <DetailRow
                label="Статья"
                value={
                  selectedTransaction.article_id
                    ? articleById.get(selectedTransaction.article_id)?.name ??
                      selectedTransaction.article_id
                    : "—"
                }
              />
              <DetailRow
                label="Контрагент"
                value={
                  selectedTransaction.counterparty_id
                    ? counterpartyById.get(selectedTransaction.counterparty_id)?.name ??
                      selectedTransaction.counterparty_id
                    : "—"
                }
              />
              <DetailRow label="Направление" value={<DirectionBadge direction={selectedTransaction.direction} />} />
              <DetailRow label="Назначение" value={compactText(selectedTransaction.payment_purpose)} />
              <DetailRow label="Комментарий" value={compactText(selectedTransaction.comment)} />
              <DetailRow label="Источник" value={selectedTransaction.source_kind} />
              <DetailRow label="Bank operation" value={selectedTransaction.source_id ?? "—"} />
              <DetailRow label="Transfer group" value={selectedTransaction.transfer_group_id ?? "—"} />
            </div>
          ) : null}
        </SheetContent>
      </Sheet>
    </div>
  );
}
