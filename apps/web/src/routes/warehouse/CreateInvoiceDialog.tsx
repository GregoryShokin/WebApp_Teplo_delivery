import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Plus, Trash2, Users, Warehouse } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
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
import { apiErrorMessage, getDdsBankOperations, getDdsWallets } from "@/lib/api";
import { cn } from "@/lib/utils";

import { getRegistry, type RegistryItem } from "../counterparties/api";
import { formatRub } from "../counterparties/shared";
import { ProductSearch } from "./ProductSearch";
import {
  createBarterReturn,
  createWarehouseInvoice,
  getLoanReturnable,
  getNextInvoiceNumber,
  getOpenLoans,
  getProductPriceStats,
  getProducts,
  getStaffArticles,
  settleLoanWithMoney,
  type StaffArticle,
  type WarehouseProduct,
} from "./api";

export type DraftLine = {
  key: string;
  product_id: string | null;
  name: string;
  unit: string | null;
  quantity: string;
  price: string;
  vat: string;
  amount: string;
};

export type StaffLine = { key: string; articleId: string; note: string; amount: string };

export function emptyLine(): DraftLine {
  return {
    key: Math.random().toString(36).slice(2),
    product_id: null,
    name: "",
    unit: null,
    quantity: "",
    price: "",
    vat: "",
    amount: "",
  };
}

export function emptyStaffLine(): StaffLine {
  return { key: Math.random().toString(36).slice(2), articleId: "", note: "", amount: "" };
}

export const num = (v: string) => Math.max(0, Number(v) || 0);
// Число → строка суммы/цены без хвостовых нулей (пустая строка для нуля).
const toAmount = (n: number) => (n > 0 ? String(Math.round(n * 100) / 100) : "");

