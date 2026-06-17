import { useEffect, useMemo, useState } from "react";
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { Check, ChevronRight, LoaderCircle, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { apiErrorMessage } from "@/lib/api";
import { cn } from "@/lib/utils";
import { formatDdsMoney, formatDateTime, toDateTimeLocalInput } from "@/routes/dds/shared";
import {
  createCheque,
  getCardTransactions,
  getKassaCounterparties,
  getKassaExpenseArticles,
  type CardTransaction,
  type ChequeLinePayload,
} from "@/routes/kassa/api";
import { getProducts } from "@/routes/warehouse/api";
import { ProductSearch } from "@/routes/warehouse/ProductSearch";

// Контрагент чеков местного закупа фиксирован — выбор магазина не нужен (метка для iiko).
const LOCAL_PURCHASE_NAME = "Местный закуп";

type DraftLine = {
  key: string;
  name: string;
  productId: string | null;
  unit: string | null;
  quantity: string;
  price: string;
  articleId: string;
};

let lineSeq = 0;
function emptyLine(): DraftLine {
  lineSeq += 1;
  return { key: `l${lineSeq}`, name: "", productId: null, unit: null, quantity: "1", price: "", articleId: "" };
}

function dayLabel(issuedAt: string): string {
  const [date] = issuedAt.split("T");
  const [y, m, d] = (date ?? "").split("-");
  return d && m && y ? `${d}.${m}.${y}` : "—";
}

type CreateChequeDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: () => void;
};

