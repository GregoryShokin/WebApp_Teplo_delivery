import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Combobox, type ComboboxOption } from "@/components/ui/combobox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ArticleCombobox } from "@/components/ui-app/ArticleCombobox";
import {
  apiErrorMessage,
  classifyCashflowTransaction,
  getDdsArticles,
  getDdsCounterparties,
  getDdsWallets,
  type CashflowClassifyPayload,
  type JournalRow,
} from "@/lib/api";
import {
  DdsStatusBadge,
  DirectionBadge,
  compactText,
  formatDate,
  formatDdsMoney,
} from "@/routes/dds/shared";

// Статьи «перевод между счетами»: у строки с такой статьёй показываем выбор счёта-получателя —
// бэкенд заводит встречную ногу перевода (наличному получателю) + TransferGroup.
const TRANSFER_OUT_ARTICLE_CODE = "vybytie_perevod_mezhdu_schetami";
const TRANSFER_IN_ARTICLE_CODE = "postuplenie_perevod_mezhdu_schetami";

type SplitRow = { key: string; articleId: string; amount: string; transferWalletId: string };

const ACTION_TOAST: Record<CashflowClassifyPayload["action"], string> = {
  split: "Проводка разнесена по статьям",
  exclude: "Проводка исключена",
};

function round2(value: number): number {
  return Math.round(value * 100) / 100;
}

/**
 * Полный разбор РУЧНОЙ проводки ДДС (без bank-операции): мультисплит по статьям (в т.ч. строка
 * «перевод между счетами» со счётом-получателем) и мягкое исключение. В отличие от операции
 * выписки, проводка сама двигает баланс кошелька — поэтому действия балансо-сохраняющие.
 */
