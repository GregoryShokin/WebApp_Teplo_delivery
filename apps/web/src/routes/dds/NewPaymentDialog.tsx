import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Plus, Trash2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Combobox, type ComboboxOption } from "@/components/ui/combobox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  apiErrorMessage,
  createNewPaymentExpenseDraft,
  createPayrollAdvance,
  getNewPaymentContext,
  type NewPaymentArticle,
  type NewPaymentArticleCounterparty,
  type NewPaymentFlow,
} from "@/lib/api";
import { createBankPrepaymentDraft, getRegistry } from "@/routes/counterparties/api";
import { formatRub } from "@/routes/counterparties/shared";

/** Маршруты построчного конструктора: у всех сумма вводится вручную. Оплата накладных
 *  (сумма из накладных) и разовая выплата сотруднику (двухшаговая) — отдельные потоки. */
const ROW_FLOWS: ReadonlySet<NewPaymentFlow> = new Set([
  "expense",
  "supplier_prepayment",
  "employee_advance",
  "employee_loan",
]);

type PaymentRow = {
  key: string;
  articleId: string;
  amount: string;
  counterpartyId: string; // маршрут supplier_prepayment
  employeeId: string; // маршруты employee_advance / employee_loan
  purpose: string; // expense — назначение (опц.); аванс/займ — комментарий (опц.)
};

function normalizeAmount(value: string): string {
  return value.trim().replace(",", ".");
}

function emptyRow(key: string, articleId = ""): PaymentRow {
  return { key, articleId, amount: "", counterpartyId: "", employeeId: "", purpose: "" };
}

/**
 * Окно «Новый платёж» — построчный конструктор: несколько платежей за один раз.
 * Каждая строка — статья ДДС (слева) + сумма (справа). Статьям, которым нужны
 * доп-данные (контрагент для аванса поставщику, сотрудник для аванса/займа), строка
 * подсвечивается жёлтым и открывает модалку по клику «Заполнить». При создании строки
 * свободного вывода объединяются в один транш на Сейф, остальные создают свой платёж.
 */