export function CreateChequeDialog({ open, onOpenChange, onCreated }: CreateChequeDialogProps) {
  const queryClient = useQueryClient();
  const [issuedAt, setIssuedAt] = useState(() => toDateTimeLocalInput(new Date()));
  const [selectedOp, setSelectedOp] = useState<CardTransaction | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [cashAmount, setCashAmount] = useState("");
  const [lines, setLines] = useState<DraftLine[]>([emptyLine()]);
  const [comment, setComment] = useState("");

  const [debouncedIssuedAt, setDebouncedIssuedAt] = useState(issuedAt);
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedIssuedAt(issuedAt), 350);
    return () => clearTimeout(timer);
  }, [issuedAt]);

  const counterpartiesQuery = useQuery({
    queryKey: ["kassa", "counterparties"],
    queryFn: () => getKassaCounterparties(),
    enabled: open,
    staleTime: 60_000,
  });
  const articlesQuery = useQuery({
    queryKey: ["kassa", "expense-articles"],
    queryFn: () => getKassaExpenseArticles(),
    enabled: open,
    staleTime: 60_000,
  });
  const productsQuery = useQuery({
    queryKey: ["wh", "products", "goods-all"],
    queryFn: () => getProducts({ type: "GOODS", limit: 2000 }),
    enabled: open,
    staleTime: 60_000,
  });
  const cardTxQuery = useQuery({
    queryKey: ["kassa", "card-transactions", debouncedIssuedAt],
    queryFn: () => getCardTransactions({ issued_at: debouncedIssuedAt }),
    enabled: open && Boolean(debouncedIssuedAt),
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  });
  const cardOps = useMemo(() => cardTxQuery.data ?? [], [cardTxQuery.data]);
  const products = productsQuery.data ?? [];
  const articles = articlesQuery.data ?? [];

  const localPurchase = useMemo(
    () => (counterpartiesQuery.data ?? []).find((cp) => cp.name === LOCAL_PURCHASE_NAME),
    [counterpartiesQuery.data],
  );

  useEffect(() => {
    if (
      selectedOp &&
      !cardTxQuery.isFetching &&
      !cardOps.some((op) => op.bank_operation_id === selectedOp.bank_operation_id)
    ) {
      setSelectedOp(null);
    }
  }, [cardOps, cardTxQuery.isFetching, selectedOp]);

  const cardTotal = selectedOp?.amount ?? 0;
  const linesTotal = useMemo(
    () =>
      lines.reduce(
        (sum, line) => sum + (Number(line.quantity) || 0) * (Number(line.price) || 0),
        0,
      ),
    [lines],
  );
  const cash = Number(cashAmount) || 0;
  const paidTotal = cardTotal + cash;

  function reset() {
    setIssuedAt(toDateTimeLocalInput(new Date()));
    setSelectedOp(null);
    setCashAmount("");
    setLines([emptyLine()]);
    setComment("");
  }

  function updateLine(key: string, patch: Partial<DraftLine>) {
    setLines((prev) => prev.map((line) => (line.key === key ? { ...line, ...patch } : line)));
  }

  const createMutation = useMutation({
    mutationFn: () => {
      const bankParts = selectedOp
        ? [{ bank_operation_id: selectedOp.bank_operation_id }]
        : [];
      const payloadLines: ChequeLinePayload[] = lines
        .filter((line) => line.name.trim())
        .map((line) => ({
          name: line.name.trim(),
          quantity: Number(line.quantity) || 0,
          unit: line.unit || null,
          price: Number(line.price) || 0,
          dds_article_id: line.articleId || null,
          iiko_product_id: line.productId,
        }));
      return createCheque({
        counterparty_id: localPurchase?.id ?? "",
        issued_at: issuedAt,
        bank_parts: bankParts,
        cash_amount: cash > 0 ? cash : null,
        track_nomenclature: true,
        lines: payloadLines,
        comment: comment.trim() || null,
      });
    },
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["kassa", "cheques"] }),
        queryClient.invalidateQueries({ queryKey: ["kassa", "card-transactions"] }),
        queryClient.invalidateQueries({ queryKey: ["dds", "cashflow"] }),
        queryClient.invalidateQueries({ queryKey: ["dds", "wallets"] }),
      ]);
      toast.success("Чек создан и проведён в ДДС");
      reset();
      onCreated();
      onOpenChange(false);
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось создать чек")),
  });

  const filledLines = lines.filter(
    (line) => line.name.trim() && Number(line.quantity) > 0 && line.articleId,
  );
  const allLinesValid = lines.every(
    (line) => !line.name.trim() || (Number(line.quantity) > 0 && Boolean(line.articleId)),
  );
  const totalsMatch = Math.abs(linesTotal - paidTotal) < 0.01;
  const canSave =
    Boolean(localPurchase) &&
    Boolean(issuedAt) &&
    paidTotal > 0 &&
    filledLines.length > 0 &&
    allLinesValid &&
    totalsMatch &&
    !createMutation.isPending;

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="max-h-[88vh] max-w-2xl overflow-y-auto">
          <DialogHeader className="space-y-1">
            <DialogTitle>Новый чек — местный закуп</DialogTitle>
            <DialogDescription>
              Оплата по бизнес-карте. Статья ДДС указывается в каждой позиции.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4">
            {/* Дата покупки */}
            <Input
              type="datetime-local"
              aria-label="Дата и время покупки"
              value={issuedAt}
              onChange={(event) => setIssuedAt(event.target.value)}
            />

            {/* Операция по карте — выбор через окно за день покупки */}
            <button
              type="button"
              onClick={() => setPickerOpen(true)}
              className={cn(
                "flex w-full items-center gap-3 rounded-md border px-3 py-2.5 text-left transition-colors hover:bg-muted/60",
                selectedOp ? "border-emerald-300 bg-emerald-50" : "border-input",
              )}
            >
              <span className="min-w-0 flex-1">
                {selectedOp ? (
                  <>
                    <span className="flex items-baseline justify-between gap-2">
                      <span className="truncate text-sm">
                        {selectedOp.counterparty_name_raw ?? "Покупка по карте"}
                      </span>
                      <span className="shrink-0 text-sm font-medium tabular-nums">
                        {formatDdsMoney(selectedOp.amount)}
                      </span>
                    </span>
                    <span className="block truncate text-xs text-muted-foreground">
                      {formatDateTime(
                        selectedOp.purchased_at ?? selectedOp.posted_at ?? selectedOp.operation_date,
                      )}{" "}
                      · нажмите, чтобы изменить
                    </span>
                  </>
                ) : (
                  <span className="text-sm text-muted-foreground">
                    Операция не выбрана — выберите оплату по карте
                  </span>
                )}
              </span>
              <ChevronRight size={16} className="shrink-0 text-muted-foreground" aria-hidden="true" />
            </button>

            <div className="flex items-center justify-between gap-3">
              <span className="text-sm text-muted-foreground">+ наличными, ₽</span>
              <Input
                inputMode="decimal"
                placeholder="0"
                className="h-9 w-28 text-right"
                value={cashAmount}
                onChange={(event) => setCashAmount(event.target.value)}
              />
            </div>

            {/* Позиции: товар из номенклатуры + статья ДДС на каждую */}
            <div className="space-y-2 border-t border-dashed pt-3">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium">Позиции</span>
                <Button
                  size="sm"
                  variant="ghost"
                  className="text-muted-foreground"
                  onClick={() => setLines((prev) => [...prev, emptyLine()])}
                >
                  <Plus size={14} aria-hidden="true" />
                  Строка
                </Button>
              </div>

              <div className="grid grid-cols-[1fr_46px_36px_64px_1fr_28px] gap-1.5 px-1 text-xs text-muted-foreground">
                <span>Товар</span>
                <span className="text-center">Кол-во</span>
                <span className="text-center">Ед.</span>
                <span className="text-right">Цена</span>
                <span>Статья ДДС</span>
                <span aria-hidden="true" />
              </div>

              {lines.map((line) => (
                <div
                  key={line.key}
                  className="grid grid-cols-[1fr_46px_36px_64px_1fr_28px] items-center gap-1.5"
                >
                  <ProductSearch
                    value={line.name}
                    products={products}
                    placeholder="Наименование"
                    onPick={(product) =>
                      updateLine(line.key, {
                        name: product.name,
                        productId: product.id,
                        unit: product.unit,
                      })
                    }
                    onTextChange={(text) =>
                      updateLine(line.key, { name: text, productId: null, unit: null })
                    }
                  />
                  <Input
                    className="h-9 px-1 text-center"
                    inputMode="decimal"
                    aria-label="Количество"
                    value={line.quantity}
                    onChange={(event) => updateLine(line.key, { quantity: event.target.value })}
                  />
                  <span className="text-center text-xs text-muted-foreground">{line.unit ?? "—"}</span>
                  <Input
                    className="h-9 px-1 text-right"
                    inputMode="decimal"
                    aria-label="Цена"
                    value={line.price}
                    onChange={(event) => updateLine(line.key, { price: event.target.value })}
                  />
                  <Select
                    value={line.articleId}
                    onValueChange={(value) => updateLine(line.key, { articleId: value })}
                  >
                    <SelectTrigger className="h-9" aria-label="Статья ДДС">
                      <SelectValue placeholder="Статья" />
                    </SelectTrigger>
                    <SelectContent>
                      {articles.map((article) => (
                        <SelectItem key={article.id} value={article.id}>
                          {article.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <Button
                    size="icon"
                    variant="ghost"
                    className="size-9"
                    aria-label="Удалить позицию"
                    onClick={() =>
                      setLines((prev) =>
                        prev.length > 1 ? prev.filter((l) => l.key !== line.key) : prev,
                      )
                    }
                  >
                    <Trash2 size={14} aria-hidden="true" />
                  </Button>
                </div>
              ))}

              {!totalsMatch && filledLines.length > 0 ? (
                <p className="text-xs text-destructive">
                  Сумма позиций {formatDdsMoney(linesTotal)} ≠ оплате {formatDdsMoney(paidTotal)}.
                </p>
              ) : null}
            </div>

            <Input
              placeholder="Комментарий (необязательно)"
              value={comment}
              onChange={(event) => setComment(event.target.value)}
            />
          </div>

          <DialogFooter className="border-t border-dashed pt-3 sm:justify-between sm:gap-3">
            <div className="text-sm tabular-nums">
              <span className="text-muted-foreground">Итого </span>
              <span className="font-medium">{formatDdsMoney(paidTotal)}</span>
              {cash > 0 ? (
                <span className="ml-1 text-xs text-muted-foreground">
                  (карта {formatDdsMoney(cardTotal)} · нал {formatDdsMoney(cash)})
                </span>
              ) : null}
            </div>
            <Button disabled={!canSave} onClick={() => createMutation.mutate()}>
              {createMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : null}
              Создать чек
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <OperationPicker
        open={pickerOpen}
        onOpenChange={setPickerOpen}
        ops={cardOps}
        isLoading={cardTxQuery.isLoading}
        day={dayLabel(issuedAt)}
        selectedId={selectedOp?.bank_operation_id ?? null}
        onPick={(op) => {
          setSelectedOp(op);
          setPickerOpen(false);
        }}
      />
    </>
  );
}

function OperationPicker({
  open,
  onOpenChange,
  ops,
  isLoading,
  day,
  selectedId,
  onPick,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  ops: CardTransaction[];
  isLoading: boolean;
  day: string;
  selectedId: string | null;
  onPick: (op: CardTransaction) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[80vh] max-w-md overflow-y-auto">
        <DialogHeader className="space-y-1">
          <DialogTitle>Оплаты по карте за {day}</DialogTitle>
          <DialogDescription>Выберите одну оплату.</DialogDescription>
        </DialogHeader>
        <div className="space-y-1">
          {isLoading ? (
            <div className="rounded-md border border-dashed px-3 py-6 text-center text-sm text-muted-foreground">
              Загрузка операций…
            </div>
          ) : ops.length === 0 ? (
            <div className="rounded-md border border-dashed px-3 py-6 text-center text-sm text-muted-foreground">
              Нет оплат по карте за {day}.
            </div>
          ) : (
            ops.map((op) => {
              const checked = op.bank_operation_id === selectedId;
              return (
                <button
                  type="button"
                  key={op.bank_operation_id}
                  onClick={() => onPick(op)}
                  className={cn(
                    "flex w-full items-center gap-3 rounded-md border px-3 py-2.5 text-left transition-colors",
                    checked
                      ? "border-emerald-300 bg-emerald-50"
                      : "border-transparent hover:bg-muted/60",
                  )}
                >
                  <span
                    className={cn(
                      "flex size-5 shrink-0 items-center justify-center rounded-full border",
                      checked
                        ? "border-emerald-500 bg-emerald-500 text-white"
                        : "border-muted-foreground/40",
                    )}
                  >
                    {checked ? <Check size={13} aria-hidden="true" /> : null}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex items-baseline justify-between gap-2">
                      <span className="truncate text-sm">
                        {op.counterparty_name_raw ?? "Покупка по карте"}
                      </span>
                      <span className="shrink-0 text-sm font-medium tabular-nums">
                        {formatDdsMoney(op.amount)}
                      </span>
                    </span>
                    <span className="block truncate text-xs text-muted-foreground">
                      {formatDateTime(op.purchased_at ?? op.posted_at ?? op.operation_date)}
                    </span>
                  </span>
                </button>
              );
            })
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