export function CashflowClassifyDialog({
  row,
  canClassify,
  onClose,
}: {
  row: JournalRow | null;
  canClassify: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const articlesQuery = useQuery({ queryKey: ["dds", "articles"], queryFn: getDdsArticles });
  const counterpartiesQuery = useQuery({
    queryKey: ["dds", "counterparties"],
    queryFn: () => getDdsCounterparties(),
  });
  const walletsQuery = useQuery({ queryKey: ["dds", "wallets"], queryFn: getDdsWallets });
  const [rows, setRows] = useState<SplitRow[]>([]);
  const [counterpartyId, setCounterpartyId] = useState("");

  // Reset to a single row covering the whole amount whenever the transaction changes.
  useEffect(() => {
    if (row) {
      setRows([
        {
          key: crypto.randomUUID(),
          articleId: row.article_id ?? "none",
          amount: row.amount,
          transferWalletId: "",
        },
      ]);
      setCounterpartyId(row.counterparty_id ?? "");
    }
  }, [row?.id]);

  const total = row ? Number(row.amount) : 0;
  const allocated = round2(rows.reduce((sum, item) => sum + (Number(item.amount) || 0), 0));
  const remainder = round2(total - allocated);
  const balanced = Math.abs(remainder) < 0.005 && rows.every((item) => Number(item.amount) > 0);

  const mutation = useMutation({
    mutationFn: (payload: CashflowClassifyPayload) =>
      classifyCashflowTransaction(row!.id, payload),
    onSuccess: async (_data, payload) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["dds", "journal"] }),
        queryClient.invalidateQueries({ queryKey: ["dds", "cashflow"] }),
        queryClient.invalidateQueries({ queryKey: ["dds", "wallets"] }),
      ]);
      toast.success(ACTION_TOAST[payload.action]);
      onClose();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить разбор")),
  });

  if (!row) {
    return null;
  }

  const articles = articlesQuery.data ?? [];
  // Id транзитных статей — по коду; у строки с такой статьёй показываем выбор счёта-получателя.
  const transferArticleIds = new Set(
    articles
      .filter(
        (article) =>
          article.code === TRANSFER_OUT_ARTICLE_CODE ||
          article.code === TRANSFER_IN_ARTICLE_CODE,
      )
      .map((article) => article.id),
  );
  const isTransferRow = (articleId: string) => transferArticleIds.has(articleId);

  function updateRow(key: string, patch: Partial<SplitRow>) {
    setRows((current) => current.map((item) => (item.key === key ? { ...item, ...patch } : item)));
  }

  function addRow() {
    setRows((current) => [
      ...current,
      {
        key: crypto.randomUUID(),
        articleId: "none",
        amount: remainder > 0 ? String(remainder) : "",
        transferWalletId: "",
      },
    ]);
  }

  function removeRow(key: string) {
    setRows((current) => (current.length > 1 ? current.filter((item) => item.key !== key) : current));
  }

  function submitSplit() {
    if (rows.some((item) => item.articleId === "none")) {
      toast.error("Выберите статью в каждой строке");
      return;
    }
    if (!balanced) {
      toast.error("Сумма по статьям должна равняться сумме проводки");
      return;
    }
    if (rows.some((item) => isTransferRow(item.articleId) && !item.transferWalletId)) {
      toast.error("Выберите счёт-получатель для строки перевода между счетами");
      return;
    }
    mutation.mutate({
      action: "split",
      splits: rows.map((item) => ({
        article_id: item.articleId,
        amount: item.amount,
        transfer_wallet_id: isTransferRow(item.articleId) ? item.transferWalletId || null : null,
      })),
      counterparty_id: counterpartyId || null,
    });
  }

  function submitExclude() {
    const confirmed = window.confirm(
      "Исключить проводку из ДДС? Баланс счёта изменится на её сумму. Действие обратимо — повторный разбор вернёт проводку.",
    );
    if (confirmed) {
      mutation.mutate({ action: "exclude" });
    }
  }

  const counterparties = counterpartiesQuery.data ?? [];
  const counterpartyOptions: ComboboxOption[] = [
    { value: "", label: "Не указан" },
    ...counterparties.map((cp) => ({ value: cp.id, label: cp.name, keywords: cp.inn ?? undefined })),
  ];
  // Счета-получатели перевода — все активные кошельки, кроме счёта самой проводки.
  const transferOptions: ComboboxOption[] = (walletsQuery.data ?? [])
    .filter((wallet) => wallet.id !== row.wallet_id && wallet.status === "active")
    .map((wallet) => ({ value: wallet.id, label: wallet.name, keywords: wallet.code }));
  const isBusy = mutation.isPending;

  return (
    <Dialog open={Boolean(row)} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex flex-wrap items-center gap-3">
            <span>Разбор проводки</span>
            <DirectionBadge direction={row.direction} />
            <DdsStatusBadge status={row.status} />
          </DialogTitle>
          <DialogDescription className="text-base font-medium text-foreground">
            {formatDate(row.operation_date)} · {formatDdsMoney(row.amount)}
          </DialogDescription>
        </DialogHeader>

        <div className="rounded-lg border bg-muted/20 p-3">
          <div className="text-xs font-medium uppercase text-muted-foreground">
            Назначение платежа
          </div>
          <div className="mt-1 break-words text-sm">{compactText(row.payment_purpose)}</div>
        </div>

        {canClassify ? (
          <div className="grid gap-3 border-t pt-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <Label className="text-base font-semibold">Разнести по статьям ДДС</Label>
              <div className="text-sm tabular-nums text-muted-foreground">
                Распределено {formatDdsMoney(allocated)} из {formatDdsMoney(total)}
                {Math.abs(remainder) >= 0.005 ? (
                  <span className="ml-2 font-medium text-amber-600">
                    остаток {formatDdsMoney(remainder)}
                  </span>
                ) : (
                  <span className="ml-2 font-medium text-emerald-600">сходится ✓</span>
                )}
              </div>
            </div>

            <div className="grid gap-2">
              {rows.map((item) => (
                <div key={item.key} className="grid gap-1.5">
                  <div className="grid grid-cols-[minmax(0,1fr)_140px_auto] items-center gap-2">
                    <ArticleCombobox
                      articles={articles}
                      value={item.articleId}
                      onChange={(value) => updateRow(item.key, { articleId: value })}
                    />
                    <Input
                      className="text-right tabular-nums"
                      inputMode="decimal"
                      value={item.amount}
                      onChange={(event) => updateRow(item.key, { amount: event.target.value })}
                    />
                    <Button
                      disabled={rows.length === 1}
                      onClick={() => removeRow(item.key)}
                      size="icon"
                      title="Удалить строку"
                      type="button"
                      variant="ghost"
                    >
                      <Trash2 size={16} aria-hidden="true" />
                    </Button>
                  </div>
                  {isTransferRow(item.articleId) ? (
                    <Combobox
                      options={transferOptions}
                      value={item.transferWalletId}
                      onChange={(value) => updateRow(item.key, { transferWalletId: value })}
                      placeholder="Счёт-получатель перевода…"
                      searchPlaceholder="Поиск счёта…"
                      emptyMessage="Счета не найдены"
                    />
                  ) : null}
                </div>
              ))}
            </div>

            <div>
              <Button onClick={addRow} size="sm" type="button" variant="outline">
                <Plus size={16} aria-hidden="true" />
                Добавить статью
              </Button>
            </div>

            <div className="grid gap-1.5">
              <Label className="text-sm">Контрагент</Label>
              <Combobox
                options={counterpartyOptions}
                value={counterpartyId}
                onChange={setCounterpartyId}
                placeholder="Не указан"
                searchPlaceholder="Поиск по названию или ИНН…"
              />
            </div>

            <div className="flex flex-wrap gap-2 border-t pt-4">
              <Button disabled={isBusy || !balanced} onClick={submitSplit}>
                {isBusy ? (
                  <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                ) : null}
                Разнести
              </Button>
              <Button disabled={isBusy} onClick={submitExclude} variant="outline">
                Исключить
              </Button>
            </div>
          </div>
        ) : (
          <div className="rounded-md border bg-muted/40 px-3 py-2 text-sm text-muted-foreground">
            Режим просмотра. Разбор недоступен.
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
