import { useEffect, useMemo, useState } from "react";
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  Check,
  ChevronRight,
  LoaderCircle,
  Plus,
  Receipt,
  Trash2,
  Undo2,
  Warehouse,
  X,
} from "lucide-react";
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
  getKassaConfig,
  getKassaCounterparties,
  getKassaExpenseArticles,
  type CardTransaction,
  type ChequeLinePayload,
} from "@/routes/kassa/api";
import { getProducts } from "@/routes/warehouse/api";
import { ProductSearch } from "@/routes/warehouse/ProductSearch";

const LOCAL_PURCHASE_NAME = "Местный закуп";
// Единственная складская статья — товары уходят в iiko. Остальные = просто расход ДДС.
const SUPPLIER_ARTICLE_NAME = "Оплата поставщикам";

type StoreLine = {
  key: string;
  name: string;
  productId: string | null;
  unit: string | null;
  quantity: string;
  price: string;
  // Сумма строки — UI-механизм взаиморасчёта (как в накладных): на бэк не уходит,
  // там sum = quantity*price; при вводе суммы пересчитывается цена.
  amount: string;
  // Позиция возвращена в магазин: остаётся в чеке (сверка с бумажным чеком копейка в
  // копейку), помечается красным и не проводится — чек проводится net.
  isReturn: boolean;
};
type ExpenseLine = { key: string; articleId: string; amount: string; note: string; isReturn: boolean };

let seq = 0;
function emptyStoreLine(): StoreLine {
  seq += 1;
  return {
    key: `s${seq}`,
    name: "",
    productId: null,
    unit: null,
    quantity: "1",
    price: "",
    amount: "",
    isReturn: false,
  };
}
function emptyExpenseLine(): ExpenseLine {
  seq += 1;
  return { key: `e${seq}`, articleId: "", amount: "", note: "", isReturn: false };
}

function dayLabel(issuedAt: string): string {
  const [date] = issuedAt.split("T");
  const [y, m, d] = (date ?? "").split("-");
  return d && m && y ? `${d}.${m}.${y}` : "—";
}

// Запятая — штатный десятичный разделитель русской раскладки: без неё `Number("0,5")` даёт NaN,
// строка выпадает из фильтров и сумма не пересчитывается из цены (тот же дефект был в накладных).
const num = (value: string) =>
  Number(String(value).replace(/[\s ]/g, "").replace(",", ".")) || 0;
const toAmount = (n: number) => (n > 0 ? String(Math.round(n * 100) / 100) : "");

type CreateChequeDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: () => void;
};

