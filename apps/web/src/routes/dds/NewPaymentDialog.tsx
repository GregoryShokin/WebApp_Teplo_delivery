import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Plus, Trash2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { InlineOptionList, type ComboboxOption } from "@/components/ui/combobox";
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
import { ArticleCombobox } from "@/components/ui-app/ArticleCombobox";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  apiErrorMessage,
  createExpenseCashReserves,
  createNewPaymentExpenseDraft,
  createNewPaymentInternalTransfer,
  createPayrollAdvance,
  getNewPaymentContext,
  getPayrollAdvanceAvailability,
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
  "internal_transfer",
]);

type PaymentRow = {
  key: string;
  articleId: string;
  amount: string;
  counterpartyId: string; // маршрут supplier_prepayment
  employeeId: string; // маршруты employee_advance / employee_loan
  destWalletId: string; // маршрут internal_transfer — счёт-получатель (Сейф/Касса)
  purpose: string; // expense — назначение (опц.); аванс/займ — комментарий (опц.)
  // Форма аванса/займа (маршруты employee_advance / employee_loan) — как в разборе ДДС.
  advanceKind: "advance" | "loan";
  installmentAmount: string; // заём: сумма удержания за период
  recoveryStartDate: string; // заём: с какой выплаты удерживать
  overrideCeiling: boolean; // заём: превысить потолок
};

function normalizeAmount(value: string): string {
  return value.trim().replace(",", ".");
}

