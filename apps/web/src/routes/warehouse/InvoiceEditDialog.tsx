import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Plus, Undo2 } from "lucide-react";
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
import { apiErrorMessage } from "@/lib/api";
import { getExpenseArticles, getRegistry } from "@/routes/counterparties/api";
import { formatRub } from "@/routes/counterparties/shared";

import {
  CounterpartySearch,
  emptyLine,
  emptyStaffLine,
  LineRow,
  num,
  StaffLineRow,
  type DraftLine,
  type StaffLine,
} from "./CreateInvoiceDialog";
import {
  adjustPaidInvoice,
  getProducts,
  getStaffArticles,
  getWarehouseInvoice,
  updateWarehouseInvoice,
} from "./api";

/** Складская строка формы + то, что документ уже знает о ней и что нельзя потерять при правке:
 * статья ДДС (у чека Кассы складская строка несёт «Оплата поставщикам») и пометка возврата. */
type StoreLine = DraftLine & { ddsArticleId: string | null; isReturn: boolean };
/** Расходная строка (не товар): своя статья ДДС. У чека Кассы такие строки — «Расходы на
 * питание персонала», «Содержание торговых точек»; у накладной тот же блок — «персонал». */
type ExpenseLine = StaffLine & { isReturn: boolean };

/** Возвращённая в магазин позиция чека: остаётся в документе (сверка с бумажным чеком копейка
 * в копейку), но не проводится. Пометку саму по себе в правке не снимают — она приехала с
 * кассы вместе со строкой; форма её показывает и возвращает обратно нетронутой. */
function ReturnMark({ isReturn, children }: { isReturn: boolean; children: ReactNode }) {
  if (!isReturn) return <>{children}</>;
  return (
    <div className="space-y-1 rounded-md border border-red-200 bg-red-50/50 p-1.5">
      <div className="flex items-center gap-1 text-xs text-red-600">
        <Undo2 size={12} aria-hidden="true" /> возвращено в магазин — в сумму не входит
      </div>
      {children}
    </div>
  );
}

/** Правка позиций накладной. По умолчанию — НЕОПЛАЧЕННОЙ (не бартерной): «переделать и отправить
 * в iiko». В режиме `paid` — исправление УЖЕ ОПЛАЧЕННОЙ (право invoices.normal.edit_paid): излишек
 * оплаты уходит в дебиторку поставщику, iiko-документ не трогаем. Форма одна, отличается гейтом,
 * эндпоинтом и предупреждением. Переиспользует строки создания.
 *
 * Чек Кассы правится этой же формой (кнопка «Исправить оплаченную» есть и у него), но устроен
 * иначе: `is_staff` у него всегда false, а «товар vs расход» живёт в СТАТЬЕ каждой строки.
 * Поэтому раскладываем строки по статье (как `CreateChequeDialog`), а не по `is_staff`, и
 * возвращаем на бэк и статью, и пометку возврата — иначе расходы чека уедут в iiko приходом
 * на склад, а возвращённые позиции станут проведёнными. */