export function CreateChequeDialog({ open, onOpenChange, onCreated }: CreateChequeDialogProps) {
  const queryClient = useQueryClient();
  const [issuedAt, setIssuedAt] = useState(() => toDateTimeLocalInput(new Date()));
  const [selectedOp, setSelectedOp] = useState<CardTransaction | null>(null);
  // Ручной ввод суммы чека, когда банк ещё не передал card-операцию. Взаимоисключающе с
  // selectedOp: чек «ожидает подтверждения банком», операция подтянется и сматчится позже.
  const [pendingAmount, setPendingAmount] = useState<number | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [cashAmount, setCashAmount] = useState("");
  const [storeLines, setStoreLines] = useState<StoreLine[]>([]);
  const [expenseLines, setExpenseLines] = useState<ExpenseLine[]>([emptyExpenseLine()]);
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
    staleTime: 10_000,
    // «Живой» список: webhook кладёт карт-оплату в bank_operations в реальном времени,
    // поэтому, пока диалог открыт, перезапрашиваем раз в 15с — оплата, прилетевшая по
    // webhook, появляется без переоткрытия окна. Выбор кассира при этом не сбрасывается
    // (guard !cardTxQuery.isFetching в useEffect ниже), keepPreviousData убирает мигание.
    refetchInterval: 15_000,
    refetchOnWindowFocus: true,
  });
  const configQuery = useQuery({
    queryKey: ["kassa", "config"],
    queryFn: () => getKassaConfig(),
    enabled: open,
    staleTime: 60_000,
  });
  // Блок ручного ввода суммы чека показываем, только если включён флаг в Настройках → Касса.
  // По умолчанию выключен: карт-операции приходят вебхуком, ручной pending — лишь страховка.
  const manualPendingEnabled = configQuery.data?.manual_pending_cheque_enabled ?? false;
  const cardOps = useMemo(() => cardTxQuery.data ?? [], [cardTxQuery.data]);
  const products = productsQuery.data ?? [];
  const articles = articlesQuery.data ?? [];

  const localPurchase = useMemo(
    () => (counterpartiesQuery.data ?? []).find((cp) => cp.name === LOCAL_PURCHASE_NAME),
    [counterpartiesQuery.data],
  );
  const supplierArticle = useMemo(
    () => articles.find((a) => a.name === SUPPLIER_ARTICLE_NAME),
    [articles],
  );
  // Прочие расходы — все статьи, кроме складской «Оплата поставщикам».
  const expenseArticles = useMemo(
    () => articles.filter((a) => a.name !== SUPPLIER_ARTICLE_NAME),
    [articles],
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

  const isPending = !selectedOp && pendingAmount !== null && pendingAmount > 0;
  const storeTotal = useMemo(
    () =>
      storeLines.reduce(
        (sum, l) => sum + (l.amount !== "" ? num(l.amount) : num(l.quantity) * num(l.price)),
        0,
      ),
    [storeLines],
  );
  const expenseTotal = useMemo(
    () => expenseLines.reduce((sum, l) => sum + num(l.amount), 0),
    [expenseLines],
  );
  // Gross — все позиции как на бумажном чеке (включая возвращённые); net — к проведению.
  const linesTotal = storeTotal + expenseTotal;
  const returnedTotal = useMemo(
    () =>
      storeLines.reduce(
        (sum, l) =>
          sum + (l.isReturn ? (l.amount !== "" ? num(l.amount) : num(l.quantity) * num(l.price)) : 0),
        0,
      ) + expenseLines.reduce((sum, l) => sum + (l.isReturn ? num(l.amount) : 0), 0),
    [storeLines, expenseLines],
  );
  const hasReturns = returnedTotal > 0;
  // С возвратами карт-часть проводится net: операция − возвраты (сервер считает так же).
  const cardTotal = selectedOp
    ? hasReturns
      ? Math.max(selectedOp.amount - returnedTotal, 0)
      : selectedOp.amount
    : isPending
      ? (pendingAmount ?? 0)
      : 0;
  const cash = num(cashAmount);
  const paidTotal = cardTotal + cash;
  // Card-scoped подсказка: на карте выбранной покупки есть непривязанный возврат в выписке
  // (надёжной привязки к конкретной покупке банк не даёт — кассир решает сам). Точную
  // сверку по сумме делает матчер после создания чека.
  const bankRefund = selectedOp?.refund_amount ?? null;
  const refundHint =
    selectedOp && bankRefund && !hasReturns
      ? `На этой карте есть непривязанный возврат ${formatDdsMoney(bankRefund)} — если он по этой покупке, отметьте возвращённые позиции кнопкой ↩`
      : null;

  function reset() {
    setIssuedAt(toDateTimeLocalInput(new Date()));
    setSelectedOp(null);
    setPendingAmount(null);
    setCashAmount("");
    setStoreLines([]);
    setExpenseLines([emptyExpenseLine()]);
    setComment("");
  }

  const createMutation = useMutation({
    mutationFn: () => {
      const storePayload: ChequeLinePayload[] = storeLines
        .filter((l) => l.name.trim() && num(l.quantity) > 0)
        .map((l) => ({
          name: l.name.trim(),
          quantity: num(l.quantity),
          unit: l.unit || null,
          price: num(l.price),
          // Сумма строки — та же, что в подытоге (введённая или кол-во×цена): бэк проводит
          // именно её, иначе построчный пересчёт qty×price расходится с оплатой на копейки.
          amount: l.amount !== "" ? num(l.amount) : num(l.quantity) * num(l.price),
          dds_article_id: supplierArticle?.id ?? null,
          iiko_product_id: l.productId,
          is_return: l.isReturn,
        }));
      const expensePayload: ChequeLinePayload[] = expenseLines
        .filter((l) => l.articleId && num(l.amount) > 0)
        .map((l) => ({
          name: l.note.trim() || articles.find((a) => a.id === l.articleId)?.name || "Расход",
          quantity: 1,
          unit: null,
          price: num(l.amount),
          amount: num(l.amount),
          dds_article_id: l.articleId,
          iiko_product_id: null,
          is_return: l.isReturn,
        }));
      return createCheque({
        counterparty_id: localPurchase?.id ?? "",
        issued_at: issuedAt,
        bank_parts: selectedOp ? [{ bank_operation_id: selectedOp.bank_operation_id }] : [],
        cash_amount: cash > 0 ? cash : null,
        pending_card_amount: isPending ? pendingAmount : null,
        track_nomenclature: true,
        lines: [...storePayload, ...expensePayload],
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
      toast.success(
        isPending
          ? "Чек создан — ожидает подтверждения банком"
          : "Чек создан и проведён в ДДС",
      );
      reset();
      onCreated();
      onOpenChange(false);
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось создать чек")),
  });

  const storeValid = storeLines.every(
    (l) => !l.name.trim() || (num(l.quantity) > 0 && num(l.price) > 0),
  );
  // Складская строка с названием, но без выбора из номенклатуры iiko — блокируем сохранение
  // (зеркало серверного гарда create_cheque): иначе позиция молча теряется при выгрузке чека в
  // iiko приходной накладной, и приход уходит на сумму меньше чека. Товара нет в номенклатуре →
  // «Синхронизировать номенклатуру» либо в блок «Прочие расходы» (без склада).
  const goodsMissingProduct = storeLines.some((l) => l.name.trim() && !l.productId);
  const expenseValid = expenseLines.every((l) => !l.articleId || num(l.amount) > 0);
  const filledCount =
    storeLines.filter((l) => l.name.trim() && num(l.quantity) > 0).length +
    expenseLines.filter((l) => l.articleId && num(l.amount) > 0).length;
  // С возвратами сверка «как на бумаге»: ВСЕ позиции (включая возвращённые) == сумме
  // операции копейка в копейку; net при этом равен оплате автоматически. Без возвратов —
  // прежняя сверка позиций с оплатой.
  const totalsMatch = hasReturns
    ? Boolean(selectedOp) && Math.abs(linesTotal - (selectedOp?.amount ?? 0)) < 0.01
    : Math.abs(linesTotal - paidTotal) < 0.01;
  // Возврат — только при одной выбранной карт-операции, без наличных и ручного пендинга
  // (зеркало серверных гардов create_cheque).
  const returnsValid =
    !hasReturns ||
    (Boolean(selectedOp) && cash === 0 && returnedTotal < (selectedOp?.amount ?? 0) - 0.005);
  const canSave =
    Boolean(localPurchase) &&
    Boolean(issuedAt) &&
    paidTotal > 0 &&
    filledCount > 0 &&
    storeValid &&
    !goodsMissingProduct &&
    expenseValid &&
    totalsMatch &&
    returnsValid &&
    !createMutation.isPending;

  // Человеко-понятная причина, почему чек нельзя создать (иначе кнопка гаснет молча).
  const disabledReason = createMutation.isPending
    ? null
    : !localPurchase
      ? counterpartiesQuery.isLoading
        ? "Загружаем контрагентов…"
        : "Контрагент «Местный закуп» недоступен — обратитесь к управляющему"
      : !issuedAt
        ? "Укажите дату и время чека"
        : filledCount === 0
          ? "Добавьте хотя бы одну позицию"
          : !storeValid
            ? "У товарных строк укажите количество и цену"
            : goodsMissingProduct
              ? "У товарных строк выберите товар из номенклатуры iiko (или перенесите в «Прочие расходы»)"
              : !expenseValid
                ? "У строк прочих расходов укажите сумму"
                : hasReturns && !selectedOp
                  ? "Возврат позиций — только при выбранной карт-операции"
                  : hasReturns && cash > 0
                    ? "Возврат не сочетается с наличной частью"
                    : hasReturns && returnedTotal >= (selectedOp?.amount ?? 0)
                      ? "Возвраты не могут покрывать всю операцию"
                      : paidTotal <= 0
                        ? "Укажите оплату — карт-операцию или наличные"
                        : !totalsMatch
                          ? hasReturns
                            ? "Позиции (с возвратами) должны сойтись с суммой операции"
                            : "Суммы позиций и оплаты не сходятся"
                          : null;

  function patchStore(key: string, patch: Partial<StoreLine>) {
    setStoreLines((prev) => prev.map((l) => (l.key === key ? { ...l, ...patch } : l)));
  }
  function patchExpense(key: string, patch: Partial<ExpenseLine>) {
    setExpenseLines((prev) => prev.map((l) => (l.key === key ? { ...l, ...patch } : l)));
  }

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
          <DialogHeader className="space-y-1">
            <DialogTitle>Новый чек — местный закуп</DialogTitle>
            <DialogDescription>
              Оплата по бизнес-карте. Сумма разносится по статьям ДДС.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4">
            <Input
              type="datetime-local"
              aria-label="Дата и время покупки"
              value={issuedAt}
              onChange={(event) => setIssuedAt(event.target.value)}
            />

            <div
              className={cn(
                "flex w-full items-center rounded-md border transition-colors",
                selectedOp
                  ? "border-emerald-300 bg-emerald-50"
                  : isPending
                    ? "border-amber-300 bg-amber-50"
                    : "border-input",
              )}
            >
              <button
                type="button"
                onClick={() => setPickerOpen(true)}
                className="flex min-w-0 flex-1 items-center gap-3 rounded-l-md px-3 py-2.5 text-left transition-colors hover:bg-muted/60"
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
                      {hasReturns ? (
                        <span className="block truncate text-xs text-red-700">
                          возврат {formatDdsMoney(returnedTotal)} · к проведению{" "}
                          {formatDdsMoney(cardTotal)}
                        </span>
                      ) : null}
                    </>
                  ) : isPending ? (
                    <>
                      <span className="flex items-baseline justify-between gap-2">
                        <span className="truncate text-sm text-amber-800">
                          Сумма введена вручную
                        </span>
                        <span className="shrink-0 text-sm font-medium tabular-nums text-amber-800">
                          {formatDdsMoney(pendingAmount ?? 0)}
                        </span>
                      </span>
                      <span className="block truncate text-xs text-amber-700">
                        Ожидает подтверждения банком · сматчится с операцией позже
                      </span>
                    </>
                  ) : (
                    <span className="text-sm text-muted-foreground">
                      Операция не выбрана — выберите оплату по карте
                    </span>
                  )}
                </span>
                <ChevronRight
                  size={16}
                  className="shrink-0 text-muted-foreground"
                  aria-hidden="true"
                />
              </button>
              {selectedOp || isPending ? (
                <Button
                  type="button"
                  size="icon"
                  variant="ghost"
                  className="mr-1 size-8 shrink-0 text-muted-foreground hover:text-foreground"
                  aria-label="Сбросить оплату по карте"
                  onClick={() => {
                    setSelectedOp(null);
                    setPendingAmount(null);
                  }}
                >
                  <X size={15} aria-hidden="true" />
                </Button>
              ) : null}
            </div>

            {refundHint ? (
              <p className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                {refundHint}
              </p>
            ) : null}

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

            {/* Секция 1 — закупка на склад (поставщики, уходит в iiko) */}
            <div className="rounded-md border p-3">
              <div className="flex items-center justify-between">
                <span className="flex items-center gap-2 text-sm font-medium">
                  <Warehouse size={16} className="text-sky-600" aria-hidden="true" />
                  Закупка на склад
                </span>
                <span className="rounded bg-sky-50 px-2 py-0.5 text-xs text-sky-700">
                  → iiko · Оплата поставщикам
                </span>
              </div>
              <p className="mt-0.5 text-xs text-muted-foreground">
                Товары из номенклатуры iiko — уходят на склад.
              </p>

              {storeLines.length > 0 ? (
                <div className="mt-2 space-y-1.5">
                  <div className="grid grid-cols-[1fr_44px_26px_56px_64px_24px_24px] gap-1.5 px-1 text-xs text-muted-foreground">
                    <span>Товар</span>
                    <span className="text-center">Кол-во</span>
                    <span className="text-center">Ед.</span>
                    <span className="text-right">Цена</span>
                    <span className="text-right">Сумма</span>
                    <span />
                    <span />
                  </div>
                  {storeLines.map((line) => (
                    <div key={line.key}>
                    <div
                      className={cn(
                        "grid grid-cols-[1fr_44px_26px_56px_64px_24px_24px] items-center gap-1.5",
                        line.isReturn && "rounded-md bg-red-50 ring-1 ring-red-200",
                        // Товар не выбран из номенклатуры — та же подсветка, что в накладной.
                        line.name.trim() && !line.productId && "rounded-md p-1 ring-1 ring-red-300",
                      )}
                    >
                      <ProductSearch
                        value={line.name}
                        products={products}
                        placeholder="Товар"
                        onPick={(p) =>
                          patchStore(line.key, { name: p.name, productId: p.id, unit: p.unit })
                        }
                        onTextChange={(text) =>
                          patchStore(line.key, { name: text, productId: null, unit: null })
                        }
                      />
                      <Input
                        className="h-9 px-1 text-center"
                        inputMode="decimal"
                        aria-label="Количество"
                        value={line.quantity}
                        onChange={(e) =>
                          patchStore(line.key, {
                            quantity: e.target.value,
                            amount: toAmount(num(e.target.value) * num(line.price)),
                          })
                        }
                      />
                      <span className="text-center text-xs text-muted-foreground">
                        {line.unit ?? "—"}
                      </span>
                      <Input
                        className="h-9 px-1 text-right"
                        inputMode="decimal"
                        aria-label="Цена"
                        value={line.price}
                        onChange={(e) =>
                          patchStore(line.key, {
                            price: e.target.value,
                            amount: toAmount(num(e.target.value) * num(line.quantity)),
                          })
                        }
                      />
                      <Input
                        className="h-9 px-1 text-right"
                        inputMode="decimal"
                        aria-label="Сумма"
                        title="Сумма строки — цена × кол-во; при вводе суммы цена пересчитывается"
                        value={line.amount}
                        onChange={(e) => {
                          const q = num(line.quantity);
                          patchStore(line.key, {
                            amount: e.target.value,
                            price: q > 0 ? toAmount(num(e.target.value) / q) : line.price,
                          });
                        }}
                      />
                      <Button
                        size="icon"
                        variant="ghost"
                        className={cn(
                          "size-9",
                          line.isReturn
                            ? "text-red-600 hover:text-red-700"
                            : "text-muted-foreground",
                        )}
                        aria-label={
                          line.isReturn ? "Снять пометку возврата" : "Пометить как возврат"
                        }
                        title="Позиция возвращена в магазин — останется в чеке, но не будет проведена"
                        onClick={() => patchStore(line.key, { isReturn: !line.isReturn })}
                      >
                        <Undo2 size={14} aria-hidden="true" />
                      </Button>
                      <Button
                        size="icon"
                        variant="ghost"
                        className="size-9"
                        aria-label="Удалить товар"
                        onClick={() =>
                          setStoreLines((prev) => prev.filter((l) => l.key !== line.key))
                        }
                      >
                        <Trash2 size={14} aria-hidden="true" />
                      </Button>
                    </div>
                    {line.name.trim() && !line.productId ? (
                      <p className="px-1 text-xs text-red-600">
                        Выберите конкретный товар из номенклатуры iiko
                      </p>
                    ) : null}
                    </div>
                  ))}
                </div>
              ) : null}

              <div className="mt-2 flex items-center justify-between">
                <Button
                  size="sm"
                  variant="ghost"
                  className="text-muted-foreground"
                  onClick={() => setStoreLines((prev) => [...prev, emptyStoreLine()])}
                >
                  <Plus size={14} aria-hidden="true" />
                  товар
                </Button>
                {storeTotal > 0 ? (
                  <span className="text-xs text-muted-foreground">
                    подытог <span className="font-medium text-foreground">{formatDdsMoney(storeTotal)}</span>
                  </span>
                ) : null}
              </div>
            </div>

            {/* Секция 2 — прочие расходы (без склада, только ДДС) */}
            <div className="rounded-md border p-3">
              <div className="flex items-center justify-between">
                <span className="flex items-center gap-2 text-sm font-medium">
                  <Receipt size={16} className="text-muted-foreground" aria-hidden="true" />
                  Прочие расходы
                </span>
                <span className="rounded bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                  только ДДС · без склада
                </span>
              </div>
              <p className="mt-0.5 text-xs text-muted-foreground">
                Статья + сумма, без номенклатуры (моющие, питание персонала и т.п.).
              </p>

              {expenseLines.length > 0 ? (
                <div className="mt-2 space-y-1.5">
                  {expenseLines.map((line) => (
                    <div
                      key={line.key}
                      className={cn(
                        "grid grid-cols-[1.1fr_1fr_74px_28px_28px] items-center gap-1.5",
                        line.isReturn && "rounded-md bg-red-50 ring-1 ring-red-200",
                      )}
                    >
                      <Select
                        value={line.articleId}
                        onValueChange={(value) => patchExpense(line.key, { articleId: value })}
                      >
                        <SelectTrigger className="h-9" aria-label="Статья ДДС">
                          <SelectValue placeholder="Статья" />
                        </SelectTrigger>
                        <SelectContent>
                          {expenseArticles.map((article) => (
                            <SelectItem key={article.id} value={article.id}>
                              {article.name}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                      <Input
                        className="h-9"
                        placeholder="Подпись (напр. губки)"
                        value={line.note}
                        onChange={(e) => patchExpense(line.key, { note: e.target.value })}
                      />
                      <Input
                        className="h-9 px-1 text-right"
                        inputMode="decimal"
                        aria-label="Сумма"
                        placeholder="Сумма"
                        value={line.amount}
                        onChange={(e) => patchExpense(line.key, { amount: e.target.value })}
                      />
                      <Button
                        size="icon"
                        variant="ghost"
                        className={cn(
                          "size-9",
                          line.isReturn
                            ? "text-red-600 hover:text-red-700"
                            : "text-muted-foreground",
                        )}
                        aria-label={
                          line.isReturn ? "Снять пометку возврата" : "Пометить как возврат"
                        }
                        title="Позиция возвращена в магазин — останется в чеке, но не будет проведена"
                        onClick={() => patchExpense(line.key, { isReturn: !line.isReturn })}
                      >
                        <Undo2 size={14} aria-hidden="true" />
                      </Button>
                      <Button
                        size="icon"
                        variant="ghost"
                        className="size-9"
                        aria-label="Удалить расход"
                        onClick={() =>
                          setExpenseLines((prev) => prev.filter((l) => l.key !== line.key))
                        }
                      >
                        <Trash2 size={14} aria-hidden="true" />
                      </Button>
                    </div>
                  ))}
                </div>
              ) : null}

              <div className="mt-2 flex items-center justify-between">
                <Button
                  size="sm"
                  variant="ghost"
                  className="text-muted-foreground"
                  onClick={() => setExpenseLines((prev) => [...prev, emptyExpenseLine()])}
                >
                  <Plus size={14} aria-hidden="true" />
                  расход
                </Button>
                {expenseTotal > 0 ? (
                  <span className="text-xs text-muted-foreground">
                    подытог{" "}
                    <span className="font-medium text-foreground">{formatDdsMoney(expenseTotal)}</span>
                  </span>
                ) : null}
              </div>
            </div>

            <Input
              placeholder="Комментарий к чеку (необязательно)"
              value={comment}
              onChange={(event) => setComment(event.target.value)}
            />
          </div>

          <DialogFooter className="border-t border-dashed pt-3 sm:justify-between sm:gap-3">
            <div className="text-sm tabular-nums">
              <span className="text-muted-foreground">Итого </span>
              <span className="font-medium">{formatDdsMoney(linesTotal)}</span>
              {hasReturns ? (
                <>
                  <span className="ml-1 text-xs text-red-600">
                    − возврат {formatDdsMoney(returnedTotal)}
                  </span>
                  <span className="ml-1 text-xs text-muted-foreground">
                    = {formatDdsMoney(Math.max(linesTotal - returnedTotal, 0))} к проведению
                  </span>
                  {!totalsMatch && selectedOp ? (
                    <span className="ml-1 text-xs text-destructive">
                      ≠ операции {formatDdsMoney(selectedOp.amount)}
                    </span>
                  ) : null}
                </>
              ) : !totalsMatch && paidTotal > 0 ? (
                <span className="ml-1 text-xs text-destructive">≠ оплате {formatDdsMoney(paidTotal)}</span>
              ) : (
                <span className="ml-1 text-xs text-muted-foreground">из {formatDdsMoney(paidTotal)}</span>
              )}
            </div>
            <div className="flex flex-col items-end gap-1">
              {disabledReason ? (
                <span className="text-xs text-amber-600">{disabledReason}</span>
              ) : null}
              <Button disabled={!canSave} onClick={() => createMutation.mutate()}>
                {createMutation.isPending ? (
                  <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                ) : null}
                Создать чек
              </Button>
            </div>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <OperationPicker
        open={pickerOpen}
        onOpenChange={setPickerOpen}
        ops={cardOps}
        isLoading={cardTxQuery.isLoading}
        manualPendingEnabled={manualPendingEnabled}
        day={dayLabel(issuedAt)}
        selectedId={selectedOp?.bank_operation_id ?? null}
        pendingAmount={isPending ? pendingAmount : null}
        onPick={(op) => {
          setSelectedOp(op);
          setPendingAmount(null);
          setPickerOpen(false);
        }}
        onClear={() => {
          setSelectedOp(null);
          setPendingAmount(null);
          setPickerOpen(false);
        }}
        onManual={(amount) => {
          setPendingAmount(amount);
          setSelectedOp(null);
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
  manualPendingEnabled,
  day,
  selectedId,
  pendingAmount,
  onPick,
  onClear,
  onManual,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  ops: CardTransaction[];
  isLoading: boolean;
  manualPendingEnabled: boolean;
  day: string;
  selectedId: string | null;
  pendingAmount: number | null;
  onPick: (op: CardTransaction) => void;
  onClear: () => void;
  onManual: (amount: number) => void;
}) {
  const [manualInput, setManualInput] = useState("");
  useEffect(() => {
    if (open) setManualInput(pendingAmount != null ? String(pendingAmount) : "");
  }, [open, pendingAmount]);
  const noCardSelected = selectedId === null && pendingAmount === null;
  const manualValue = Number(manualInput) || 0;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[80vh] max-w-md overflow-y-auto">
        <DialogHeader className="space-y-1">
          <DialogTitle>Оплаты по карте за {day}</DialogTitle>
          <DialogDescription>Выберите оплату или «без карты» — для чисто наличного чека.</DialogDescription>
        </DialogHeader>
        <div className="space-y-1">
          <button
            type="button"
            onClick={onClear}
            className={cn(
              "flex w-full items-center gap-3 rounded-md border px-3 py-2.5 text-left transition-colors",
              noCardSelected
                ? "border-emerald-300 bg-emerald-50"
                : "border-transparent hover:bg-muted/60",
            )}
          >
            <span
              className={cn(
                "flex size-5 shrink-0 items-center justify-center rounded-full border",
                noCardSelected
                  ? "border-emerald-500 bg-emerald-500 text-white"
                  : "border-muted-foreground/40",
              )}
            >
              {noCardSelected ? <Check size={13} aria-hidden="true" /> : null}
            </span>
            <span className="text-sm">
              Без оплаты картой <span className="text-muted-foreground">— только наличные</span>
            </span>
          </button>
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
                    {op.refund_amount ? (
                      <span className="block truncate text-xs text-amber-700">
                        на карте есть возврат {formatDdsMoney(op.refund_amount)} — если по этой
                        покупке, введите чек полностью и отметьте позиции
                      </span>
                    ) : null}
                  </span>
                </button>
              );
            })
          )}

          {manualPendingEnabled && (
            <div className="mt-2 rounded-md border border-dashed border-amber-300 bg-amber-50/40 p-3">
              <p className="text-sm font-medium text-amber-900">Добавить сумму чека вручную</p>
              <p className="mt-0.5 text-xs text-amber-700">
                Банк ещё не показал оплату (выходные / задержка). Введите сумму — чек будет
                «ожидать подтверждения банком» и сматчится с операцией позже.
              </p>
              <div className="mt-2 flex items-center gap-2">
                <Input
                  inputMode="decimal"
                  placeholder="Введите точную сумму до копейки"
                  className="h-9"
                  value={manualInput}
                  onChange={(event) => setManualInput(event.target.value)}
                />
                <Button
                  type="button"
                  size="sm"
                  disabled={manualValue <= 0}
                  onClick={() => onManual(Math.round(manualValue * 100) / 100)}
                >
                  Указать
                </Button>
              </div>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