function emptyRow(key: string, articleId = ""): PaymentRow {
  return {
    key,
    articleId,
    amount: "",
    counterpartyId: "",
    employeeId: "",
    destWalletId: "",
    purpose: "",
    advanceKind: "advance",
    installmentAmount: "",
    recoveryStartDate: "",
    overrideCeiling: false,
  };
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

  // Сбер — банк-плательщик ТОЛЬКО свободного расхода; накладные/предоплата/авансы остаются
  // в Т-Банке. Поэтому Сбер-счёт можно выбрать лишь когда в конструкторе нет строк не-expense
  // маршрутов; иначе счёт списания форсим на Т-Банк.
  const hasNonExpenseRow = rows.some((row) => {
    const flow = flowOf(row);
    return flow !== null && flow !== "expense";
  });
  const selectedWallet = wallets.find((wallet) => wallet.id === walletId) ?? null;
  const expenseChannel: "bank_draft" | "bank_draft_sber" =
    !hasNonExpenseRow && selectedWallet?.bank_code === "sber" ? "bank_draft_sber" : "bank_draft";
  // Наличный источник (Сейф/Касса): платёж не создаёт банковский черновик, а сразу
  // резервируется на счёте. Доступен для свободного вывода и авансов/займов; предоплата
  // поставщику наличными пока не поддержана (её наличный путь платит сразу, не резервирует).
  const isCashSource = selectedWallet?.kind === "cash";

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
    updateRow(key, {
      articleId,
      counterpartyId: presetCp,
      employeeId: "",
      destWalletId: "",
      purpose: "",
      // Тип по маршруту статьи (переключаемо в под-модалке), поля займа сброшены.
      advanceKind: article?.flow === "employee_loan" ? "loan" : "advance",
      installmentAmount: "",
      recoveryStartDate: "",
      overrideCeiling: false,
    });
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
    if (flow === "internal_transfer") {
      if (!row.destWalletId) {
        return "нужен счёт-получатель";
      }
      const dest = wallets.find((w) => w.id === row.destWalletId);
      // С банковского источника перевод возможен только на Сейф (в кассу — нельзя).
      if (!isCashSource && dest?.location !== "safe") {
        return "с банка — только на Сейф";
      }
      return null;
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
        // Наличный источник → сразу резерв на Сейфе/в Кассе; банк → черновик на карту ИП.
        tasks.push(
          isCashSource
            ? createExpenseCashReserves({ wallet_id: walletId, lines: expenseLines })
            : createNewPaymentExpenseDraft({ lines: expenseLines, channel: expenseChannel }),
        );
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
          const isLoan = row.advanceKind === "loan";
          tasks.push(
            createPayrollAdvance({
              employee_id: row.employeeId,
              amount: normalizeAmount(row.amount),
              kind: row.advanceKind,
              wallet_id: walletId,
              installment_amount:
                isLoan && row.installmentAmount.trim()
                  ? normalizeAmount(row.installmentAmount)
                  : undefined,
              recovery_start_date:
                isLoan && row.recoveryStartDate ? row.recoveryStartDate : undefined,
              override_ceiling: isLoan ? row.overrideCeiling : false,
              comment: row.purpose.trim() ? row.purpose.trim() : null,
            }),
          );
        } else if (flow === "internal_transfer") {
          // Наличный источник → мгновенный перевод; банк → черновик-пополнение Сейфа.
          tasks.push(
            createNewPaymentInternalTransfer({
              source_wallet_id: walletId,
              dest_wallet_id: row.destWalletId,
              amount: rowAmount(row),
              purpose: row.purpose.trim() || null,
            }),
          );
        }
      }
      const results = await Promise.allSettled(tasks);
      return { total: tasks.length, failed: results.filter((r) => r.status === "rejected").length };
    },
    onSuccess: async ({ total: count, failed }) => {
      await invalidate();
      const done = isCashSource ? "Создано" : "Отправлено";
      if (failed === 0) {
        toast.success(
          count === 1
            ? isCashSource
              ? "Резерв создан"
              : "Платёж отправлен в банк"
            : `${done} платежей: ${count}`,
        );
        onOpenChange(false);
        return;
      }
      if (failed < count) {
        toast.warning(`${done} ${count - failed} из ${count}; ошибок: ${failed}`);
        onOpenChange(false);
        return;
      }
      toast.error("Не удалось создать платежи");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось создать платёж")),
  });

  const counterpartyOptions: ComboboxOption[] = counterparties.map((item) => ({
    value: item.counterparty_id,
    label: item.name,
    keywords: item.inn ?? undefined,
  }));

  const modalRow = rows.find((row) => row.key === modalRowKey) ?? null;

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="overflow-visible sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle>Новый платёж</DialogTitle>
            <DialogDescription>
              Соберите платежи построчно: статья ДДС и сумма.{" "}
              {isCashSource
                ? "Наличный счёт — сразу резерв на Сейфе/в Кассе, без банка."
                : "Банковский счёт — создаётся черновик, подтверждение в банке."}
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
                  {wallets.map((wallet) => {
                    const isCash = wallet.kind === "cash";
                    // Наличные (Сейф/Касса) недоступны, если в конструкторе есть предоплата
                    // поставщику (её наличный путь пока не поддержан). Сбер — только всё-expense.
                    const disabled = isCash
                      ? needsCounterparties
                      : wallet.bank_code === "tbank"
                        ? false
                        : wallet.bank_code === "sber"
                          ? hasNonExpenseRow
                          : true;
                    let hint = "";
                    if (isCash) {
                      hint = needsCounterparties
                        ? " — недоступно с предоплатой поставщику"
                        : wallet.location === "kassa"
                          ? " — наличными, резерв в Кассе"
                          : " — наличными, резерв на Сейфе";
                    } else if (wallet.bank_code === "sber") {
                      hint = hasNonExpenseRow
                        ? " — только для свободного расхода"
                        : " — черновик через Сбер (расход на Сейф)";
                    } else if (wallet.bank_code !== "tbank") {
                      hint = " — черновики создаются в Т-Банке";
                    }
                    return (
                      <SelectItem disabled={disabled} key={wallet.id} value={wallet.id}>
                        {wallet.name}
                        {hint}
                      </SelectItem>
                    );
                  })}
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
                const detailText = detailSummary(row, article, counterparties, employees);
                return (
                  <div className="grid gap-1.5" key={row.key}>
                    <div className="grid grid-cols-[minmax(0,1fr)_140px_auto] items-center gap-2">
                      <ArticleCombobox
                        articles={articles}
                        onChange={(value) => handleArticleChange(row.key, value)}
                        value={row.articleId}
                      />
                      <Input
                        aria-label="Сумма"
                        className="text-right tabular-nums"
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
                        title="Убрать строку"
                        type="button"
                        variant="ghost"
                      >
                        <Trash2 className="h-4 w-4" aria-hidden="true" />
                      </Button>
                    </div>
                    {flow === "internal_transfer" ? (
                      <div className="flex items-center gap-2 pl-1">
                        <span className="shrink-0 text-xs text-muted-foreground">
                          На счёт
                        </span>
                        <Select
                          onValueChange={(value) => updateRow(row.key, { destWalletId: value })}
                          value={row.destWalletId}
                        >
                          <SelectTrigger className="h-8 w-56">
                            <SelectValue placeholder="Счёт-получатель" />
                          </SelectTrigger>
                          <SelectContent>
                            {wallets
                              .filter(
                                (w) =>
                                  w.kind === "cash" &&
                                  w.id !== walletId &&
                                  (isCashSource || w.location === "safe"),
                              )
                              .map((w) => (
                                <SelectItem key={w.id} value={w.id}>
                                  {w.name}
                                </SelectItem>
                              ))}
                          </SelectContent>
                        </Select>
                        {missing ? (
                          <span className="text-xs font-medium text-amber-700">{missing}</span>
                        ) : null}
                      </div>
                    ) : flow ? (
                      needsDetails ? (
                        <button
                          className={`self-start pl-1 text-left text-xs underline-offset-2 hover:underline ${
                            missing ? "font-medium text-amber-700" : "text-muted-foreground"
                          }`}
                          onClick={() => setModalRowKey(row.key)}
                          type="button"
                        >
                          {detailText}
                        </button>
                      ) : (
                        <span className="self-start pl-1 text-xs text-muted-foreground">
                          {detailText}
                        </span>
                      )
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
    const kind = row.advanceKind === "loan" ? "Займ" : "Аванс";
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
    : isEmployee
      ? row.advanceKind === "loan"
        ? "Заём сотруднику"
        : "Аванс сотруднику"
      : "Кому платим";
  const expenseCpOptions: ComboboxOption[] = [
    { value: "", label: "Не указан" },
    ...articleCounterparties.map((item) => ({
      value: item.counterparty_id,
      label: item.name,
      keywords: item.inn ?? undefined,
    })),
  ];
  const employeeOptions: ComboboxOption[] = employees.map((employee) => ({
    value: employee.id,
    label: employee.full_name,
  }));
  // «Доступно к авансу» на сегодня (платёж создаётся сейчас) — как в разборе ДДС.
  const availabilityQuery = useQuery({
    queryKey: ["payroll-advance-availability", row.employeeId],
    queryFn: () => getPayrollAdvanceAvailability(row.employeeId),
    enabled: isEmployee && Boolean(row.employeeId),
  });
  const available = availabilityQuery.data?.available ?? 0;
  const amountNumber = Number(normalizeAmount(row.amount));

  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>Заполните данные для этой строки платежа.</DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          {isExpenseCp ? (
            <div className="space-y-1">
              <Label className="text-sm">Контрагент (необязательно)</Label>
              <InlineOptionList
                emptyMessage="Контрагенты не найдены"
                listClassName="max-h-72"
                onChange={(value) => onUpdate({ counterpartyId: value })}
                options={expenseCpOptions}
                searchPlaceholder="Название или ИНН…"
                value={row.counterpartyId}
              />
              <p className="text-xs text-muted-foreground">
                Кому платим по статье «{articleName}» — целёвка на Сейфе будет помечена этим
                контрагентом. Деньги идут на карту ИП → Сейф.
              </p>
            </div>
          ) : null}
          {isPrepayment ? (
            <>
              <div className="space-y-1">
                <Label className="text-sm">Контрагент</Label>
                <InlineOptionList
                  emptyMessage="Контрагенты не найдены"
                  listClassName="max-h-72"
                  onChange={(value) => onUpdate({ counterpartyId: value })}
                  options={counterpartyOptions}
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
              <div className="inline-flex w-fit overflow-hidden rounded-md border">
                <button
                  className={`px-4 py-1.5 text-sm ${
                    row.advanceKind === "advance" ? "bg-primary/10 font-medium text-primary" : ""
                  }`}
                  onClick={() => onUpdate({ advanceKind: "advance" })}
                  type="button"
                >
                  Аванс
                </button>
                <button
                  className={`px-4 py-1.5 text-sm ${
                    row.advanceKind === "loan" ? "bg-primary/10 font-medium text-primary" : ""
                  }`}
                  onClick={() => onUpdate({ advanceKind: "loan" })}
                  type="button"
                >
                  Заём
                </button>
              </div>
              <div className="space-y-1">
                <Label className="text-sm">Сотрудник</Label>
                <InlineOptionList
                  emptyMessage="Сотрудники не найдены"
                  listClassName="max-h-56"
                  onChange={(value) => onUpdate({ employeeId: value })}
                  options={employeeOptions}
                  searchPlaceholder="Поиск по имени…"
                  value={row.employeeId}
                />
              </div>
              {row.employeeId ? (
                <div className="rounded-md border bg-muted/40 p-2.5 text-sm">
                  {availabilityQuery.isLoading ? (
                    "Считаем доступное…"
                  ) : availabilityQuery.data ? (
                    <>
                      Доступно к авансу сегодня: <b>{formatRub(available)}</b>
                    </>
                  ) : (
                    "—"
                  )}
                </div>
              ) : null}
              {row.advanceKind === "advance" &&
              row.employeeId &&
              availabilityQuery.data &&
              amountNumber > available ? (
                <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-800">
                  Больше заработанного ({formatRub(available)}). Остаток удержится из следующей
                  выплаты.
                </div>
              ) : null}
              {row.advanceKind === "loan" ? (
                <>
                  <div className="space-y-1">
                    <Label className="text-sm">Сумма удержания за период, ₽</Label>
                    <Input
                      inputMode="decimal"
                      onChange={(event) => onUpdate({ installmentAmount: event.target.value })}
                      placeholder="Пусто — весь заём одной ведомостью"
                      type="number"
                      value={row.installmentAmount}
                    />
                  </div>
                  <div className="space-y-1">
                    <Label className="text-sm">Удерживать с выплаты</Label>
                    <Input
                      onChange={(event) => onUpdate({ recoveryStartDate: event.target.value })}
                      type="date"
                      value={row.recoveryStartDate}
                    />
                  </div>
                  <label className="flex items-center gap-2 text-sm text-muted-foreground">
                    <input
                      checked={row.overrideCeiling}
                      onChange={(event) => onUpdate({ overrideCeiling: event.target.checked })}
                      type="checkbox"
                    />
                    Превысить потолок займа (подтверждаю)
                  </label>
                </>
              ) : null}
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
                {row.advanceKind === "loan" ? "займ" : "аванс"} активируется при выплате резерва.
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