export function CreateInvoiceDialog({
  open,
  onOpenChange,
  onCreated,
  barter = false,
  kassaOnly = false,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onCreated: () => void;
  barter?: boolean;
  // В контуре Кассы дропдаун контрагентов показывает только «Активных в Кассе».
  kassaOnly?: boolean;
}) {
  const [kind, setKind] = useState<"normal" | "barter">(barter ? "barter" : "normal");
  const [weLend, setWeLend] = useState(true);
  const [barterAction, setBarterAction] = useState<"loan" | "return">("loan");
  const [counterpartyId, setCounterpartyId] = useState("");
  const [issuedAt, setIssuedAt] = useState("");
  const [number, setNumber] = useState("");
  const [markPaid, setMarkPaid] = useState(false);
  const [paidAmount, setPaidAmount] = useState("");
  const [unpaidConfirmOpen, setUnpaidConfirmOpen] = useState(false);
  const [lines, setLines] = useState<DraftLine[]>([emptyLine()]);
  const [staffLines, setStaffLines] = useState<StaffLine[]>([]);
  const queryClient = useQueryClient();
  const isBarter = kind === "barter";
  const isReturn = isBarter && barterAction === "return";

  const registryQuery = useQuery({
    queryKey: ["cp", "registry", kassaOnly ? "kassa" : "all"],
    queryFn: () => getRegistry(kassaOnly ? { kassa_only: true } : undefined),
    enabled: open,
  });
  const selectedCp = registryQuery.data?.find((i) => i.counterparty_id === counterpartyId);
  const cpHasGuid = selectedCp?.has_iiko_guid ?? false;
  // Бартер: контрагент только из бартерных партнёров; обычная накладная — все поставщики.
  const counterpartyOptions = isBarter
    ? (registryQuery.data ?? []).filter((i) => i.relationship === "barter")
    : registryQuery.data ?? [];
  // Все GOODS загружаем разом и фильтруем на клиенте — как поиск контрагентов.
  const productsQuery = useQuery({
    queryKey: ["wh", "products", "goods-all"],
    queryFn: () => getProducts({ type: "GOODS", limit: 2000 }),
    enabled: open,
  });
  const nextNumberQuery = useQuery({
    queryKey: ["wh", "next-number"],
    queryFn: getNextInvoiceNumber,
    enabled: open,
  });
  const staffArticlesQuery = useQuery({
    queryKey: ["wh", "staff-articles"],
    queryFn: getStaffArticles,
    enabled: open,
    staleTime: 60_000,
  });
  const staffArticles = staffArticlesQuery.data ?? [];

  useEffect(() => {
    if (open && !number && nextNumberQuery.data) {
      setNumber(nextNumberQuery.data);
    }
  }, [open, number, nextNumberQuery.data]);

  // Инбокс задаёт лишь значение по умолчанию; внутри окна можно переключить.
  useEffect(() => {
    if (open) setKind(barter ? "barter" : "normal");
  }, [open, barter]);

  const reset = () => {
    setKind(barter ? "barter" : "normal");
    setWeLend(true);
    setBarterAction("loan");
    setCounterpartyId("");
    setIssuedAt("");
    setNumber("");
    setLines([emptyLine()]);
    setStaffLines([]);
    setMarkPaid(false);
    setPaidAmount("");
  };

  const createMutation = useMutation({
    mutationFn: () =>
      createWarehouseInvoice({
        counterparty_id: counterpartyId,
        issued_at: issuedAt,
        mode: isBarter ? "loan" : "normal",
        we_lend: weLend,
        number: number || null,
        via_kassa: kassaOnly,
        ...(kassaOnly && markPaid && !isBarter
          ? { mark_paid: true, paid_amount: paidAmount ? num(paidAmount) : null }
          : {}),
        lines: [
          ...lines
            .filter((l) => l.name && num(l.quantity) > 0)
            .map((l) => ({
              name: l.name,
              quantity: num(l.quantity),
              price: num(l.price),
              iiko_product_id: l.product_id,
              vat_percent: num(l.vat) > 0 ? num(l.vat) : null,
              is_staff: false,
            })),
          // Блок «Траты на персонал» — только у обычной накладной; подпись → наименование,
          // сумма → цена (кол-во 1), статья ДДС → dds_article_id, без товара.
          ...(isBarter
            ? []
            : staffLines
                .filter((l) => l.articleId && num(l.amount) > 0)
                .map((l) => ({
                  name:
                    l.note.trim() ||
                    staffArticles.find((a) => a.id === l.articleId)?.name ||
                    "Персонал",
                  quantity: 1,
                  price: num(l.amount),
                  iiko_product_id: null,
                  vat_percent: null,
                  is_staff: true,
                  dds_article_id: l.articleId,
                }))),
        ],
      }),
    onSuccess: () => {
      onCreated();
      void queryClient.invalidateQueries({ queryKey: ["wh", "next-number"] });
      reset();
      onOpenChange(false);
      toast.success(isBarter ? "Займ оформлен" : "Накладная создана");
    },
    onError: (e) => toast.error(apiErrorMessage(e, "Не удалось создать накладную")),
  });

  const totals = useMemo(() => {
    const total = lines.reduce(
      (s, l) => s + (l.amount !== "" ? num(l.amount) : num(l.quantity) * num(l.price)),
      0,
    );
    const staff = staffLines.reduce((s, l) => s + num(l.amount), 0);
    return { total, staff };
  }, [lines, staffLines]);

  const updateLine = (key: string, patch: Partial<DraftLine>) =>
    setLines((prev) => prev.map((l) => (l.key === key ? { ...l, ...patch } : l)));
  const updateStaffLine = (key: string, patch: Partial<StaffLine>) =>
    setStaffLines((prev) => prev.map((l) => (l.key === key ? { ...l, ...patch } : l)));

  const filledStore = lines.filter((l) => l.name && num(l.quantity) > 0).length;
  const filledStaff = staffLines.filter((l) => l.articleId && num(l.amount) > 0).length;
  // Товарная строка с названием, но без выбора из номенклатуры iiko — блокируем сохранение
  // (иначе позиция потеряется при выгрузке в iiko, как было с «зеленью» в накладной №4).
  const goodsMissingProduct = lines.some(
    (l) => l.name.trim() && num(l.quantity) > 0 && !l.product_id,
  );
  const canSave =
    !!counterpartyId &&
    !!issuedAt &&
    filledStore + filledStaff > 0 &&
    !createMutation.isPending &&
    !goodsMissingProduct &&
    !(markPaid && !cpHasGuid);

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] max-w-3xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Warehouse size={18} aria-hidden="true" />
            {isBarter ? "Бартерная накладная" : "Создать накладную"}
          </DialogTitle>
        </DialogHeader>

        <div className="grid gap-4">
          <div className="inline-flex w-fit overflow-hidden rounded-md border">
            <button
              type="button"
              className={cn(
                "px-4 py-1.5 text-sm",
                !isBarter && "bg-primary/10 font-medium text-primary",
              )}
              onClick={() => setKind("normal")}
            >
              Обычная
            </button>
            <button
              type="button"
              className={cn(
                "px-4 py-1.5 text-sm",
                isBarter && "bg-primary/10 font-medium text-primary",
              )}
              onClick={() => setKind("barter")}
            >
              Бартер
            </button>
          </div>

          {isBarter ? (
            <div className="flex flex-wrap items-center gap-2">
              <div className="inline-flex overflow-hidden rounded-md border">
                <button
                  type="button"
                  className={cn("px-3 py-1 text-xs", !isReturn && "bg-primary/10 font-medium text-primary")}
                  onClick={() => setBarterAction("loan")}
                >
                  Займ
                </button>
                <button
                  type="button"
                  className={cn("px-3 py-1 text-xs", isReturn && "bg-primary/10 font-medium text-primary")}
                  onClick={() => setBarterAction("return")}
                >
                  Возврат
                </button>
              </div>
              {!isReturn ? (
                <div className="inline-flex overflow-hidden rounded-md border">
                  <button
                    type="button"
                    className={cn("px-3 py-1 text-xs", weLend && "bg-muted font-medium")}
                    onClick={() => setWeLend(true)}
                  >
                    Мы выдаём
                  </button>
                  <button
                    type="button"
                    className={cn("px-3 py-1 text-xs", !weLend && "bg-muted font-medium")}
                    onClick={() => setWeLend(false)}
                  >
                    Нам выдают
                  </button>
                </div>
              ) : null}
            </div>
          ) : null}

          {isReturn ? (
            <ReturnForm
              onDone={() => {
                onCreated();
                void queryClient.invalidateQueries({ queryKey: ["wh", "next-number"] });
                reset();
                onOpenChange(false);
              }}
            />
          ) : (
            <>
          <div className="grid gap-4 sm:grid-cols-3">
            <div className="grid gap-2">
              <div className="flex items-center justify-between gap-2">
                <Label>{isBarter ? "Бартерный контрагент" : "Контрагент"}</Label>
              </div>
              <CounterpartySearch
                items={counterpartyOptions}
                value={counterpartyId}
                onPick={setCounterpartyId}
              />
              {isBarter && counterpartyOptions.length === 0 ? (
                <p className="text-xs text-muted-foreground">
                  Нет бартерных партнёров. Добавьте контрагента с типом «Бартер».
                </p>
              ) : null}
            </div>
            <div className="grid gap-2">
              <Label>Дата и время чека</Label>
              <Input
                type="datetime-local"
                value={issuedAt}
                onChange={(e) => setIssuedAt(e.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <Label>Номер</Label>
              <Input value={number} onChange={(e) => setNumber(e.target.value)} />
            </div>
          </div>

          <div className="rounded-md border p-3">
            <div className="flex items-center justify-between">
              <span className="flex items-center gap-2 text-sm font-medium">
                <Warehouse size={16} className="text-sky-600" aria-hidden="true" />
                {isBarter ? "Товары займа" : "Закупка на склад"}
              </span>
              {!isBarter ? (
                <span className="rounded bg-sky-50 px-2 py-0.5 text-xs text-sky-700">
                  → iiko · приходная накладная
                </span>
              ) : null}
            </div>

            <div
              className={cn(
                "mt-2 grid items-center gap-2 px-1 text-xs text-muted-foreground",
                isBarter
                  ? "grid-cols-[minmax(0,1fr)_64px_88px_104px_28px]"
                  : "grid-cols-[minmax(0,1fr)_64px_88px_56px_104px_28px]",
              )}
            >
              <span>Товар</span>
              <span>Кол-во</span>
              <span>Цена</span>
              {!isBarter ? <span>НДС%</span> : null}
              <span className="text-right">Сумма</span>
              <span aria-hidden="true" />
            </div>

            <div className="mt-1.5 space-y-1.5">
              {lines.map((line) => (
                <LineRow
                  key={line.key}
                  line={line}
                  barter={isBarter}
                  products={productsQuery.data ?? []}
                  onChange={(patch) => updateLine(line.key, patch)}
                  onRemove={() =>
                    setLines((prev) =>
                      prev.length > 1 ? prev.filter((l) => l.key !== line.key) : prev,
                    )
                  }
                />
              ))}
            </div>

            <div className="mt-2 flex items-center justify-between">
              <Button
                size="sm"
                variant="ghost"
                className="text-muted-foreground"
                onClick={() => setLines((prev) => [...prev, emptyLine()])}
              >
                <Plus size={14} aria-hidden="true" />
                товар
              </Button>
              {totals.total > 0 ? (
                <span className="text-xs text-muted-foreground">
                  подытог{" "}
                  <span className="font-medium text-foreground">{formatRub(totals.total)}</span>
                </span>
              ) : null}
            </div>
          </div>

          {!isBarter ? (
            <div className="rounded-md border border-amber-200 bg-amber-50/40 p-3">
              <div className="flex items-center justify-between">
                <span className="flex items-center gap-2 text-sm font-medium">
                  <Users size={16} className="text-amber-700" aria-hidden="true" />
                  Траты на персонал
                </span>
                <span className="rounded bg-amber-100/70 px-2 py-0.5 text-xs text-amber-800">
                  только ДДС · не в iiko
                </span>
              </div>
              <p className="mt-0.5 text-xs text-amber-700">
                Статья + подпись + сумма, без номенклатуры и склада.
              </p>

              {staffLines.length > 0 ? (
                <div className="mt-2 space-y-1.5">
                  {staffLines.map((line) => (
                    <StaffLineRow
                      key={line.key}
                      line={line}
                      articles={staffArticles}
                      onChange={(patch) => updateStaffLine(line.key, patch)}
                      onRemove={() =>
                        setStaffLines((prev) => prev.filter((l) => l.key !== line.key))
                      }
                    />
                  ))}
                </div>
              ) : null}

              <div className="mt-2 flex items-center justify-between">
                <Button
                  size="sm"
                  variant="ghost"
                  className="text-muted-foreground"
                  onClick={() => setStaffLines((prev) => [...prev, emptyStaffLine()])}
                >
                  <Plus size={14} aria-hidden="true" />
                  трата
                </Button>
                {totals.staff > 0 ? (
                  <span className="text-xs text-amber-700">
                    подытог <span className="font-medium">{formatRub(totals.staff)}</span>
                  </span>
                ) : null}
              </div>
            </div>
          ) : null}

          <div className="flex flex-wrap items-center gap-4 rounded-md bg-muted/50 p-2 text-sm tabular-nums">
            <span>
              Итого: <span className="font-medium">{formatRub(totals.total + totals.staff)}</span>
            </span>
            {!isBarter && totals.staff > 0 ? (
              <span className="text-xs text-muted-foreground">
                склад {formatRub(totals.total)} + персонал {formatRub(totals.staff)}
              </span>
            ) : null}
          </div>
            </>
          )}
        </div>

        {kassaOnly && !isBarter ? (
          <div className="space-y-2 rounded-md border p-3">
            <label className="flex items-center gap-2 text-sm font-medium">
              <Switch
                checked={markPaid}
                disabled={!cpHasGuid}
                onCheckedChange={(v) => {
                  setMarkPaid(v);
                  if (v && !paidAmount) setPaidAmount(String(totals.total + totals.staff));
                }}
              />
              Оплачено с ТК Черникова
            </label>
            {counterpartyId && !cpHasGuid ? (
              <p className="text-xs text-amber-600">
                Контрагент не сматчен с iiko — оплату провести нельзя.
              </p>
            ) : null}
            {markPaid ? (
              <div className="flex flex-wrap items-center gap-2">
                <Label className="text-sm">Сумма оплаты</Label>
                <Input
                  type="number"
                  className="w-40"
                  value={paidAmount}
                  onChange={(e) => setPaidAmount(e.target.value)}
                  placeholder={String(totals.total + totals.staff)}
                />
                <span className="text-xs text-muted-foreground">
                  по умолчанию — вся сумма {formatRub(totals.total + totals.staff)}
                </span>
              </div>
            ) : null}
          </div>
        ) : null}

        {!isReturn ? (
          <DialogFooter>
            <Button
              disabled={!canSave}
              onClick={() => {
                // В Кассе без тоггла «Оплачено» — переспросить (частая ошибка: забыли отметить).
                if (kassaOnly && !isBarter && !markPaid) {
                  setUnpaidConfirmOpen(true);
                } else {
                  createMutation.mutate();
                }
              }}
            >
              {createMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : null}
              {isBarter ? "Оформить займ" : "Создать"}
            </Button>
          </DialogFooter>
        ) : null}
      </DialogContent>
      </Dialog>
      <Dialog open={unpaidConfirmOpen} onOpenChange={setUnpaidConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Создать без оплаты?</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            Накладная будет создана{" "}
            <span className="font-medium text-foreground">неоплаченной</span>. Если вы платили за
            неё с кассы — вернитесь и включите тоггл «Оплачено с ТК Черникова». Иначе её можно
            будет оплатить позже во вкладке «Накладные».
          </p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setUnpaidConfirmOpen(false)}>
              Вернуться
            </Button>
            <Button
              onClick={() => {
                setUnpaidConfirmOpen(false);
                createMutation.mutate();
              }}
            >
              Создать неоплаченную
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

function ReturnForm({ onDone }: { onDone: () => void }) {
  const [loanId, setLoanId] = useState("");
  const [issuedAt, setIssuedAt] = useState("");
  const [returnKind, setReturnKind] = useState<"goods" | "money">("goods");
  const [moneyAmount, setMoneyAmount] = useState("");
  const [qtyByLine, setQtyByLine] = useState<Record<string, string>>({});
  // Гашение деньгами нашего займа: по строкам займа — кг × ТЕКУЩАЯ цена (ввод оператора).
  const [moneyByLine, setMoneyByLine] = useState<Record<string, { qty: string; price: string }>>(
    {},
  );
  const [moneyChannel, setMoneyChannel] = useState<"wallet" | "bank">("wallet");
  const [walletId, setWalletId] = useState("");
  const [bankOperationId, setBankOperationId] = useState("");

  const loansQuery = useQuery({ queryKey: ["wh", "loans"], queryFn: () => getOpenLoans() });
  const returnableQuery = useQuery({
    queryKey: ["wh", "returnable", loanId],
    queryFn: () => getLoanReturnable(loanId),
    enabled: !!loanId,
  });
  const loan = returnableQuery.data;
  const walletsQuery = useQuery({
    queryKey: ["dds", "wallets"],
    queryFn: getDdsWallets,
    enabled: returnKind === "money",
  });
  // Пикер операции выписки: наш заём гасят входящие, их заём — наши исходящие платежи.
  const opsQuery = useQuery({
    queryKey: ["wh", "barter-ops", loan?.we_lend],
    queryFn: () => getDdsBankOperations({ limit: 200 }),
    enabled: returnKind === "money" && moneyChannel === "bank" && !!loan,
  });
  const bankOps = useMemo(() => {
    const wantIn = loan?.we_lend ?? false;
    return (opsQuery.data?.items ?? []).filter(
      (op) => op.direction === (wantIn ? "in" : "out") && !op.transfer_group_id,
    );
  }, [opsQuery.data, loan]);

  const returnLines = useMemo(() => {
    if (!loan) return [];
    return loan.lines.map((l) => {
      const qty = Math.min(num(qtyByLine[l.id] ?? ""), l.remaining_qty);
      return { ...l, raw: qtyByLine[l.id] ?? "", qty, amount: Math.round(qty * l.price * 100) / 100 };
    });
  }, [loan, qtyByLine]);
  // Денежные строки нашего займа: деньги = кг × текущая цена; в зачёт долга — кг × цена займа.
  const moneyLines = useMemo(() => {
    if (!loan) return [];
    return loan.lines.map((l) => {
      const entry = moneyByLine[l.id] ?? { qty: "", price: "" };
      const qty = Math.min(num(entry.qty), l.remaining_qty);
      const price = num(entry.price);
      return {
        ...l,
        rawQty: entry.qty,
        rawPrice: entry.price,
        qty,
        unitPrice: price,
        money: Math.round(qty * price * 100) / 100,
        credited: Math.round(qty * l.price * 100) / 100,
      };
    });
  }, [loan, moneyByLine]);
  const money = loan
    ? loan.we_lend
      ? moneyLines.reduce((s, l) => s + l.money, 0)
      : Math.min(num(moneyAmount), loan.remaining)
    : 0;
  const moneyCredited = moneyLines.reduce((s, l) => s + l.credited, 0);
  const totalReturn =
    returnKind === "money" ? money : returnLines.reduce((s, l) => s + l.amount, 0);

  const returnMutation = useMutation({
    mutationFn: async (): Promise<void> => {
      if (returnKind === "money") {
        // Деньги двигаются по-настоящему: нал — приход/расход по кошельку, банк — привязка
        // операции выписки. Наш заём — кг × текущая цена, их заём — сумма по накладной.
        await settleLoanWithMoney(loanId, {
          operation_date: issuedAt.slice(0, 10),
          wallet_id: moneyChannel === "wallet" ? walletId : null,
          bank_operation_id: moneyChannel === "bank" ? bankOperationId : null,
          lines: loan?.we_lend
            ? moneyLines
                .filter((l) => l.qty > 0 && l.unitPrice > 0)
                .map((l) => ({
                  loan_line_item_id: l.id,
                  quantity: l.qty,
                  unit_price: l.unitPrice,
                }))
            : null,
          amount: loan?.we_lend ? null : money,
        });
        return;
      }
      await createBarterReturn({
        loan_id: loanId,
        issued_at: issuedAt,
        // Товаром — построчно по ИСХОДНОЙ цене займа (кг возвращаются по цене выдачи).
        returns: returnLines
          .filter((l) => l.qty > 0)
          .map((l) => ({ loan_line_item_id: l.id, quantity: l.qty, amount: l.amount })),
      });
    },
    onSuccess: () => {
      toast.success(returnKind === "money" ? "Гашение деньгами проведено" : "Возврат проведён");
      onDone();
    },
    onError: (e) => toast.error(apiErrorMessage(e, "Не удалось провести возврат")),
  });

  const moneyChannelReady =
    moneyChannel === "wallet" ? !!walletId : !!bankOperationId;
  const canReturn =
    !!loanId &&
    !!issuedAt &&
    totalReturn > 0 &&
    (returnKind === "goods" || moneyChannelReady) &&
    !returnMutation.isPending;

  return (
    <div className="space-y-4">
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="grid gap-2">
          <Label>Заём для возврата</Label>
          <Select
            value={loanId}
            onValueChange={(v) => {
              setLoanId(v);
              setQtyByLine({});
            }}
          >
            <SelectTrigger>
              <SelectValue placeholder="Выберите открытый заём" />
            </SelectTrigger>
            <SelectContent>
              {(loansQuery.data ?? []).map((ln) => (
                <SelectItem key={ln.id} value={ln.id}>
                  {ln.counterparty_name} · №{ln.number ?? "—"} · остаток {formatRub(ln.remaining)} ·{" "}
                  {ln.we_lend ? "мы выдали" : "нам выдали"}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="grid gap-2">
          <Label>Дата и время возврата</Label>
          <Input
            type="datetime-local"
            value={issuedAt}
            onChange={(e) => setIssuedAt(e.target.value)}
          />
        </div>
      </div>

      {loan ? (
        <>
          <div className="inline-flex w-fit overflow-hidden rounded-md border">
            <button
              type="button"
              className={cn("px-3 py-1 text-xs", returnKind === "goods" && "bg-muted font-medium")}
              onClick={() => setReturnKind("goods")}
            >
              Товаром
            </button>
            <button
              type="button"
              className={cn("px-3 py-1 text-xs", returnKind === "money" && "bg-muted font-medium")}
              onClick={() => setReturnKind("money")}
            >
              Деньгами
            </button>
          </div>

          {returnKind === "goods" ? (
            <div className="space-y-2">
              <div className="grid grid-cols-[1fr_96px_84px_90px] gap-2 px-2 text-xs text-muted-foreground">
                <span>Товар</span>
                <span className="text-right">Остаток</span>
                <span className="text-right">Вернуть</span>
                <span className="text-right">Сумма</span>
              </div>
              {returnLines.map((l) => (
                <div
                  key={l.id}
                  className="grid grid-cols-[1fr_96px_84px_90px] items-center gap-2 rounded-md border p-2 text-sm"
                >
                  <span className="truncate">{l.name}</span>
                  <span className="text-right tabular-nums text-muted-foreground">
                    {l.remaining_qty} {l.unit ?? ""}
                  </span>
                  <Input
                    type="number"
                    min={0}
                    max={l.remaining_qty}
                    value={l.raw}
                    onChange={(e) => setQtyByLine((p) => ({ ...p, [l.id]: e.target.value }))}
                  />
                  <span className="text-right tabular-nums">{formatRub(l.amount)}</span>
                </div>
              ))}
            </div>
          ) : (
            <div className="space-y-3">
              {loan.we_lend ? (
                <div className="space-y-2">
                  <p className="text-xs text-muted-foreground">
                    Контрагент платит по ТЕКУЩЕЙ цене; в зачёт долга идёт кг × цена выдачи.
                  </p>
                  <div className="grid grid-cols-[1fr_84px_84px_96px_90px] gap-2 px-2 text-xs text-muted-foreground">
                    <span>Товар</span>
                    <span className="text-right">Остаток</span>
                    <span className="text-right">Кг</span>
                    <span className="text-right">Цена сейчас</span>
                    <span className="text-right">Деньги</span>
                  </div>
                  {moneyLines.map((l) => (
                    <div
                      key={l.id}
                      className="grid grid-cols-[1fr_84px_84px_96px_90px] items-center gap-2 rounded-md border p-2 text-sm"
                    >
                      <span className="truncate">
                        {l.name}
                        <span className="block text-[11px] text-muted-foreground">
                          выдано по {formatRub(l.price)}
                          {l.last_purchase_price != null
                            ? ` · закуп сейчас ~${formatRub(l.last_purchase_price)}`
                            : ""}
                        </span>
                      </span>
                      <span className="text-right tabular-nums text-muted-foreground">
                        {l.remaining_qty} {l.unit ?? ""}
                      </span>
                      <Input
                        type="number"
                        min={0}
                        max={l.remaining_qty}
                        value={l.rawQty}
                        onChange={(e) =>
                          setMoneyByLine((p) => ({
                            ...p,
                            [l.id]: { qty: e.target.value, price: p[l.id]?.price ?? "" },
                          }))
                        }
                      />
                      <Input
                        type="number"
                        min={0}
                        placeholder={
                          l.last_purchase_price != null ? String(l.last_purchase_price) : ""
                        }
                        value={l.rawPrice}
                        onChange={(e) =>
                          setMoneyByLine((p) => ({
                            ...p,
                            [l.id]: { qty: p[l.id]?.qty ?? "", price: e.target.value },
                          }))
                        }
                      />
                      <span className="text-right tabular-nums">{formatRub(l.money)}</span>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="grid gap-2 sm:max-w-xs">
                  <Label>Сумма оплаты займа, ₽</Label>
                  <Input
                    type="number"
                    min={0}
                    max={loan.remaining}
                    value={moneyAmount}
                    onChange={(e) => setMoneyAmount(e.target.value)}
                  />
                  <p className="text-xs text-muted-foreground">
                    Оплата «как обычной поставки» — по сумме накладной займа.
                  </p>
                </div>
              )}

              <div className="grid gap-4 sm:grid-cols-2">
                <div className="grid gap-2">
                  <Label>Канал денег</Label>
                  <div className="inline-flex w-fit overflow-hidden rounded-md border">
                    <button
                      type="button"
                      className={cn(
                        "px-3 py-1 text-xs",
                        moneyChannel === "wallet" && "bg-muted font-medium",
                      )}
                      onClick={() => setMoneyChannel("wallet")}
                    >
                      Наличные / кошелёк
                    </button>
                    <button
                      type="button"
                      className={cn(
                        "px-3 py-1 text-xs",
                        moneyChannel === "bank" && "bg-muted font-medium",
                      )}
                      onClick={() => setMoneyChannel("bank")}
                    >
                      Банк-операция
                    </button>
                  </div>
                </div>
                {moneyChannel === "wallet" ? (
                  <div className="grid gap-2">
                    <Label>Кошелёк</Label>
                    <Select value={walletId} onValueChange={setWalletId}>
                      <SelectTrigger>
                        <SelectValue placeholder="Куда пришли / откуда ушли деньги" />
                      </SelectTrigger>
                      <SelectContent>
                        {(walletsQuery.data ?? [])
                          .filter((w) => w.status === "active")
                          .map((w) => (
                            <SelectItem key={w.id} value={w.id}>
                              {w.name}
                            </SelectItem>
                          ))}
                      </SelectContent>
                    </Select>
                  </div>
                ) : (
                  <div className="grid gap-2">
                    <Label>Операция выписки ({loan.we_lend ? "входящая" : "исходящая"})</Label>
                    <Select value={bankOperationId} onValueChange={setBankOperationId}>
                      <SelectTrigger>
                        <SelectValue placeholder="Выберите операцию" />
                      </SelectTrigger>
                      <SelectContent>
                        {bankOps.map((op) => (
                          <SelectItem key={op.id} value={op.id}>
                            {op.operation_date} · {formatRub(Number(op.amount))} ·{" "}
                            {op.counterparty_name_raw ?? op.payment_purpose ?? "—"}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                )}
              </div>
            </div>
          )}

          <div className="flex items-center justify-between rounded-md bg-muted/50 p-2 text-sm">
            <span>
              {returnKind === "money" && loan.we_lend ? (
                <>
                  Деньгами: <span className="font-medium tabular-nums">{formatRub(money)}</span>
                  <span className="ml-2 text-xs text-muted-foreground">
                    в зачёт долга {formatRub(moneyCredited)}
                  </span>
                </>
              ) : (
                <>
                  К возврату:{" "}
                  <span className="font-medium tabular-nums">{formatRub(totalReturn)}</span>
                </>
              )}
            </span>
            <span className="text-xs text-muted-foreground">
              остаток займа {formatRub(loan.remaining)}
            </span>
          </div>
        </>
      ) : null}

      <DialogFooter>
        <Button disabled={!canReturn} onClick={() => returnMutation.mutate()}>
          {returnMutation.isPending ? (
            <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
          ) : null}
          Провести возврат
        </Button>
      </DialogFooter>
    </div>
  );
}

export function LineRow({
  line,
  barter,
  products,
  onChange,
  onRemove,
}: {
  line: DraftLine;
  barter: boolean;
  products: WarehouseProduct[];
  onChange: (patch: Partial<DraftLine>) => void;
  onRemove: () => void;
}) {
  // Товарная строка обязана быть выбрана из номенклатуры iiko (есть product_id). Иначе при
  // выгрузке в iiko строка молча теряется (нет product_guid) — накладная уходит неполной.
  const needsProduct = !!line.name.trim() && !line.product_id;
  // Живой контроль цены: подтягиваем скользящее среднее по товару и предупреждаем прямо при
  // вводе, если цена улетела за порог (+сверху/−снизу). Не блокирует — только подсветка.
  const priceStatsQuery = useQuery({
    queryKey: ["wh", "price-stats", line.product_id],
    queryFn: () => getProductPriceStats(line.product_id!),
    enabled: !!line.product_id,
    staleTime: 60_000,
  });
  const stats = priceStatsQuery.data;
  const priceNum = num(line.price);
  let priceWarn: { dir: "high" | "low"; dev: number; avg: number } | null = null;
  if (
    stats &&
    stats.avg_price != null &&
    stats.sample_count >= stats.min_samples &&
    priceNum > 0
  ) {
    const dev = ((priceNum - stats.avg_price) / stats.avg_price) * 100;
    if (dev > stats.upper_pct) priceWarn = { dir: "high", dev, avg: stats.avg_price };
    else if (dev < -stats.lower_pct) priceWarn = { dir: "low", dev, avg: stats.avg_price };
  }
  return (
    <>
    <div
      className={cn(
        "grid items-center gap-2",
        barter
          ? "grid-cols-[minmax(0,1fr)_64px_88px_104px_28px]"
          : "grid-cols-[minmax(0,1fr)_64px_88px_56px_104px_28px]",
        needsProduct && "rounded-md p-1 ring-1 ring-red-300",
      )}
    >
      <ProductSearch
        value={line.name}
        products={products}
        onPick={(p) => onChange({ product_id: p.id, name: p.name, unit: p.unit })}
        onTextChange={(text) => onChange({ name: text, product_id: null, unit: null })}
      />
      <Input
        inputMode="decimal"
        className="text-right"
        value={line.quantity}
        title={line.unit ?? undefined}
        onChange={(e) =>
          onChange({
            quantity: e.target.value,
            amount: toAmount(num(e.target.value) * num(line.price)),
          })
        }
      />
      <Input
        inputMode="decimal"
        className={cn("text-right", priceWarn && "ring-1 ring-amber-400")}
        value={line.price}
        title={
          priceWarn
            ? `Среднее ${formatRub(priceWarn.avg)} · отклонение ${
                priceWarn.dev > 0 ? "+" : ""
              }${priceWarn.dev.toFixed(1)}%`
            : undefined
        }
        onChange={(e) =>
          onChange({
            price: e.target.value,
            amount: toAmount(num(e.target.value) * num(line.quantity)),
          })
        }
      />
      {!barter ? (
        <Input
          inputMode="decimal"
          className="text-right"
          value={line.vat}
          onChange={(e) => onChange({ vat: e.target.value })}
          title="Ставка НДС, %"
        />
      ) : null}
      <Input
        inputMode="decimal"
        className="text-right"
        value={line.amount}
        title="Сумма строки — цена × кол-во; при вводе суммы цена пересчитывается"
        onChange={(e) => {
          const q = num(line.quantity);
          onChange({
            amount: e.target.value,
            price: q > 0 ? toAmount(num(e.target.value) / q) : line.price,
          });
        }}
      />
      <button
        type="button"
        className="text-muted-foreground hover:text-red-600"
        onClick={onRemove}
        aria-label="Удалить строку"
      >
        <Trash2 size={15} />
      </button>
    </div>
    {needsProduct ? (
      <p className="px-1 text-xs text-red-600">
        Выберите конкретный товар из номенклатуры iiko
      </p>
    ) : null}
    {priceWarn ? (
      <p className="px-1 text-xs text-amber-600">
        Цена {priceWarn.dir === "high" ? "выше" : "ниже"} среднего ({formatRub(priceWarn.avg)}) на{" "}
        {priceWarn.dev > 0 ? "+" : ""}
        {priceWarn.dev.toFixed(1)}% — накладная попадёт на проверку цен.
      </p>
    ) : null}
    </>
  );
}

export function StaffLineRow({
  line,
  articles,
  onChange,
  onRemove,
}: {
  line: StaffLine;
  articles: StaffArticle[];
  onChange: (patch: Partial<StaffLine>) => void;
  onRemove: () => void;
}) {
  return (
    <div className="grid grid-cols-[1.1fr_1.2fr_76px_28px] items-center gap-2">
      <Select value={line.articleId} onValueChange={(v) => onChange({ articleId: v })}>
        <SelectTrigger className="h-9" aria-label="Статья ДДС">
          <SelectValue placeholder="Статья" />
        </SelectTrigger>
        <SelectContent>
          {articles.map((a) => (
            <SelectItem key={a.id} value={a.id}>
              {a.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Input
        placeholder="Подпись (напр. обеды поварам)"
        value={line.note}
        onChange={(e) => onChange({ note: e.target.value })}
      />
      <Input
        inputMode="decimal"
        className="text-right"
        placeholder="сумма"
        value={line.amount}
        onChange={(e) => onChange({ amount: e.target.value })}
      />
      <button
        type="button"
        className="text-muted-foreground hover:text-red-600"
        onClick={onRemove}
        aria-label="Удалить трату"
      >
        <Trash2 size={15} />
      </button>
    </div>
  );
}

function CounterpartySearch({
  items,
  value,
  onPick,
}: {
  items: RegistryItem[];
  value: string;
  onPick: (id: string) => void;
}) {
  const [term, setTerm] = useState("");
  const [focused, setFocused] = useState(false);
  const selected = items.find((i) => i.counterparty_id === value);

  const matches = useMemo(() => {
    const q = term.trim().toLowerCase();
    if (!q) return items.slice(0, 12);
    return items.filter((i) => i.name.toLowerCase().includes(q)).slice(0, 12);
  }, [items, term]);

  return (
    <div className="relative">
      <Input
        value={focused ? term : selected?.name ?? term}
        placeholder="Начните вводить имя"
        onChange={(e) => setTerm(e.target.value)}
        onFocus={() => {
          setFocused(true);
          setTerm("");
        }}
        onBlur={() => setTimeout(() => setFocused(false), 150)}
      />
      {focused && matches.length > 0 ? (
        <div className="absolute z-50 mt-1 max-h-56 w-full overflow-auto rounded-md border bg-popover p-1 shadow-md">
          {matches.map((i) => (
            <button
              key={i.counterparty_id}
              type="button"
              className="block w-full truncate rounded px-2 py-1.5 text-left text-sm hover:bg-muted"
              onMouseDown={(e) => {
                e.preventDefault();
                onPick(i.counterparty_id);
                setFocused(false);
              }}
            >
              {i.name}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

