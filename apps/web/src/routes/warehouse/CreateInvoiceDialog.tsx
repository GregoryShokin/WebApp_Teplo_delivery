import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Download,
  LoaderCircle,
  Plus,
  Trash2,
  Upload,
  Users,
  Warehouse,
} from "lucide-react";
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
import { apiErrorMessage } from "@/lib/api";
import { cn } from "@/lib/utils";

import { getRegistry, type RegistryItem } from "../counterparties/api";
import { formatRub } from "../counterparties/shared";
import { ProductSearch } from "./ProductSearch";
import {
  createWarehouseInvoice,
  getNextInvoiceNumber,
  getProductPriceStats,
  getProducts,
  getStaffArticles,
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

// Разбор числа из поля ввода. Запятая — штатный десятичный разделитель русской раскладки:
// без неё `Number("0,5")` даёт NaN → 0, и строка молча выпадает из фильтров (кнопка сохранения
// гаснет), а сумма перестаёт пересчитываться из цены. Пробелы-разделители разрядов тоже режем.
export const num = (v: string) =>
  Math.max(0, Number(String(v).replace(/[\s ]/g, "").replace(",", ".")) || 0);
// Число → строка суммы/цены без хвостовых нулей (пустая строка для нуля).
const toAmount = (n: number) => (n > 0 ? String(Math.round(n * 100) / 100) : "");
// Кол-во для показа: в русском UI разделитель — запятая (0,5 кг, а не 0.5 кг).
const fmtQty = (n: number) => String(n).replace(".", ",");

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
  const [counterpartyId, setCounterpartyId] = useState("");
  const [issuedAt, setIssuedAt] = useState("");
  const [number, setNumber] = useState("");
  const [markPaid, setMarkPaid] = useState(false);
  const [paidAmount, setPaidAmount] = useState("");
  const [unpaidConfirmOpen, setUnpaidConfirmOpen] = useState(false);
  const [loanConfirmOpen, setLoanConfirmOpen] = useState(false);
  const [lines, setLines] = useState<DraftLine[]>([emptyLine()]);
  const [staffLines, setStaffLines] = useState<StaffLine[]>([]);
  const queryClient = useQueryClient();
  const isBarter = kind === "barter";

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
              // Сумма строки — эталон: то, что видит пользователь, а не кол-во×округлённая цена.
              sum: l.amount !== "" ? num(l.amount) : num(l.quantity) * num(l.price),
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
                  sum: num(l.amount),
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
  // Долг по займу номинирован товаром и гасится ПО ЦЕНЕ ВЫДАЧИ — заём без цены нечем гасить.
  const barterNeedsPrice = isBarter && filledStore > 0 && totals.total <= 0;
  const canSave =
    !!counterpartyId &&
    !!issuedAt &&
    filledStore + filledStaff > 0 &&
    !createMutation.isPending &&
    !goodsMissingProduct &&
    !barterNeedsPrice &&
    !(markPaid && !cpHasGuid);
  // Кнопка гасла молча: пользователь видел заполненную форму и не понимал, чего не хватает
  // (чаще всего — контрагент напечатан, но не выбран из выпадающего списка).
  const blockReason = createMutation.isPending
    ? null
    : !counterpartyId
      ? "выберите контрагента из выпадающего списка"
      : !issuedAt
        ? "укажите дату и время"
        : goodsMissingProduct
          ? "выберите товар из номенклатуры iiko"
          : filledStore + filledStaff === 0
            ? "заполните строку: товар и количество"
            : barterNeedsPrice
              ? "укажите цену за единицу или сумму строки"
              : markPaid && !cpHasGuid
                ? "контрагент не сматчен с iiko — снимите отметку об оплате"
                : null;

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

          {/* Только НАПРАВЛЕНИЕ займа (возврат оформляется в окне гашения). Одна кнопка-
              переключатель: стрелка ИЗ поддона — товар уходит от нас, В поддон — приходит к нам. */}
          {isBarter ? (
            <button
              type="button"
              onClick={() => setWeLend((prev) => !prev)}
              className="inline-flex w-fit items-center gap-2 rounded-md border px-3 py-2 text-sm transition hover:bg-muted"
              title="Нажмите, чтобы сменить направление займа"
            >
              {weLend ? (
                <Upload size={18} className="text-amber-600" aria-hidden="true" />
              ) : (
                <Download size={18} className="text-emerald-600" aria-hidden="true" />
              )}
              <span className="font-medium">{weLend ? "Мы выдаём" : "Нам выдают"}</span>
              <span className="text-xs text-muted-foreground">
                {weLend ? "товар уходит партнёру" : "товар приходит к нам"}
              </span>
            </button>
          ) : null}

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
                  ? "grid-cols-[minmax(0,1fr)_64px_32px_88px_104px_28px]"
                  : "grid-cols-[minmax(0,1fr)_64px_32px_88px_56px_104px_28px]",
              )}
            >
              <span>Товар</span>
              <span className="text-right">Кол-во</span>
              <span className="text-center">Ед.</span>
              <span className="text-right">Цена/ед.</span>
              {!isBarter ? <span className="text-right">НДС%</span> : null}
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

        <DialogFooter className="sm:items-center sm:justify-between">
            {blockReason ? (
              <p className="text-xs text-amber-600">Чтобы сохранить — {blockReason}.</p>
            ) : (
              <span aria-hidden="true" />
            )}
            <Button
              disabled={!canSave}
              onClick={() => {
                // В Кассе без тоггла «Оплачено» — переспросить (частая ошибка: забыли отметить).
                if (isBarter) {
                  // Заём — обязательство в товаре: показываем итог словами до оформления.
                  setLoanConfirmOpen(true);
                } else if (kassaOnly && !markPaid) {
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
      </DialogContent>
      </Dialog>
      <Dialog open={loanConfirmOpen} onOpenChange={setLoanConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Подтвердите заём</DialogTitle>
          </DialogHeader>
          <div className="space-y-3 text-sm">
            <p>
              {weLend ? "Мы выдаём" : "Нам выдают"}{" "}
              {weLend ? "компании" : "от компании"}{" "}
              <span className="font-medium">{selectedCp?.name ?? "—"}</span> заём:
            </p>
            <ul className="space-y-1 rounded-md bg-muted/50 px-3 py-2">
              {lines
                .filter((l) => l.name && num(l.quantity) > 0)
                .map((l) => (
                  <li key={l.key} className="flex items-center justify-between gap-3">
                    <span className="font-medium">{l.name}</span>
                    {/* Раскладка «кол-во × цена = сумма»: партнёр должен вернуть тот же товар
                        по цене выдачи, поэтому цена за единицу важнее итога. */}
                    <span className="tabular-nums text-muted-foreground">
                      {fmtQty(num(l.quantity))} {l.unit ?? "ед."} × {formatRub(num(l.price))} ={" "}
                      {formatRub(l.amount !== "" ? num(l.amount) : num(l.quantity) * num(l.price))}
                    </span>
                  </li>
                ))}
            </ul>
            <p className="text-muted-foreground">
              Долг в ТОВАРЕ на {formatRub(totals.total)}: гасится возвратом того же товара по цене
              выдачи либо деньгами — в карточке партнёра.
            </p>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setLoanConfirmOpen(false)}>
              Вернуться
            </Button>
            <Button
              onClick={() => {
                setLoanConfirmOpen(false);
                createMutation.mutate();
              }}
            >
              ОК, подтверждаю
            </Button>
          </DialogFooter>
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
  // Единица приходит из номенклатуры iiko вместе с товаром; до выбора товара её нет.
  const unitLabel = line.unit?.trim() || null;
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
          ? "grid-cols-[minmax(0,1fr)_64px_32px_88px_104px_28px]"
          : "grid-cols-[minmax(0,1fr)_64px_32px_88px_56px_104px_28px]",
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
        aria-label="Количество"
        value={line.quantity}
        title={unitLabel ? `Количество, ${unitLabel}` : undefined}
        onChange={(e) =>
          onChange({
            quantity: e.target.value,
            amount: toAmount(num(e.target.value) * num(line.price)),
          })
        }
      />
      {/* Единица из номенклатуры iiko: без неё непонятно, за что цена — за кг или за фасовку. */}
      <span
        className="truncate text-center text-xs text-muted-foreground"
        title={unitLabel ? `Единица измерения: ${unitLabel}` : "Единица появится после выбора товара"}
      >
        {unitLabel ?? "—"}
      </span>
      <Input
        inputMode="decimal"
        className={cn("text-right", priceWarn && "ring-1 ring-amber-400")}
        aria-label={unitLabel ? `Цена за 1 ${unitLabel}` : "Цена за единицу"}
        value={line.price}
        placeholder={unitLabel ? `₽/${unitLabel}` : "₽/ед."}
        title={
          priceWarn
            ? `Среднее ${formatRub(priceWarn.avg)} · отклонение ${
                priceWarn.dev > 0 ? "+" : ""
              }${priceWarn.dev.toFixed(1)}%`
            : `Цена за 1 ${unitLabel ?? "единицу"} — сумма = цена × кол-во`
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
          aria-label="Ставка НДС, %"
          onChange={(e) => onChange({ vat: e.target.value })}
          title="Ставка НДС, %"
        />
      ) : null}
      <Input
        inputMode="decimal"
        className="text-right"
        aria-label="Сумма строки"
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

export function CounterpartySearch({
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

