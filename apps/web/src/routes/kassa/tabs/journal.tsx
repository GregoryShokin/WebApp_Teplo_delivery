import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import { apiErrorMessage } from "@/lib/api";
import { formatRub } from "@/routes/counterparties/shared";
import {
  deleteKassaPayout,
  getKassaJournal,
  type KassaJournalItem,
} from "@/routes/kassa/api";
import {
  editTargetFromJournalItem,
  KassaPayoutDialog,
  type KassaPayoutEditTarget,
} from "@/routes/kassa/KassaPayoutDialog";

function currentMonth(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

function monthRange(month: string): { date_from: string; date_to: string } {
  const [year, monthNumber] = month.split("-").map(Number);
  const lastDay = new Date(year, monthNumber, 0).getDate();
  const prefix = `${year}-${String(monthNumber).padStart(2, "0")}`;
  return { date_from: `${prefix}-01`, date_to: `${prefix}-${String(lastDay).padStart(2, "0")}` };
}

function formatDate(value: string): string {
  return new Date(value).toLocaleDateString("ru-RU");
}

/** Вкладка «Кассовый журнал»: зеркало ДДС строго по ТК Черникова, других счетов нет. */
export function KassaJournalTab({ canPayout }: { canPayout: boolean }) {
  const queryClient = useQueryClient();
  const [month, setMonth] = useState(currentMonth());
  const [direction, setDirection] = useState<"all" | "in" | "out">("all");
  const [search, setSearch] = useState("");
  const [editTarget, setEditTarget] = useState<KassaPayoutEditTarget | null>(null);
  const [deleteItem, setDeleteItem] = useState<KassaJournalItem | null>(null);

  const range = useMemo(() => monthRange(month), [month]);
  const journalQuery = useQuery({
    queryKey: ["kassa", "journal", range, direction, search],
    queryFn: () =>
      getKassaJournal({
        ...range,
        direction: direction === "all" ? undefined : direction,
        search: search.trim() || undefined,
      }),
  });

  const deleteMutation = useMutation({
    mutationFn: (transactionId: string) => deleteKassaPayout(transactionId),
    onSuccess: () => {
      toast.success("Запись удалена из журнала");
      setDeleteItem(null);
      void queryClient.invalidateQueries({ queryKey: ["kassa"] });
      void queryClient.invalidateQueries({ queryKey: ["dds"] });
      void queryClient.invalidateQueries({ queryKey: ["cp"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось удалить запись")),
  });

  const journal = journalQuery.data;

  const columns: Array<DataTableColumn<KassaJournalItem>> = [
    {
      key: "date",
      header: "Дата",
      className: "whitespace-nowrap",
      cell: (item) => formatDate(item.operation_date),
    },
    {
      key: "article",
      header: "Статья и назначение",
      className: "min-w-[260px]",
      cell: (item) => (
        <div className="min-w-0">
          <div className="font-medium">{item.article_name ?? "Без статьи"}</div>
          {item.counterparty_label || item.purpose || item.comment ? (
            <div className="truncate text-xs text-muted-foreground">
              {[item.counterparty_label, item.purpose ?? item.comment]
                .filter(Boolean)
                .join(" · ")}
            </div>
          ) : null}
        </div>
      ),
    },
    {
      key: "amount",
      header: "Сумма",
      className: "text-right tabular-nums whitespace-nowrap",
      headerClassName: "text-right",
      cell: (item) =>
        item.direction === "in" ? (
          <span className="font-medium text-emerald-600">+{formatRub(item.amount)}</span>
        ) : (
          <span className="font-medium text-foreground">−{formatRub(item.amount)}</span>
        ),
    },
    {
      key: "actions",
      header: "",
      className: "text-right",
      cell: (item) =>
        canPayout && item.editable ? (
          <div className="flex justify-end gap-2">
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setEditTarget(editTargetFromJournalItem(item))}
            >
              Изменить
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setDeleteItem(item)}>
              Удалить
            </Button>
          </div>
        ) : null,
    },
  ];

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-3">
        <MetricCard
          label="В кассе сейчас"
          value={journal ? formatRub(journal.balance) : "—"}
          hint={journal?.wallet_name}
        />
        <MetricCard
          label="Приход за период"
          value={journal ? `+${formatRub(journal.period_in)}` : "—"}
          valueClassName="text-emerald-600"
        />
        <MetricCard
          label="Расход за период"
          value={journal ? `−${formatRub(journal.period_out)}` : "—"}
        />
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Input
          className="w-40"
          type="month"
          value={month}
          onChange={(event) => setMonth(event.target.value || currentMonth())}
        />
        <Select
          value={direction}
          onValueChange={(value) => setDirection(value as "all" | "in" | "out")}
        >
          <SelectTrigger className="w-36">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">Все движения</SelectItem>
            <SelectItem value="in">Приход</SelectItem>
            <SelectItem value="out">Расход</SelectItem>
          </SelectContent>
        </Select>
        <Input
          className="w-64"
          placeholder="Поиск по назначению"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
      </div>

      <DataTable
        columns={columns}
        rows={journal?.items ?? []}
        isLoading={journalQuery.isLoading}
        getRowKey={(item) => item.id}
        emptyMessage="Движений за период нет"
      />

      <KassaPayoutDialog
        open={editTarget !== null}
        onOpenChange={(open) => !open && setEditTarget(null)}
        onSaved={() => setEditTarget(null)}
        editTarget={editTarget}
      />

      <AlertDialog open={Boolean(deleteItem)} onOpenChange={() => setDeleteItem(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Удалить запись?</AlertDialogTitle>
            <AlertDialogDescription>
              {deleteItem?.kassa_flow === "employee_advance" ||
              deleteItem?.kassa_flow === "employee_loan"
                ? "Выдача будет отменена: запись уйдёт из леджера авансов, расход снимется с кассы."
                : deleteItem?.kassa_flow === "supplier_prepayment"
                  ? "Предоплата будет снята вместе с дебиторкой поставщика, расход снимется с кассы."
                  : "Запись будет удалена из кассового журнала, остаток кассы пересчитается."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Отмена</AlertDialogCancel>
            <AlertDialogAction
              disabled={deleteMutation.isPending}
              onClick={() => deleteItem && deleteMutation.mutate(deleteItem.id)}
            >
              Удалить
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function MetricCard({
  label,
  value,
  hint,
  valueClassName,
}: {
  label: string;
  value: string;
  hint?: string | null;
  valueClassName?: string;
}) {
  return (
    <Card>
      <CardContent className="pt-5">
        <div className="text-sm text-muted-foreground">{label}</div>
        <div className={`mt-1 text-2xl font-semibold tabular-nums ${valueClassName ?? ""}`}>
          {value}
        </div>
        {hint ? <div className="mt-0.5 text-xs text-muted-foreground">{hint}</div> : null}
      </CardContent>
    </Card>
  );
}