export function NewPaymentDialog({
  open,
  onOpenChange,
  presetArticleCode = null,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Пресет FAB-пункта: код статьи первой строки (напр. «аванс поставщику»). */
  presetArticleCode?: string | null;
}) {
  const queryClient = useQueryClient();
  const rowSeq = useRef(0);
  const nextKey = () => {
    rowSeq.current += 1;
    return `row-${rowSeq.current}`;
  };

  const [walletId, setWalletId] = useState("");
  const [rows, setRows] = useState<PaymentRow[]>([]);
  const [modalRowKey, setModalRowKey] = useState<string | null>(null);

  const contextQuery = useQuery({
    queryKey: ["new-payment", "context"],
    queryFn: getNewPaymentContext,
    enabled: open,
  });
  const articles = useMemo<NewPaymentArticle[]>(
    () => (contextQuery.data?.articles ?? []).filter((item) => ROW_FLOWS.has(item.flow)),
    [contextQuery.data],
  );
  const wallets = useMemo(() => contextQuery.data?.wallets ?? [], [contextQuery.data]);
  const employees = useMemo(() => contextQuery.data?.employees ?? [], [contextQuery.data]);
  const tbankWallet = wallets.find((wallet) => wallet.bank_code === "tbank") ?? null;

  const articleById = useMemo(() => {
    const map = new Map<string, NewPaymentArticle>();
    articles.forEach((item) => map.set(item.id, item));
    return map;
  }, [articles]);
  const flowOf = (row: PaymentRow): NewPaymentFlow | null =>
    articleById.get(row.articleId)?.flow ?? null;

  const needsCounterparties = rows.some((row) => flowOf(row) === "supplier_prepayment");
  const registryQuery = useQuery({
    queryKey: ["cp", "registry"],
    queryFn: () => getRegistry(),
    enabled: open && needsCounterparties,
  });
  const counterparties = useMemo(
    () =>
      (registryQuery.data ?? [])
        .filter((item) => item.relationship !== "barter")
        .sort((a, b) => a.name.localeCompare(b.name, "ru")),
    [registryQuery.data],
  );

  // Сброс на каждое открытие: одна пустая строка (или пресет-статья).
  useEffect(() => {
    if (!open) {
      return;
    }
    setWalletId("");
    setModalRowKey(null);
    rowSeq.current = 0;
    setRows([emptyRow(nextKey())]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Пресет статьи — как только справочник загрузился (первая строка ещё пуста).
  useEffect(() => {
    if (!open || !presetArticleCode || articles.length === 0) {
      return;
    }
    const preset = articles.find((item) => item.code === presetArticleCode);
    if (!preset) {
      return;
    }
    setRows((prev) =>
      prev.length === 1 && !prev[0].articleId
        ? [{ ...prev[0], articleId: preset.id }]
        : prev,
    );
  }, [open, presetArticleCode, articles]);

  // Счёт списания: дефолт — расчётный Т-Банка (все маршруты создают черновик в Т-Банке).
  useEffect(() => {
    if (open && !walletId && tbankWallet) {
      setWalletId(tbankWallet.id);
    }
  }, [open, walletId, tbankWallet]);

  function updateRow(key: string, patch: Partial<PaymentRow>) {
    setRows((prev) => prev.map((row) => (row.key === key ? { ...row, ...patch } : row)));
  }
  function addRow() {
    setRows((prev) => [...prev, emptyRow(nextKey())]);
  }
  function removeRow(key: string) {
    setRows((prev) => (prev.length <= 1 ? prev : prev.filter((row) => row.key !== key)));
  }
  function handleArticleChange(key: string, articleId: string) {
    // Смена статьи сбрасывает доп-данные строки (у другого маршрута они иные).
    const article = articleById.get(articleId) ?? null;
    // Свободный вывод с единственным привязанным контрагентом — предвыбираем его.
    const presetCp =
      article?.flow === "expense" && article.counterparties?.length === 1
        ? article.counterparties[0].counterparty_id
        : "";
    updateRow(key, { articleId, counterpartyId: presetCp, employeeId: "", purpose: "" });
  }

  const rowAmount = (row: PaymentRow) => Number(normalizeAmount(row.amount));

  /** Чего не хватает строке из доп-данных (для жёлтой подсветки и текста), иначе null. */
  function rowMissing(row: PaymentRow): string | null {
    const flow = flowOf(row);
    if (flow === "supplier_prepayment") {
      if (!row.counterpartyId) {
        return "нужен контрагент";
      }
      const cp = counterparties.find((item) => item.counterparty_id === row.counterpartyId);
      if (cp?.relationship === "informal") {
        return "неофициальный — аванс через кассу";
      }
      return null;
    }
    if (flow === "employee_advance" || flow === "employee_loan") {
      return row.employeeId ? null : "нужен сотрудник";
    }
    return null;
  }
  const rowComplete = (row: PaymentRow) =>
    Boolean(row.articleId) && rowAmount(row) > 0 && rowMissing(row) === null;

  const total = rows.reduce((sum, row) => sum + (rowAmount(row) > 0 ? rowAmount(row) : 0), 0);
  const canSubmit =
    Boolean(walletId) && rows.length > 0 && rows.every(rowComplete);

  async function invalidate() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["dds"] }),
      queryClient.invalidateQueries({ queryKey: ["cashflow"] }),
      queryClient.invalidateQueries({ queryKey: ["cp"] }),
      queryClient.invalidateQueries({ queryKey: ["payroll-advances"] }),
      queryClient.invalidateQueries({ queryKey: ["new-payment"] }),
    ]);
  }

  const createMutation = useMutation({
    mutationFn: async () => {
      const tasks: Array<Promise<unknown>> = [];
      // Строки свободного вывода — одним траншем на Сейф.
      const expenseLines = rows
        .filter((row) => flowOf(row) === "expense")
        .map((row) => ({
          article_id: row.articleId,
          amount: rowAmount(row),
          purpose: row.purpose.trim(),
          counterparty_id: row.counterpartyId || null,
        }));
      if (expenseLines.length > 0) {
        tasks.push(createNewPaymentExpenseDraft({ lines: expenseLines }));
      }
      // Остальные маршруты — каждая строка своим механизмом (разные получатели).
      for (const row of rows) {
        const flow = flowOf(row);
        if (flow === "supplier_prepayment") {
          tasks.push(
            createBankPrepaymentDraft({
              counterparty_id: row.counterpartyId,
              amount: rowAmount(row),
              article_id: row.articleId,
            }),
          );
        } else if (flow === "employee_advance" || flow === "employee_loan") {
          tasks.push(
            createPayrollAdvance({
              employee_id: row.employeeId,
              amount: normalizeAmount(row.amount),
              kind: flow === "employee_loan" ? "loan" : "advance",
              wallet_id: walletId,
              comment: row.purpose.trim() ? row.purpose.trim() : null,
            }),
          );
        }
      }
      const results = await Promise.allSettled(tasks);
      return { total: tasks.length, failed: results.filter((r) => r.status === "rejected").length };
    },
    onSuccess: async ({ total: count, failed }) => {
      await invalidate();
      if (failed === 0) {
        toast.success(count === 1 ? "Платёж отправлен в банк" : `Отправлено платежей: ${count}`);
        onOpenChange(false);
        return;
      }
      if (failed < count) {
        toast.warning(`Отправлено ${count - failed} из ${count}; ошибок: ${failed}`);
        onOpenChange(false);
        return;
      }
      toast.error("Не удалось создать платежи");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось создать платёж")),
  });

  const articleOptions: ComboboxOption[] = articles.map((item) => ({
    value: item.id,
    label: item.name,
  }));
  const counterpartyOptions: ComboboxOption[] = counterparties.map((item) => ({
    value: item.counterparty_id,
    label: item.name,
    keywords: item.inn ?? undefined,
  }));

  const modalRow = rows.find((row) => row.key === modalRowKey) ?? null;

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="sm:max-w-xl">
          <DialogHeader>
            <DialogTitle>Новый платёж</DialogTitle>
            <DialogDescription>
              Соберите платежи построчно: статья ДДС и сумма. Всё создаёт банковский
              черновик — подтверждение всегда в банке.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-3">
            <Label className="block space-y-1">
              <span className="text-sm">Счёт списания</span>
              <Select onValueChange={setWalletId} value={walletId}>
                <SelectTrigger>
                  <SelectValue placeholder="Выберите счёт" />
                </SelectTrigger>
                <SelectContent>
                  {wallets.map((wallet) => (
                    <SelectItem
                      disabled={wallet.bank_code !== "tbank"}
                      key={wallet.id}
                      value={wallet.id}
                    >
                      {wallet.name}
                      {wallet.bank_code !== "tbank" ? " — черновики создаются в Т-Банке" : ""}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Label>

            <div className="space-y-2">
              <span className="text-sm font-medium">Платежи</span>
              {rows.map((row) => {
                const article = articleById.get(row.articleId) ?? null;
                const flow = article?.flow ?? null;
                const missing = rowMissing(row);
                const expenseHasCps =
                  flow === "expense" && (article?.counterparties?.length ?? 0) > 0;
                const needsDetails =
                  flow === "supplier_prepayment" ||
                  flow === "employee_advance" ||
                  flow === "employee_loan" ||
                  expenseHasCps;
                const hasDetail = Boolean(row.counterpartyId || row.employeeId);
                const detailText = detailSummary(row, article, counterparties, employees);
                return (
                  <div
                    className={`rounded-md border p-2 ${
                      missing ? "border-amber-300 bg-amber-50/60" : ""
                    }`}
                    key={row.key}
                  >
                    <div className="flex items-start gap-2">
                      <div className="min-w-0 flex-1">
                        <Combobox
                          emptyMessage="Статьи не найдены"
                          id={`article-${row.key}`}
                          onChange={(value) => handleArticleChange(row.key, value)}
                          options={articleOptions}
                          placeholder="Статья ДДС"
                          searchPlaceholder="Поиск статьи…"
                          value={row.articleId}
                        />
                      </div>
                      <Input
                        aria-label="Сумма"
                        className="w-32"
                        inputMode="decimal"
                        onChange={(event) => updateRow(row.key, { amount: event.target.value })}
                        placeholder="Сумма, ₽"
                        value={row.amount}
                      />
                      <Button
                        aria-label="Убрать строку"
                        disabled={rows.length <= 1}
                        onClick={() => removeRow(row.key)}
                        size="icon"
                        type="button"
                        variant="ghost"
                      >
                        <Trash2 className="h-4 w-4" aria-hidden="true" />
                      </Button>
                    </div>
                    {flow ? (
                      <div className="mt-1 flex items-center justify-between gap-2 pl-1 text-xs">
                        <span className={missing ? "text-amber-700" : "text-muted-foreground"}>
                          {detailText}
                        </span>
                        {needsDetails ? (
                          <Button
                            className="h-6 px-2 text-xs"
                            onClick={() => setModalRowKey(row.key)}
                            size="sm"
                            type="button"
                            variant={missing ? "default" : "outline"}
                          >
                            {missing ? "Заполнить" : hasDetail ? "Изменить" : "Выбрать"}
                          </Button>
                        ) : null}
                      </div>
                    ) : null}
                  </div>
                );
              })}
              <Button onClick={addRow} size="sm" type="button" variant="outline">
                <Plus className="mr-1 h-4 w-4" aria-hidden="true" />
                Добавить платёж
              </Button>
            </div>

            {total > 0 ? (
              <div className="flex items-center justify-between border-t pt-2 text-sm">
                <span className="text-muted-foreground">Итого</span>
                <span className="font-medium tabular-nums">{formatRub(total)}</span>
              </div>
            ) : null}

            <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-800">
              Строки свободного вывода уйдут одним черновиком на карту ИП → Сейф (разнос по
              статьям при выплате). Авансы поставщикам и авансы/займы сотрудникам — отдельными
              черновиками получателям. Подтверждение всегда в банке.
            </div>
          </div>

          <DialogFooter>
            <Button onClick={() => onOpenChange(false)} type="button" variant="outline">
              Отмена
            </Button>
            <Button
              disabled={!canSubmit || createMutation.isPending}
              onClick={() => createMutation.mutate()}
              type="button"
            >
              {createMutation.isPending ? (
                <LoaderCircle className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
              ) : null}
              Создать платёж
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {modalRow ? (
        <RowDetailModal
          articleCounterparties={articleById.get(modalRow.articleId)?.counterparties ?? []}
          articleName={articleById.get(modalRow.articleId)?.name ?? ""}
          counterpartyOptions={counterpartyOptions}
          employees={employees}
          flow={flowOf(modalRow)}
          missing={rowMissing(modalRow)}
          onClose={() => setModalRowKey(null)}
          onUpdate={(patch) => updateRow(modalRow.key, patch)}
          row={modalRow}
        />
      ) : null}
    </>
  );
}

/** Краткая сводка доп-данных строки под статьёй. */
function detailSummary(
  row: PaymentRow,
  article: NewPaymentArticle | null,
  counterparties: Array<{ counterparty_id: string; name: string }>,
  employees: Array<{ id: string; full_name: string }>,
): string {
  const flow = article?.flow ?? null;
  if (flow === "supplier_prepayment") {
    const cp = counterparties.find((item) => item.counterparty_id === row.counterpartyId);
    return cp ? `Контрагент: ${cp.name}` : "нужен контрагент";
  }
  if (flow === "employee_advance" || flow === "employee_loan") {
    const emp = employees.find((item) => item.id === row.employeeId);
    const kind = flow === "employee_loan" ? "Займ" : "Аванс";
    return emp ? `${kind}: ${emp.full_name}` : "нужен сотрудник";
  }
  // Свободный вывод: у статьи есть привязанные контрагенты → выбор «кому платим».
  const pinned = article?.counterparties ?? [];
  if (pinned.length > 0) {
    const cp = pinned.find((item) => item.counterparty_id === row.counterpartyId);
    return cp ? `Контрагент: ${cp.name}` : "контрагент (необязательно)";
  }
  return row.purpose.trim() ? `Назначение: ${row.purpose.trim()}` : "На Сейф целёвкой";
}

function RowDetailModal({
  row,
  flow,
  missing,
  articleName,
  articleCounterparties,
  counterpartyOptions,
  employees,
  onUpdate,
  onClose,
}: {
  row: PaymentRow;
  flow: NewPaymentFlow | null;
  missing: string | null;
  articleName: string;
  articleCounterparties: NewPaymentArticleCounterparty[];
  counterpartyOptions: ComboboxOption[];
  employees: Array<{ id: string; full_name: string }>;
  onUpdate: (patch: Partial<PaymentRow>) => void;
  onClose: () => void;
}) {
  const isPrepayment = flow === "supplier_prepayment";
  const isEmployee = flow === "employee_advance" || flow === "employee_loan";
  const isExpenseCp = flow === "expense" && articleCounterparties.length > 0;
  const title = isPrepayment
    ? "Аванс поставщику"
    : flow === "employee_loan"
      ? "Займ сотруднику"
      : isEmployee
        ? "Аванс сотруднику"
        : "Кому платим";
  const expenseCpOptions: ComboboxOption[] = articleCounterparties.map((item) => ({
    value: item.counterparty_id,
    label: item.name,
    keywords: item.inn ?? undefined,
  }));

  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>Заполните данные для этой строки платежа.</DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          {isExpenseCp ? (
            <div className="space-y-1">
              <Label className="text-sm" htmlFor="row-expense-cp">
                Контрагент (необязательно)
              </Label>
              <Combobox
                emptyMessage="Контрагенты не найдены"
                id="row-expense-cp"
                onChange={(value) => onUpdate({ counterpartyId: value })}
                options={expenseCpOptions}
                placeholder="Выберите контрагента"
                searchPlaceholder="Название или ИНН…"
                value={row.counterpartyId}
              />
              {row.counterpartyId ? (
                <button
                  className="text-xs text-muted-foreground underline"
                  onClick={() => onUpdate({ counterpartyId: "" })}
                  type="button"
                >
                  Убрать контрагента
                </button>
              ) : null}
              <p className="text-xs text-muted-foreground">
                Кому платим по статье «{articleName}» — целёвка на Сейфе будет помечена этим
                контрагентом. Деньги идут на карту ИП → Сейф.
              </p>
            </div>
          ) : null}
          {isPrepayment ? (
            <>
              <div className="space-y-1">
                <Label className="text-sm" htmlFor="row-counterparty">
                  Контрагент
                </Label>
                <Combobox
                  emptyMessage="Контрагенты не найдены"
                  id="row-counterparty"
                  onChange={(value) => onUpdate({ counterpartyId: value })}
                  options={counterpartyOptions}
                  placeholder="Выберите контрагента"
                  searchPlaceholder="Название или ИНН…"
                  value={row.counterpartyId}
                />
              </div>
              {missing && row.counterpartyId ? (
                <p className="text-sm text-amber-600">
                  Неофициальный поставщик: предоплата в банк недоступна — аванс выдаётся
                  наличными через кассу. Выберите другого контрагента или уберите строку.
                </p>
              ) : (
                <p className="text-xs text-muted-foreground">
                  После оплаты создастся предоплата (дебиторка) — погасится будущими накладными.
                </p>
              )}
            </>
          ) : null}

          {isEmployee ? (
            <>
              <Label className="block space-y-1">
                <span className="text-sm">Сотрудник</span>
                <Select
                  onValueChange={(value) => onUpdate({ employeeId: value })}
                  value={row.employeeId}
                >
                  <SelectTrigger>
                    <SelectValue placeholder="Выберите сотрудника" />
                  </SelectTrigger>
                  <SelectContent>
                    {employees.map((employee) => (
                      <SelectItem key={employee.id} value={employee.id}>
                        {employee.full_name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Label>
              <Label className="block space-y-1">
                <span className="text-sm">Комментарий</span>
                <Input
                  maxLength={210}
                  onChange={(event) => onUpdate({ purpose: event.target.value })}
                  placeholder="Необязательно"
                  value={row.purpose}
                />
              </Label>
              <p className="text-xs text-muted-foreground">
                После оплаты на Сейфе появится резерв выдачи —{" "}
                {flow === "employee_loan" ? "займ" : "аванс"} активируется при выплате резерва.
              </p>
            </>
          ) : null}
        </div>

        <DialogFooter>
          <Button onClick={onClose} type="button">
            Готово
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