export function InvoiceEditDialog({
  invoiceId,
  onOpenChange,
  paid = false,
}: {
  invoiceId: string | null;
  onOpenChange: (open: boolean) => void;
  paid?: boolean;
}) {
  const queryClient = useQueryClient();
  const open = Boolean(invoiceId);

  const detailQuery = useQuery({
    queryKey: ["wh", "invoice", invoiceId],
    queryFn: () => getWarehouseInvoice(invoiceId!),
    enabled: open,
  });
  const productsQuery = useQuery({
    queryKey: ["wh", "products", "goods-all"],
    queryFn: () => getProducts({ type: "GOODS", limit: 2000 }),
    enabled: open,
  });
  const staffArticlesQuery = useQuery({
    queryKey: ["wh", "staff-articles"],
    queryFn: getStaffArticles,
    enabled: open,
    staleTime: 60_000,
  });
  // Полный расходный каталог — для строк чека Кассы («содержание точек», «хозрасходы» и т.п.):
  // блок «Траты на персонал» знает только две статьи, а чек разносится по любой расходной.
  const expenseArticlesQuery = useQuery({
    queryKey: ["cp", "expense-articles"],
    queryFn: getExpenseArticles,
    enabled: open,
    staleTime: 60_000,
  });
  // Реестр контрагентов — для смены поставщика (только неоплаченная накладная).
  const registryQuery = useQuery({
    queryKey: ["cp", "registry", "all"],
    queryFn: () => getRegistry(),
    enabled: open && !paid,
  });
  const detail = detailQuery.data;
  const staffArticles = staffArticlesQuery.data ?? [];
  const expenseArticles = expenseArticlesQuery.data ?? [];
  // Чек Кассы: расходы живут не в «персонале», а в статье строки — форма показывает их
  // отдельным блоком с полным каталогом статей, как в окне создания чека.
  const isCheque = detail?.source === "kassa_cheque";

  const [lines, setLines] = useState<StoreLine[]>([]);
  const [staffLines, setStaffLines] = useState<StaffLine[]>([]);
  const [expenseLines, setExpenseLines] = useState<ExpenseLine[]>([]);
  const [number, setNumber] = useState("");
  const [counterpartyId, setCounterpartyId] = useState("");

  // Инициализируем из накладной при загрузке детали.
  useEffect(() => {
    if (!detail) return;
    setLines(
      detail.lines
        .filter((l) => !l.is_staff && !l.is_expense)
        .map((l) => ({
          key: l.id,
          product_id: l.iiko_product_id,
          name: l.name,
          unit: l.unit,
          quantity: String(l.quantity),
          price: String(l.price),
          vat: l.vat_percent ? String(l.vat_percent) : "",
          amount: String(l.sum),
          // Статью складской строки (у чека — «Оплата поставщикам») возвращаем как есть:
          // по ней бэк отличает товар от расхода.
          ddsArticleId: l.dds_article_id,
          isReturn: Boolean(l.is_return),
        })),
    );
    setStaffLines(
      detail.lines
        .filter((l) => l.is_staff)
        .map((l) => ({
          key: l.id,
          articleId: l.dds_article_id ?? "",
          note: l.name,
          amount: String(l.sum),
        })),
    );
    setExpenseLines(
      detail.lines
        .filter((l) => !l.is_staff && l.is_expense)
        .map((l) => ({
          key: l.id,
          articleId: l.dds_article_id ?? "",
          note: l.name,
          amount: String(l.sum),
          isReturn: Boolean(l.is_return),
        })),
    );
    setNumber(detail.number ?? "");
    setCounterpartyId(detail.counterparty_id);
  }, [detail?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const totals = useMemo(() => {
    // Возвращённые в магазин позиции в сумму документа не идут (он проводится net) —
    // считаем их отдельно, чтобы кассир видел и «как на бумаге», и «к оплате».
    const lineAmount = (l: StoreLine) =>
      l.amount !== "" ? num(l.amount) : num(l.quantity) * num(l.price);
    const store = lines.reduce((s, l) => s + (l.isReturn ? 0 : lineAmount(l)), 0);
    const staff = staffLines.reduce((s, l) => s + num(l.amount), 0);
    const expense = expenseLines.reduce((s, l) => s + (l.isReturn ? 0 : num(l.amount)), 0);
    const returned =
      lines.reduce((s, l) => s + (l.isReturn ? lineAmount(l) : 0), 0) +
      expenseLines.reduce((s, l) => s + (l.isReturn ? num(l.amount) : 0), 0);
    return { store, staff, expense, returned, total: store + staff + expense };
  }, [lines, staffLines, expenseLines]);

  // Правка идёт через позиции — без сохранённых строк (старая iiko-синхронизация) исправлять нечего.
  const hasSavedLines = (detail?.lines?.length ?? 0) > 0;
  const editable = paid
    ? (detail?.payment_status === "paid" || detail?.payment_status === "partially_paid") &&
      !detail?.barter_role &&
      // Черновик блокирует, только пока платёж НЕ финализирован (висит в банке / резерв на Сейфе);
      // банк-оплаченную (draft.status='paid', не через Сейф) править можно — как в гейте бэкенда.
      (!detail?.draft_id ||
        (detail?.draft_status === "paid" && !detail?.draft_pays_via_safe)) &&
      hasSavedLines
    : detail?.payment_status === "unpaid" && !detail?.barter_role;

  // Смена поставщика — только у неоплаченной. Если накладная уже в iiko (external_id), новый
  // контрагент обязан иметь iiko-GUID: иначе Cloud update повиснет, а документ останется на старом
  // поставщике (рассинхрон). Блокируем прямо в форме — тот же гейт дублирует бэкенд (409).
  const supplierChanged = !paid && !!detail && counterpartyId !== detail.counterparty_id;
  const selectedCp = registryQuery.data?.find((i) => i.counterparty_id === counterpartyId);
  const supplierNeedsIikoGuid =
    supplierChanged && !!detail?.external_id && !(selectedCp?.has_iiko_guid ?? false);

  const buildPayload = () => ({
    number: number.trim() || undefined,
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
          // Статья складской строки уезжает обратно как есть: у чека это «Оплата
          // поставщикам», у обычной накладной её нет (null) — товарность от этого не меняется.
          dds_article_id: l.ddsArticleId,
          is_return: l.isReturn,
          // Сумма строки — эталон (как при создании): не пересчитываем кол-во×округлённая цена.
          sum: l.amount !== "" ? num(l.amount) : num(l.quantity) * num(l.price),
        })),
      ...staffLines
        .filter((l) => l.articleId && num(l.amount) > 0)
        .map((l) => ({
          name:
            l.note.trim() || staffArticles.find((a) => a.id === l.articleId)?.name || "Персонал",
          quantity: 1,
          price: num(l.amount),
          iiko_product_id: null,
          vat_percent: null,
          is_staff: true,
          dds_article_id: l.articleId,
          sum: num(l.amount),
        })),
      ...expenseLines
        .filter((l) => l.articleId && num(l.amount) > 0)
        .map((l) => ({
          name: l.note.trim() || expenseArticles.find((a) => a.id === l.articleId)?.name || "Расход",
          quantity: 1,
          price: num(l.amount),
          iiko_product_id: null,
          vat_percent: null,
          // У чека расход помечен статьёй, а не is_staff (иначе он попадёт в «персонал»-часть
          // накладной и разъедется с проводками, которые чек уже сделал по статьям).
          is_staff: false,
          dds_article_id: l.articleId,
          is_return: l.isReturn,
          sum: num(l.amount),
        })),
    ],
  });

  const saveMutation = useMutation({
    mutationFn: () =>
      paid
        ? adjustPaidInvoice(invoiceId!, buildPayload())
        : updateWarehouseInvoice(invoiceId!, {
            ...buildPayload(),
            // Поставщик уходит всегда; бэк сам сверит с текущим и не тронет, если не менялся.
            counterparty_id: counterpartyId || undefined,
          }),
    onSuccess: (updated) => {
      queryClient.setQueryData(["wh", "invoice", invoiceId], updated);
      void queryClient.invalidateQueries({ queryKey: ["wh"] });
      void queryClient.invalidateQueries({ queryKey: ["cp"] });
      void queryClient.invalidateQueries({ queryKey: ["kassa"] });
      if (paid) {
        const moved = (updated as { moved_to_receivable?: number }).moved_to_receivable ?? 0;
        toast.success(
          moved > 0
            ? `Накладная исправлена · ${formatRub(moved)} перенесено в дебиторку`
            : "Накладная исправлена",
        );
      } else {
        toast.success("Накладная обновлена");
      }
      onOpenChange(false);
    },
    onError: (e) => toast.error(apiErrorMessage(e, "Не удалось сохранить накладную")),
  });

  const filled = lines.filter((l) => l.name && num(l.quantity) > 0).length;
  const filledStaff = staffLines.filter((l) => l.articleId && num(l.amount) > 0).length;
  const filledExpense = expenseLines.filter((l) => l.articleId && num(l.amount) > 0).length;
  // Товарная строка без выбора из номенклатуры iiko теряется при выгрузке — блокируем сохранение.
  const goodsMissingProduct = lines.some(
    (l) => l.name.trim() && num(l.quantity) > 0 && !l.product_id,
  );
  // Начатая расходная строка без статьи не сохранится (бэк примет её за товар) — не пускаем.
  const expenseMissingArticle = expenseLines.some((l) => !l.articleId && num(l.amount) > 0);
  // Блок расходов — для чека Кассы (он так и заводится) и для любого документа, где такие
  // строки уже есть. Блок «персонал» — наоборот, для накладной: у чека is_staff всегда false.
  const showExpenseBlock = isCheque || expenseLines.length > 0;
  const showStaffBlock = !isCheque || staffLines.length > 0;
  // Статья новой складской строки — та же, что у уже существующих («Оплата поставщикам» у чека,
  // ничего у накладной): иначе новая позиция чека выпадет из разноса по статьям ДДС.
  const storeArticleId = lines.find((l) => l.ddsArticleId)?.ddsArticleId ?? null;
  const canSave =
    !!editable &&
    filled + filledStaff + filledExpense > 0 &&
    !saveMutation.isPending &&
    !goodsMissingProduct &&
    !expenseMissingArticle &&
    !supplierNeedsIikoGuid;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] max-w-3xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>
            {paid ? "Исправить оплаченную накладную" : "Редактировать накладную"}{" "}
            {detail?.number ? `№${detail.number}` : ""}
          </DialogTitle>
        </DialogHeader>

        {!detail ? (
          <div className="flex justify-center py-8 text-muted-foreground">
            <LoaderCircle className="animate-spin" aria-hidden="true" />
          </div>
        ) : !editable ? (
          <p className="py-4 text-sm text-amber-600">
            {paid
              ? hasSavedLines
                ? "Исправить так можно только оплаченную обычную накладную, не отправленную в банк."
                : "У этой накладной нет сохранённых позиций (старая синхронизация из iiko) — исправить этим способом нельзя."
              : "Редактировать можно только неоплаченную обычную накладную — снимите оплату или используйте «Исправить оплаченную»."}
          </p>
        ) : (
          <div className="grid gap-4">
            {paid ? (
              <p className="rounded-md border border-amber-300 bg-amber-50 p-2.5 text-xs text-amber-800">
                Исправление ОПЛАЧЕННОЙ накладной. Если новая сумма меньше уже оплаченной — излишек
                уйдёт в дебиторку поставщику («поставщик нам должен», гасится будущими поставками).
                iiko-документ не меняется — корректируйте его отдельно (возвратная накладная).
              </p>
            ) : null}
            {!paid ? (
              <div className="grid max-w-md gap-1.5">
                <Label>Поставщик</Label>
                <CounterpartySearch
                  items={registryQuery.data ?? []}
                  value={counterpartyId}
                  onPick={setCounterpartyId}
                />
                {supplierChanged && !supplierNeedsIikoGuid ? (
                  <span className="text-xs text-muted-foreground">
                    Поставщик изменён
                    {detail?.external_id
                      ? " — правка уйдёт в iiko (сменится контрагент документа)"
                      : ""}
                  </span>
                ) : null}
                {supplierNeedsIikoGuid ? (
                  <span className="text-xs text-red-600">
                    У выбранного контрагента нет привязки к iiko, а накладная уже выгружена. Сначала
                    сматчите контрагента с поставщиком iiko — иначе документ останется на старом
                    поставщике.
                  </span>
                ) : null}
              </div>
            ) : null}
            <div className="grid max-w-[220px] gap-1.5">
              <Label htmlFor="invoice-number">Номер</Label>
              <Input
                id="invoice-number"
                value={number}
                onChange={(e) => setNumber(e.target.value)}
              />
              <span className="text-xs text-muted-foreground">
                Уникален на дату — иначе iiko отклонит дубль
              </span>
            </div>
            {/* Закупка на склад */}
            <div className="space-y-2 rounded-md border p-3">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium">Закупка на склад</span>
                <span className="text-xs text-muted-foreground">iiko · приходная накладная</span>
              </div>
              <div className="grid grid-cols-[minmax(0,1fr)_64px_32px_88px_56px_104px_28px] gap-2 text-xs text-muted-foreground">
                <span>Товар</span>
                <span className="text-right">Кол-во</span>
                <span className="text-center">Ед.</span>
                <span className="text-right">Цена/ед.</span>
                <span className="text-right">НДС%</span>
                <span className="text-right">Сумма</span>
                <span />
              </div>
              {lines.map((line) => (
                <ReturnMark key={line.key} isReturn={line.isReturn}>
                  <LineRow
                    line={line}
                    barter={false}
                    products={productsQuery.data ?? []}
                    onChange={(patch) =>
                      setLines((prev) =>
                        prev.map((l) => (l.key === line.key ? { ...l, ...patch } : l)),
                      )
                    }
                    onRemove={() => setLines((prev) => prev.filter((l) => l.key !== line.key))}
                  />
                </ReturnMark>
              ))}
              <Button
                size="sm"
                variant="ghost"
                onClick={() =>
                  setLines((prev) => [
                    ...prev,
                    { ...emptyLine(), ddsArticleId: storeArticleId, isReturn: false },
                  ])
                }
              >
                <Plus size={14} aria-hidden="true" /> товар
              </Button>
            </div>

            {/* Прочие расходы — блок чека Кассы: строка помечена статьёй, а не «персоналом» */}
            {showExpenseBlock ? (
              <div className="space-y-2 rounded-md border p-3">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium">Прочие расходы</span>
                  <span className="text-xs text-muted-foreground">только ДДС · без склада</span>
                </div>
                {expenseLines.map((line) => (
                  <ReturnMark key={line.key} isReturn={line.isReturn}>
                    <StaffLineRow
                      line={line}
                      articles={expenseArticles}
                      onChange={(patch) =>
                        setExpenseLines((prev) =>
                          prev.map((l) => (l.key === line.key ? { ...l, ...patch } : l)),
                        )
                      }
                      onRemove={() =>
                        setExpenseLines((prev) => prev.filter((l) => l.key !== line.key))
                      }
                    />
                  </ReturnMark>
                ))}
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() =>
                    setExpenseLines((prev) => [...prev, { ...emptyStaffLine(), isReturn: false }])
                  }
                >
                  <Plus size={14} aria-hidden="true" /> расход
                </Button>
              </div>
            ) : null}

            {/* Траты на персонал — блок накладной (у чека Кассы is_staff всегда false) */}
            {showStaffBlock ? (
              <div className="space-y-2 rounded-md border border-amber-200 bg-amber-50/40 p-3">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium">Траты на персонал</span>
                  <span className="text-xs text-muted-foreground">только ДДС · не в iiko</span>
                </div>
                {staffLines.map((line) => (
                  <StaffLineRow
                    key={line.key}
                    line={line}
                    articles={staffArticles}
                    onChange={(patch) =>
                      setStaffLines((prev) =>
                        prev.map((l) => (l.key === line.key ? { ...l, ...patch } : l)),
                      )
                    }
                    onRemove={() => setStaffLines((prev) => prev.filter((l) => l.key !== line.key))}
                  />
                ))}
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => setStaffLines((prev) => [...prev, emptyStaffLine()])}
                >
                  <Plus size={14} aria-hidden="true" /> трата
                </Button>
              </div>
            ) : null}

            <div className="text-sm">
              Итого: <span className="font-medium tabular-nums">{formatRub(totals.total)}</span>{" "}
              <span className="text-muted-foreground">
                (склад {formatRub(totals.store)}
                {showExpenseBlock ? ` + расходы ${formatRub(totals.expense)}` : ""}
                {showStaffBlock ? ` + персонал ${formatRub(totals.staff)}` : ""})
              </span>
              {totals.returned > 0 ? (
                <div className="text-xs text-red-600">
                  Возвращено в магазин {formatRub(totals.returned)} — в сумму документа не входит
                </div>
              ) : null}
            </div>
          </div>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Отмена
          </Button>
          <Button disabled={!canSave} onClick={() => saveMutation.mutate()}>
            {saveMutation.isPending ? (
              <LoaderCircle size={16} className="animate-spin" aria-hidden="true" />
            ) : null}
            Сохранить
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
