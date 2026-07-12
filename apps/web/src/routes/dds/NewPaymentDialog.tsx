import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeftRight,
  ArrowRight,
  Building2,
  HandCoins,
  LoaderCircle,
  Banknote,
  MousePointerClick,
  Plus,
  Receipt,
  Search,
  Trash2,
  User,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { InlineOptionList, type ComboboxOption } from "@/components/ui/combobox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
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
  confirmEmployeePayout,
  createEmployeePayout,
  createExpenseCashReserves,
  createInternalTransfer,
  createNewPaymentExpenseDraft,
  createNewPaymentIncome,
  createNewPaymentInternalTransfer,
  createPayrollAdvance,
  getDdsBankOperations,
  getNewPaymentContext,
  getOnDemandEmployees,
  getPayrollAdvanceAvailability,
  type EmployeePayout,
  type NewPaymentArticle,
  type NewPaymentEmployee,
  type NewPaymentExpenseLine,
  type NewPaymentWallet,
} from "@/lib/api";
import { usePermissions } from "@/lib/permissions";
import { cn } from "@/lib/utils";
import {
  createBankPrepaymentDraft,
  createPrepayment,
  getRegistry,
} from "@/routes/counterparties/api";
import { formatRub } from "@/routes/counterparties/shared";

/**
 * Окно «Новый платёж» — единая точка создания всех исходящих денег («статья решает всё»):
 * слева палитра операций с поиском (расходные статьи + операции сотрудникам + перевод),
 * справа форма выбранной операции. Группа «Расходы» схлопнута до первых статей —
 * раскрывается кнопкой «Ещё…» или поиском, чтобы все группы были видны без скролла.
 *
 * Маршрутизация как раньше — по flow статьи из контекста (services/new_payment.py):
 * expense / supplier_prepayment / employee_advance / employee_loan / internal_transfer /
 * employee_payout. Внутренний перевод имеет фиксированное направление по источнику:
 * банк → Сейф (черновиком), Сейф → Касса, Касса → Сейф. Резервы под цели — это расход
 * с наличного счёта (отдельного «целевого перевода» больше нет).
 */

type OperationKind =
  | "expense"
  | "income"
  | "supplier_prepayment"
  | "employee_advance"
  | "employee_loan"
  | "employee_payout"
  | "transfer_plain";

/** Ключи учёта «в форме есть неотправленный ввод» (см. handleDone). */
type DirtyKind = "expense" | "income" | "prepayment" | "advance" | "payout" | "transfer";

const DIRTY_LABELS: Record<DirtyKind, string> = {
  expense: "строки расхода",
  income: "поступление",
  prepayment: "предоплата поставщику",
  advance: "аванс/заём",
  payout: "выплата долга по ЗП",
  transfer: "перевод",
};

const DIRTY_TO_MODE: Record<DirtyKind, OperationKind> = {
  expense: "expense",
  income: "income",
  prepayment: "supplier_prepayment",
  advance: "employee_advance",
  payout: "employee_payout",
  transfer: "transfer_plain",
};

/** Леджеры палитры: вид деятельности статьи (activity_type каталога ДДС). */
type LedgerKey = "operating" | "financing" | "investing";

const LEDGERS: Array<{ key: LedgerKey; label: string; title: string }> = [
  { key: "operating", label: "Опер.", title: "Операционная деятельность" },
  { key: "financing", label: "Фин.", title: "Финансовая деятельность" },
  { key: "investing", label: "Инвест.", title: "Инвестиционная деятельность" },
];

/** Сколько расходных статей видно в схлопнутой палитре — подобрано так, чтобы все
 *  группы влезали в окно без скролла. */
const EXPENSE_COLLAPSED_COUNT = 5;

type ExpenseRow = {
  key: string;
  articleId: string;
  amount: string;
  purpose: string;
  counterpartyId: string; // «кому платим» — статьи с закреплёнными контрагентами
};

function normalizeAmount(value: string): string {
  return value.trim().replace(",", ".");
}

/** Строка суммы для payload: trim, запятая→точка, все пробелы (включая NBSP) вырезаны.
 *  Валидация и payload обязаны использовать одну и ту же нормализацию. */
function amountStr(value: string): string {
  return normalizeAmount(value).replace(/\s/g, "");
}

function amountOf(value: string): number {
  return Number(amountStr(value));
}

function todayInput(): string {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

function daysAgoInput(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${month}-${day}`;
}

export function NewPaymentDialog({
  open,
  onOpenChange,
  presetArticleCode = null,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Пресет вызывающей стороны: код статьи, с которой открыть окно. */
  presetArticleCode?: string | null;
}) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  // «Создать платёж» (сразу) и «Передать в кассу» двигают живые деньги — уровень
  // права подтверждения оплат, как у выдачи резерва Сейфа.
  const canConfirmPaid = permissions.hasPermission("finance.safe.confirm_paid");

  const [mode, setMode] = useState<OperationKind | null>(null);
  const [search, setSearch] = useState("");
  const [ledger, setLedger] = useState<LedgerKey>("operating");
  // Ключ сессии окна: на каждое открытие формы пересоздаются с чистым состоянием.
  const [sessionKey, setSessionKey] = useState(0);
  // Группа «Расходы» схлопнута — раскрывается кнопкой «Ещё…» или поиском.
  const [expenseExpanded, setExpenseExpanded] = useState(false);
  // Активный шаг привязки банковской операции («Долг по ЗП»): палитра скрыта, чтобы
  // черновик не потерялся от случайного переключения — выйти можно только явно.
  const [linkPending, setLinkPending] = useState(false);
  // Эпоха формы: бамп пересоздаёт отправленную форму, когда окно остаётся открытым.
  const [formEpoch, setFormEpoch] = useState<Partial<Record<DirtyKind, number>>>({});
  // Реестр «в форме есть неотправленный ввод» — гард от молчаливой потери при закрытии
  // окна после успешной отправки другой операции.
  const dirtyRef = useRef<Partial<Record<DirtyKind, boolean>>>({});

  const contextQuery = useQuery({
    queryKey: ["new-payment", "context"],
    queryFn: getNewPaymentContext,
    enabled: open,
  });
  const articles = useMemo(() => contextQuery.data?.articles ?? [], [contextQuery.data]);
  const wallets = useMemo(() => contextQuery.data?.wallets ?? [], [contextQuery.data]);
  const employees = useMemo(() => contextQuery.data?.employees ?? [], [contextQuery.data]);

  const expenseArticles = useMemo(
    () => articles.filter((item) => item.flow === "expense"),
    [articles],
  );
  const incomeArticles = useMemo(
    () => articles.filter((item) => item.flow === "income"),
    [articles],
  );
  const prepaymentArticle = articles.find((item) => item.flow === "supplier_prepayment") ?? null;
  const advanceArticle = articles.find((item) => item.flow === "employee_advance") ?? null;
  const loanArticle = articles.find((item) => item.flow === "employee_loan") ?? null;
  const payoutArticles = useMemo(
    () => articles.filter((item) => item.flow === "employee_payout"),
    [articles],
  );
  const transferArticle = articles.find((item) => item.flow === "internal_transfer") ?? null;
  const kassaWallet =
    wallets.find((wallet) => wallet.kind === "cash" && wallet.location === "kassa") ?? null;

  // Статья поступления живёт в родителе — палитра выбирает её напрямую.
  const [incomeArticleId, setIncomeArticleId] = useState("");

  // --- Строки расхода живут в родителе: палитра добавляет статьи прямо в форму ---
  const rowSeq = useRef(0);
  const nextKey = () => {
    rowSeq.current += 1;
    return `row-${rowSeq.current}`;
  };
  const [expenseRows, setExpenseRows] = useState<ExpenseRow[]>([]);

  function emptyExpenseRow(articleId = "", counterpartyId = ""): ExpenseRow {
    return { key: nextKey(), articleId, amount: "", purpose: "", counterpartyId };
  }
  function updateExpenseRow(key: string, patch: Partial<ExpenseRow>) {
    setExpenseRows((prev) => prev.map((row) => (row.key === key ? { ...row, ...patch } : row)));
  }
  function presetCounterparty(article: NewPaymentArticle | null): string {
    return article && (article.counterparties?.length ?? 0) === 1
      ? article.counterparties![0].counterparty_id
      : "";
  }
  function changeExpenseArticle(key: string, articleId: string) {
    const article = expenseArticles.find((item) => item.id === articleId) ?? null;
    // Смена статьи сбрасывает доп-данные строки — они относились к прежней статье.
    updateExpenseRow(key, {
      articleId,
      counterpartyId: presetCounterparty(article),
      purpose: "",
    });
  }

  /** Клик по статье в палитре: расходная — заполняет пустую строку или добавляет новую
   *  (уже выбранная статья не дублируется); статья-маршрут — переключает операцию. */
  function selectArticle(article: NewPaymentArticle) {
    if (article.flow === "expense") {
      setMode("expense");
      setExpenseRows((prev) => {
        if (prev.some((row) => row.articleId === article.id)) {
          return prev;
        }
        const emptyIndex = prev.findIndex((row) => !row.articleId);
        if (emptyIndex >= 0) {
          return prev.map((row, index) =>
            index === emptyIndex
              ? { ...row, articleId: article.id, counterpartyId: presetCounterparty(article) }
              : row,
          );
        }
        return [...prev, emptyExpenseRow(article.id, presetCounterparty(article))];
      });
      return;
    }
    if (article.flow === "income") {
      setMode("income");
      setIncomeArticleId(article.id);
      return;
    }
    const modeByFlow: Partial<Record<NewPaymentArticle["flow"], OperationKind>> = {
      supplier_prepayment: "supplier_prepayment",
      employee_advance: "employee_advance",
      employee_loan: "employee_loan",
      employee_payout: "employee_payout",
      internal_transfer: "transfer_plain",
    };
    const next = modeByFlow[article.flow];
    if (next) {
      setMode(next);
    }
  }

  // Сброс на каждое открытие — синхронно в рендере (React перерендерит до коммита):
  // без вспышки состояния прошлой сессии и без двойного mount форм.
  const [prevOpen, setPrevOpen] = useState(false);
  if (open !== prevOpen) {
    setPrevOpen(open);
    if (open) {
      setMode(null);
      setSearch("");
      setLedger("operating");
      setIncomeArticleId("");
      rowSeq.current = 0;
      setExpenseRows([emptyExpenseRow()]);
      setSessionKey((key) => key + 1);
      setExpenseExpanded(false);
      setLinkPending(false);
      setFormEpoch({});
      dirtyRef.current = {};
    }
  }

  // Пресет статьи — пока пользователь ничего не выбрал сам.
  useEffect(() => {
    if (!open || !presetArticleCode || mode !== null || articles.length === 0) {
      return;
    }
    const preset = articles.find((item) => item.code === presetArticleCode);
    if (preset) {
      selectArticle(preset);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, presetArticleCode, articles, mode]);

  async function invalidateAll() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["dds"] }),
      queryClient.invalidateQueries({ queryKey: ["cashflow"] }),
      queryClient.invalidateQueries({ queryKey: ["cp"] }),
      queryClient.invalidateQueries({ queryKey: ["payroll"] }),
      queryClient.invalidateQueries({ queryKey: ["payroll-advances"] }),
      queryClient.invalidateQueries({ queryKey: ["new-payment"] }),
      queryClient.invalidateQueries({ queryKey: ["finance-payments"] }),
    ]);
  }

  const setDirty = (kind: DirtyKind, value: boolean) => {
    dirtyRef.current[kind] = value;
  };
  const expenseDirty = () =>
    expenseRows.some((row) => row.articleId && amountOf(row.amount) > 0);

  /** После успешной отправки: закрыть окно, если в других формах нет неотправленного
   *  ввода; иначе — остаться, пересоздать отправленную форму и показать, что осталось. */
  async function handleDone(kind: DirtyKind) {
    dirtyRef.current[kind] = false;
    await invalidateAll();
    const others = (Object.keys(DIRTY_LABELS) as DirtyKind[]).filter((key) =>
      key === kind ? false : key === "expense" ? expenseDirty() : Boolean(dirtyRef.current[key]),
    );
    if (others.length === 0) {
      onOpenChange(false);
      return;
    }
    if (kind === "expense") {
      rowSeq.current = 0;
      setExpenseRows([emptyExpenseRow()]);
    } else {
      setFormEpoch((prev) => ({ ...prev, [kind]: (prev[kind] ?? 0) + 1 }));
    }
    setMode(DIRTY_TO_MODE[others[0]]);
    toast.info(
      `Создано. В окне остался неотправленный ввод: ${others
        .map((key) => DIRTY_LABELS[key])
        .join(", ")} — отправьте или закройте окно.`,
    );
  }
  const close = () => onOpenChange(false);

  // --- Палитра: группы, леджер-фильтр, схлопывание «Расходов», поиск ---
  const q = search.trim().toLowerCase();
  const matches = (label: string) => !q || label.toLowerCase().includes(q);
  // Поиск ищет по всем леджерам; без поиска статьи фильтруются активным леджером.
  const inLedger = (item: NewPaymentArticle) =>
    Boolean(q) || (item.activity ?? "operating") === ledger;

  const usedArticleIds = new Set(expenseRows.map((row) => row.articleId).filter(Boolean));
  // Уже выбранные статьи видимы всегда — сквозь леджер и схлопывание.
  const matchedExpense = expenseArticles.filter(
    (item) => matches(item.name) && (inLedger(item) || usedArticleIds.has(item.id)),
  );
  const matchedIncome = incomeArticles.filter(
    (item) => matches(item.name) && (inLedger(item) || item.id === incomeArticleId),
  );
  // Без поиска и раскрытия — первые N статей + статьи, уже выбранные в строках.
  const visibleExpense =
    q || expenseExpanded
      ? matchedExpense
      : matchedExpense.filter(
          (item, index) => index < EXPENSE_COLLAPSED_COUNT || usedArticleIds.has(item.id),
        );
  const hiddenExpenseCount = matchedExpense.length - visibleExpense.length;

  const showPrepayment =
    prepaymentArticle !== null && matches(prepaymentArticle.name) && inLedger(prepaymentArticle);
  const advanceLabel = "Аванс сотруднику";
  const loanLabel = "Заём сотруднику";
  const payoutLabel = "Долг по ЗП (по требованию)";
  const transferLabel = transferArticle?.name ?? "Внутренний перевод";
  const showAdvance = advanceArticle !== null && matches(advanceLabel);
  const showLoan = loanArticle !== null && matches(loanLabel);
  const showPayout = payoutArticles.length > 0 && matches(payoutLabel);
  const showTransfer = transferArticle !== null && matches(transferLabel);
  const expenseGroupVisible = visibleExpense.length > 0 || showPrepayment;
  const incomeGroupVisible = matchedIncome.length > 0;
  const employeeGroupVisible = showAdvance || showLoan || showPayout;
  const nothingFound =
    !expenseGroupVisible && !incomeGroupVisible && !employeeGroupVisible && !showTransfer;

  const context = contextQuery.data ?? null;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex h-[640px] max-h-[88vh] flex-col gap-0 overflow-hidden p-0 sm:max-w-3xl">
        <DialogHeader className="shrink-0 space-y-0 border-b py-4 pl-6 pr-14">
          <DialogTitle>Новый платёж</DialogTitle>
          <DialogDescription className="mt-0.5">
            {linkPending
              ? "Завершите привязку операции — или «Позже», чтобы привязать при разборе выписки."
              : "Выберите операцию — форма подстроится."}
          </DialogDescription>
        </DialogHeader>

        <div className="flex min-h-0 flex-1">
          {/* Палитра операций; на шаге привязки скрыта — черновик нельзя потерять случайно */}
          {linkPending ? null : (
            <aside className="flex w-52 shrink-0 flex-col border-r sm:w-60">
              <div className="shrink-0 p-2.5 pb-1">
                <div className="relative">
                  <Search
                    className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground"
                    size={14}
                  />
                  <Input
                    value={search}
                    onChange={(event) => setSearch(event.target.value)}
                    placeholder="Статья или операция…"
                    className="h-8 pl-8 text-sm"
                  />
                </div>
                <div className="mt-1.5 flex gap-1">
                  {LEDGERS.map((item) => (
                    <button
                      key={item.key}
                      type="button"
                      title={item.title}
                      onClick={() => setLedger(item.key)}
                      className={cn(
                        "rounded-full border px-2 py-0.5 text-[11px] transition-colors",
                        ledger === item.key && !q
                          ? "border-primary/40 bg-primary/10 font-medium text-primary"
                          : "border-input text-muted-foreground hover:bg-muted",
                        q && "opacity-50",
                      )}
                    >
                      {item.label}
                    </button>
                  ))}
                </div>
              </div>
              <div className="min-h-0 flex-1 overflow-y-auto px-1.5 pb-3">
                {contextQuery.isLoading ? (
                  <div className="flex items-center gap-2 px-2 py-4 text-sm text-muted-foreground">
                    <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden="true" />
                    Загрузка…
                  </div>
                ) : contextQuery.isError ? (
                  <div className="space-y-2 px-2 py-4">
                    <div className="text-sm text-muted-foreground">
                      Не удалось загрузить операции.
                    </div>
                    <Button size="sm" variant="outline" onClick={() => contextQuery.refetch()}>
                      Повторить
                    </Button>
                  </div>
                ) : nothingFound ? (
                  <div className="px-2 py-4 text-sm text-muted-foreground">Ничего не найдено</div>
                ) : (
                  <>
                    {expenseGroupVisible ? (
                      <PaletteGroup title="Расходы">
                        {visibleExpense.map((article) => (
                          <PaletteItem
                            key={article.id}
                            icon={Receipt}
                            label={article.name}
                            active={
                              mode === "expense" &&
                              expenseRows.some((row) => row.articleId === article.id)
                            }
                            onClick={() => selectArticle(article)}
                          />
                        ))}
                        {hiddenExpenseCount > 0 ? (
                          <button
                            type="button"
                            onClick={() => setExpenseExpanded(true)}
                            className="flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-sm text-muted-foreground transition-colors hover:bg-muted"
                          >
                            <Plus size={15} className="shrink-0" aria-hidden="true" />
                            Ещё {hiddenExpenseCount} статей…
                          </button>
                        ) : null}
                        {showPrepayment && prepaymentArticle ? (
                          <PaletteItem
                            icon={Building2}
                            label={prepaymentArticle.name}
                            active={mode === "supplier_prepayment"}
                            onClick={() => selectArticle(prepaymentArticle)}
                          />
                        ) : null}
                      </PaletteGroup>
                    ) : null}
                    {incomeGroupVisible ? (
                      <PaletteGroup title="Поступления">
                        {matchedIncome.map((article) => (
                          <PaletteItem
                            key={article.id}
                            icon={Banknote}
                            label={article.name}
                            active={mode === "income" && incomeArticleId === article.id}
                            onClick={() => selectArticle(article)}
                          />
                        ))}
                      </PaletteGroup>
                    ) : null}
                    {employeeGroupVisible ? (
                      <PaletteGroup title="Сотрудникам">
                        {showAdvance && advanceArticle ? (
                          <PaletteItem
                            icon={HandCoins}
                            label={advanceLabel}
                            active={mode === "employee_advance"}
                            onClick={() => selectArticle(advanceArticle)}
                          />
                        ) : null}
                        {showLoan && loanArticle ? (
                          <PaletteItem
                            icon={HandCoins}
                            label={loanLabel}
                            active={mode === "employee_loan"}
                            onClick={() => selectArticle(loanArticle)}
                          />
                        ) : null}
                        {showPayout ? (
                          <PaletteItem
                            icon={User}
                            label={payoutLabel}
                            active={mode === "employee_payout"}
                            onClick={() => setMode("employee_payout")}
                          />
                        ) : null}
                      </PaletteGroup>
                    ) : null}
                    {showTransfer && transferArticle ? (
                      <PaletteGroup title="Переводы">
                        <PaletteItem
                          icon={ArrowLeftRight}
                          label={transferLabel}
                          active={mode === "transfer_plain"}
                          onClick={() => selectArticle(transferArticle)}
                        />
                      </PaletteGroup>
                    ) : null}
                  </>
                )}
              </div>
            </aside>
          )}

          {/* Форма выбранной операции. Формы смонтированы постоянно (скрыты классом) —
              состояние переживает переключение операций внутри одной сессии окна. */}
          <section className="min-h-0 flex-1 overflow-y-auto p-5">
            {mode === null ? (
              <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
                <div className="flex h-12 w-12 items-center justify-center rounded-full bg-muted">
                  <MousePointerClick className="text-muted-foreground" size={22} />
                </div>
                <div className="max-w-64 text-sm text-muted-foreground">
                  Выберите операцию слева: расходную статью, выплату сотруднику или перевод.
                </div>
              </div>
            ) : null}
            {context ? (
              <>
                <div className={cn(mode === "expense" ? "" : "hidden")}>
                  <ExpenseForm
                    key={`expense-${sessionKey}`}
                    articles={expenseArticles}
                    wallets={wallets}
                    kassaWallet={kassaWallet}
                    canConfirmPaid={canConfirmPaid}
                    rows={expenseRows}
                    onChangeArticle={changeExpenseArticle}
                    onUpdateRow={updateExpenseRow}
                    onAddRow={() => setExpenseRows((prev) => [...prev, emptyExpenseRow()])}
                    onRemoveRow={(key) =>
                      setExpenseRows((prev) =>
                        prev.length <= 1 ? prev : prev.filter((row) => row.key !== key),
                      )
                    }
                    onDone={() => handleDone("expense")}
                    onCancel={close}
                  />
                </div>
                {incomeArticles.length > 0 ? (
                  <div className={cn(mode === "income" ? "" : "hidden")}>
                    <IncomeForm
                      active={mode === "income"}
                      key={`income-${sessionKey}-${formEpoch.income ?? 0}`}
                      articles={incomeArticles}
                      wallets={wallets}
                      articleId={incomeArticleId}
                      onArticleChange={setIncomeArticleId}
                      onDirty={(value) => setDirty("income", value)}
                      onDone={() => handleDone("income")}
                      onCancel={close}
                    />
                  </div>
                ) : null}
                {prepaymentArticle ? (
                  <div className={cn(mode === "supplier_prepayment" ? "" : "hidden")}>
                    <PrepaymentForm
                      active={mode === "supplier_prepayment"}
                      key={`prepayment-${sessionKey}-${formEpoch.prepayment ?? 0}`}
                      article={prepaymentArticle}
                      wallets={wallets}
                      canConfirmPaid={canConfirmPaid}
                      onDirty={(value) => setDirty("prepayment", value)}
                      onDone={() => handleDone("prepayment")}
                      onCancel={close}
                    />
                  </div>
                ) : null}
                {advanceArticle || loanArticle ? (
                  <div
                    className={cn(
                      mode === "employee_advance" || mode === "employee_loan" ? "" : "hidden",
                    )}
                  >
                    <AdvanceForm
                      active={mode === "employee_advance" || mode === "employee_loan"}
                      key={`advance-${sessionKey}-${formEpoch.advance ?? 0}`}
                      kind={mode === "employee_loan" ? "loan" : "advance"}
                      canLoan={loanArticle !== null}
                      onKindChange={(kind) =>
                        setMode(kind === "loan" ? "employee_loan" : "employee_advance")
                      }
                      wallets={wallets}
                      employees={employees}
                      onDirty={(value) => setDirty("advance", value)}
                      onDone={() => handleDone("advance")}
                      onCancel={close}
                    />
                  </div>
                ) : null}
                {payoutArticles.length > 0 ? (
                  <div className={cn(mode === "employee_payout" ? "" : "hidden")}>
                    <PayoutDebtForm
                      active={mode === "employee_payout"}
                      key={`payout-${sessionKey}-${formEpoch.payout ?? 0}`}
                      articles={payoutArticles}
                      wallets={wallets}
                      employees={employees}
                      invalidate={invalidateAll}
                      onDirty={(value) => setDirty("payout", value)}
                      onLinkPending={setLinkPending}
                      onClose={close}
                    />
                  </div>
                ) : null}
                {transferArticle ? (
                  <div className={cn(mode === "transfer_plain" ? "" : "hidden")}>
                    <TransferPlainForm
                      key={`transfer-${sessionKey}-${formEpoch.transfer ?? 0}`}
                      wallets={wallets}
                      onDirty={(value) => setDirty("transfer", value)}
                      onDone={() => handleDone("transfer")}
                      onCancel={close}
                    />
                  </div>
                ) : null}
              </>
            ) : null}
          </section>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// --------------------------------------------------------------------------- //
// Палитра

function PaletteGroup({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mb-1">
      <div className="px-2.5 pb-1 pt-2.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {title}
      </div>
      <div className="space-y-0.5">{children}</div>
    </div>
  );
}

function PaletteItem({
  icon: Icon,
  label,
  active,
  onClick,
}: {
  icon: LucideIcon;
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-sm transition-colors",
        active ? "bg-primary/10 font-medium text-primary" : "hover:bg-muted",
      )}
    >
      <Icon
        size={15}
        className={cn("shrink-0", active ? "text-primary" : "text-muted-foreground")}
        aria-hidden="true"
      />
      <span className="line-clamp-1">{label}</span>
    </button>
  );
}

function FormHeader({ title, description }: { title: string; description: string }) {
  return (
    <div className="mb-4">
      <h3 className="text-base font-semibold">{title}</h3>
      <p className="mt-0.5 text-sm text-muted-foreground">{description}</p>
    </div>
  );
}

function FormFooter({
  cancel,
  cancelLabel = "Отмена",
  submit,
  submitLabel,
  disabled,
  pending,
}: {
  cancel: () => void;
  cancelLabel?: string;
  submit: () => void;
  submitLabel: string;
  disabled: boolean;
  pending: boolean;
}) {
  return (
    <div className="mt-4 flex justify-end gap-2 border-t pt-3.5">
      <Button onClick={cancel} type="button" variant="outline">
        {cancelLabel}
      </Button>
      <Button disabled={disabled || pending} onClick={submit} type="button">
        {pending ? (
          <LoaderCircle className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
        ) : null}
        {submitLabel}
      </Button>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Расход: построчный конструктор (банк → один черновик-транш, наличные → резервы,
// т.е. «целевой» резерв на счёте = расход с наличного счёта)

function ExpenseForm({
  articles,
  wallets,
  kassaWallet,
  canConfirmPaid,
  rows,
  onChangeArticle,
  onUpdateRow,
  onAddRow,
  onRemoveRow,
  onDone,
  onCancel,
}: {
  articles: NewPaymentArticle[];
  wallets: NewPaymentWallet[];
  kassaWallet: NewPaymentWallet | null;
  canConfirmPaid: boolean;
  rows: ExpenseRow[];
  onChangeArticle: (key: string, articleId: string) => void;
  onUpdateRow: (key: string, patch: Partial<ExpenseRow>) => void;
  onAddRow: () => void;
  onRemoveRow: (key: string) => void;
  onDone: () => Promise<void>;
  onCancel: () => void;
}) {
  const [walletId, setWalletId] = useState("");
  const tbankWallet = wallets.find((wallet) => wallet.bank_code === "tbank") ?? null;
  useEffect(() => {
    if (!walletId && tbankWallet) {
      setWalletId(tbankWallet.id);
    }
  }, [walletId, tbankWallet]);

  const selectedWallet = wallets.find((wallet) => wallet.id === walletId) ?? null;
  const isCashSource = selectedWallet?.kind === "cash";
  const channel: "bank_draft" | "bank_draft_sber" =
    selectedWallet?.bank_code === "sber" ? "bank_draft_sber" : "bank_draft";

  const articleById = useMemo(() => {
    const map = new Map<string, NewPaymentArticle>();
    articles.forEach((item) => map.set(item.id, item));
    return map;
  }, [articles]);

  const total = rows.reduce(
    (sum, row) => sum + (amountOf(row.amount) > 0 ? amountOf(row.amount) : 0),
    0,
  );
  const canSubmit =
    Boolean(walletId) &&
    rows.length > 0 &&
    rows.every((row) => row.articleId && amountOf(row.amount) > 0);

  const buildLines = (): NewPaymentExpenseLine[] =>
    rows.map((row) => ({
      article_id: row.articleId,
      amount: amountOf(row.amount),
      purpose: row.purpose.trim(),
      counterparty_id: row.counterpartyId || null,
    }));

  const mutation = useMutation({
    mutationFn: async ({ payNow }: { payNow: boolean }) => {
      const lines = buildLines();
      return isCashSource
        ? createExpenseCashReserves({ wallet_id: walletId, lines, pay_now: payNow })
        : createNewPaymentExpenseDraft({ lines, channel });
    },
    onSuccess: async (_result, { payNow }) => {
      toast.success(
        !isCashSource
          ? "Черновик отправлен в банк"
          : payNow
            ? "Платёж проведён — деньги списаны со счёта"
            : rows.length > 1
              ? "Резервы созданы"
              : "Резерв создан",
      );
      await onDone();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось создать платёж")),
  });

  // «Передать …»: наличные уезжают на другой наличный счёт (Сейф↔Касса) и
  // резервируются там под каждую строку (выдача — из счёта-получателя).
  const isSafeSource = selectedWallet?.kind === "cash" && selectedWallet.location === "safe";
  const safeWallet = wallets.find((w) => w.kind === "cash" && w.location === "safe") ?? null;
  const transferDest = isSafeSource ? kassaWallet : safeWallet;
  const transferLabel = isSafeSource ? "Передать в кассу" : "Передать на Сейф";
  const transferMutation = useMutation({
    mutationFn: () =>
      createInternalTransfer({
        source_wallet_id: walletId,
        dest_wallet_id: transferDest?.id ?? "",
        mode: "targeted",
        lines: buildLines(),
      }),
    onSuccess: async (result) => {
      toast.success(
        `${isSafeSource ? "Передано в кассу" : "Передано на Сейф"}: ${formatRub(result.amount)}, резервов: ${result.reserves}`,
      );
      await onDone();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось передать резерв")),
  });

  const busy = mutation.isPending || transferMutation.isPending;
  const reservePending = mutation.isPending && mutation.variables?.payNow === false;
  const payNowPending = mutation.isPending && mutation.variables?.payNow === true;

  return (
    <div>
      <FormHeader
        title="Свободный расход"
        description="Банковский счёт — черновик на карту ИП → Сейф; наличные — сразу резерв на счёте."
      />
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
                const disabled =
                  !isCash && wallet.bank_code !== "tbank" && wallet.bank_code !== "sber";
                let hint = "";
                if (isCash) {
                  hint =
                    wallet.location === "kassa"
                      ? " — наличными, резерв в Кассе"
                      : " — наличными, резерв на Сейфе";
                } else if (wallet.bank_code === "sber") {
                  hint = " — черновик через Сбер (расход на Сейф)";
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
          {rows.map((row) => {
            const article = articleById.get(row.articleId) ?? null;
            const pinned = article?.counterparties ?? [];
            return (
              <div className="space-y-1.5 rounded-md border p-2.5" key={row.key}>
                <div className="grid grid-cols-[minmax(0,1fr)_130px_auto] items-center gap-2">
                  <ArticleCombobox
                    articles={articles}
                    onChange={(value) => onChangeArticle(row.key, value)}
                    value={row.articleId}
                  />
                  <Input
                    aria-label="Сумма"
                    className="text-right tabular-nums"
                    inputMode="decimal"
                    onChange={(event) => onUpdateRow(row.key, { amount: event.target.value })}
                    placeholder="Сумма, ₽"
                    value={row.amount}
                  />
                  <Button
                    aria-label="Убрать строку"
                    disabled={rows.length <= 1}
                    onClick={() => onRemoveRow(row.key)}
                    size="icon"
                    title="Убрать строку"
                    type="button"
                    variant="ghost"
                  >
                    <Trash2 className="h-4 w-4" aria-hidden="true" />
                  </Button>
                </div>
                <div className={cn("gap-2", pinned.length > 0 ? "grid grid-cols-2" : "")}>
                  <Input
                    className="h-8 text-sm"
                    maxLength={210}
                    onChange={(event) => onUpdateRow(row.key, { purpose: event.target.value })}
                    placeholder="Назначение (необязательно)"
                    value={row.purpose}
                  />
                  {pinned.length > 0 ? (
                    <Select
                      onValueChange={(value) =>
                        onUpdateRow(row.key, { counterpartyId: value === "none" ? "" : value })
                      }
                      value={row.counterpartyId || "none"}
                    >
                      <SelectTrigger className="h-8 text-sm">
                        <SelectValue placeholder="Кому платим" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="none">Кому платим: не указан</SelectItem>
                        {pinned.map((cp) => (
                          <SelectItem key={cp.counterparty_id} value={cp.counterparty_id}>
                            {cp.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  ) : null}
                </div>
              </div>
            );
          })}
          <Button onClick={onAddRow} size="sm" type="button" variant="outline">
            <Plus className="mr-1 h-4 w-4" aria-hidden="true" />
            Добавить строку
          </Button>
        </div>

        {total > 0 ? (
          <div className="flex items-center justify-between border-t pt-2 text-sm">
            <span className="text-muted-foreground">Итого</span>
            <span className="font-medium tabular-nums">{formatRub(total)}</span>
          </div>
        ) : null}

        <div className="rounded-md border bg-muted/40 p-3 text-sm text-muted-foreground">
          {isCashSource
            ? [
                "«Создать резерв» — плановый платёж (выплата позже).",
                canConfirmPaid ? "«Создать платёж» — деньги списываются сразу." : null,
                canConfirmPaid && transferDest
                  ? `«${transferLabel}» — наличные уедут резервом под выдачу.`
                  : null,
              ]
                .filter(Boolean)
                .join(" ")
            : "Строки уйдут одним черновиком на карту ИП → Сейф (разнос по статьям при оплате). Подтверждение в банке."}
        </div>
      </div>
      <div className="mt-4 flex flex-wrap items-center justify-end gap-2 border-t pt-3.5">
        <Button onClick={onCancel} type="button" variant="outline">
          Отмена
        </Button>
        {isCashSource ? (
          <>
            <Button
              disabled={!canSubmit || busy}
              onClick={() => mutation.mutate({ payNow: false })}
              type="button"
              variant={canConfirmPaid ? "outline" : "default"}
            >
              {reservePending ? (
                <LoaderCircle className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
              ) : null}
              Создать резерв
            </Button>
            {transferDest && canConfirmPaid ? (
              <Button
                disabled={!canSubmit || busy}
                onClick={() => transferMutation.mutate()}
                type="button"
                variant="outline"
              >
                {transferMutation.isPending ? (
                  <LoaderCircle className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
                ) : null}
                {transferLabel}
              </Button>
            ) : null}
            {canConfirmPaid ? (
              <Button
                disabled={!canSubmit || busy}
                onClick={() => mutation.mutate({ payNow: true })}
                type="button"
              >
                {payNowPending ? (
                  <LoaderCircle className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
                ) : null}
                Создать платёж
              </Button>
            ) : null}
          </>
        ) : (
          <Button disabled={!canSubmit || busy} onClick={() => mutation.mutate({ payNow: false })} type="button">
            {mutation.isPending ? (
              <LoaderCircle className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
            ) : null}
            Создать черновик
          </Button>
        )}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Предоплата поставщику (только банк: черновик в Т-Банке на счёт поставщика)

function PrepaymentForm({
  active,
  article,
  wallets,
  canConfirmPaid,
  onDirty,
  onDone,
  onCancel,
}: {
  active: boolean;
  article: NewPaymentArticle;
  wallets: NewPaymentWallet[];
  canConfirmPaid: boolean;
  onDirty: (value: boolean) => void;
  onDone: () => Promise<void>;
  onCancel: () => void;
}) {
  const [counterpartyId, setCounterpartyId] = useState("");
  const [amount, setAmount] = useState("");
  const [walletId, setWalletId] = useState("");

  const tbankWallet = wallets.find((wallet) => wallet.bank_code === "tbank") ?? null;
  useEffect(() => {
    if (!walletId && tbankWallet) {
      setWalletId(tbankWallet.id);
    }
  }, [walletId, tbankWallet]);
  const selectedWallet = wallets.find((wallet) => wallet.id === walletId) ?? null;
  const isCashSource = selectedWallet?.kind === "cash";

  const dirty = amountOf(amount) > 0 || Boolean(counterpartyId);
  useEffect(() => {
    onDirty(dirty);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty]);

  const registryQuery = useQuery({
    queryKey: ["cp", "registry"],
    queryFn: () => getRegistry(),
    enabled: active,
  });
  const counterparties = useMemo(
    () =>
      (registryQuery.data ?? [])
        .filter((item) => item.relationship !== "barter")
        .sort((a, b) => a.name.localeCompare(b.name, "ru")),
    [registryQuery.data],
  );
  const options: ComboboxOption[] = counterparties.map((item) => ({
    value: item.counterparty_id,
    label: item.name,
    keywords: item.inn ?? undefined,
  }));
  const selected = counterparties.find((item) => item.counterparty_id === counterpartyId) ?? null;
  const isInformal = selected?.relationship === "informal";
  // Неофициальный поставщик: банк-черновик запрещён, наличными — можно.
  const informalBlocked = isInformal && !isCashSource;
  const canSubmit =
    Boolean(counterpartyId) && Boolean(walletId) && !informalBlocked && amountOf(amount) > 0;

  const mutation = useMutation({
    mutationFn: async () => {
      if (isCashSource) {
        await createPrepayment({
          counterparty_id: counterpartyId,
          wallet_id: walletId,
          amount: amountOf(amount),
          article_id: article.id,
        });
        return;
      }
      await createBankPrepaymentDraft({
        counterparty_id: counterpartyId,
        amount: amountOf(amount),
        article_id: article.id,
      });
    },
    onSuccess: async () => {
      toast.success(
        isCashSource
          ? "Предоплата выплачена — дебиторка создана"
          : "Черновик предоплаты отправлен в банк",
      );
      await onDone();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось создать предоплату")),
  });

  // Резерв предоплаты: плановый платёж; дебиторка возникнет при выплате резерва.
  const prepaymentLine = () => [
    {
      article_id: article.id,
      amount: amountOf(amount),
      purpose: "",
      counterparty_id: counterpartyId,
    },
  ];
  const reserveMutation = useMutation({
    mutationFn: () => createExpenseCashReserves({ wallet_id: walletId, lines: prepaymentLine() }),
    onSuccess: async () => {
      toast.success("Резерв предоплаты создан — дебиторка возникнет при выплате");
      await onDone();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось создать резерв")),
  });
  const isSafeSource = selectedWallet?.kind === "cash" && selectedWallet.location === "safe";
  const transferDest = isSafeSource
    ? (wallets.find((w) => w.kind === "cash" && w.location === "kassa") ?? null)
    : (wallets.find((w) => w.kind === "cash" && w.location === "safe") ?? null);
  const transferLabel = isSafeSource ? "Передать в кассу" : "Передать на Сейф";
  const transferMutation = useMutation({
    mutationFn: () =>
      createInternalTransfer({
        source_wallet_id: walletId,
        dest_wallet_id: transferDest?.id ?? "",
        mode: "targeted",
        lines: prepaymentLine(),
      }),
    onSuccess: async (result) => {
      toast.success(
        `${isSafeSource ? "Передано в кассу" : "Передано на Сейф"}: ${formatRub(result.amount)} — дебиторка возникнет при выдаче`,
      );
      await onDone();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось передать резерв")),
  });
  const busy = mutation.isPending || reserveMutation.isPending || transferMutation.isPending;

  return (
    <div>
      <FormHeader
        title={article.name}
        description="Банк — черновик на счёт поставщика; наличные — выплата сразу. Предоплата (дебиторка) погасится будущими накладными."
      />
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
                const disabled = !isCash && wallet.bank_code !== "tbank";
                let hint = "";
                if (isCash) {
                  hint = " — наличными, выплата сразу";
                } else if (wallet.bank_code === "tbank") {
                  hint = " — черновик в банке";
                } else {
                  hint = " — банковская предоплата только из Т-Банка";
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
        <div className="space-y-1">
          <Label className="text-sm">Контрагент</Label>
          <InlineOptionList
            emptyMessage="Контрагенты не найдены"
            listClassName="max-h-56"
            onChange={setCounterpartyId}
            options={options}
            searchPlaceholder="Название или ИНН…"
            value={counterpartyId}
          />
        </div>
        {informalBlocked ? (
          <p className="text-sm text-amber-600">
            Неофициальный поставщик: предоплата в банк недоступна — выберите наличный счёт
            (Сейф/Касса) или другого контрагента.
          </p>
        ) : null}
        <Label className="block space-y-1">
          <span className="text-sm">Сумма, ₽</span>
          <Input
            className="tabular-nums"
            inputMode="decimal"
            onChange={(event) => setAmount(event.target.value)}
            placeholder="0"
            value={amount}
          />
        </Label>
      </div>
      {isCashSource ? (
        <div className="mt-4 flex flex-wrap items-center justify-end gap-2 border-t pt-3.5">
          <Button onClick={onCancel} type="button" variant="outline">
            Отмена
          </Button>
          <Button
            disabled={!canSubmit || busy}
            onClick={() => reserveMutation.mutate()}
            type="button"
            variant="outline"
          >
            {reserveMutation.isPending ? (
              <LoaderCircle className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
            ) : null}
            Создать резерв
          </Button>
          {transferDest && canConfirmPaid ? (
            <Button
              disabled={!canSubmit || busy}
              onClick={() => transferMutation.mutate()}
              type="button"
              variant="outline"
            >
              {transferMutation.isPending ? (
                <LoaderCircle className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
              ) : null}
              {transferLabel}
            </Button>
          ) : null}
          <Button disabled={!canSubmit || busy} onClick={() => mutation.mutate()} type="button">
            {mutation.isPending ? (
              <LoaderCircle className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
            ) : null}
            Выплатить
          </Button>
        </div>
      ) : (
        <FormFooter
          cancel={onCancel}
          submit={() => mutation.mutate()}
          submitLabel="Отправить в банк"
          disabled={!canSubmit}
          pending={mutation.isPending}
        />
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Поступление: наличный приход на Сейф/в Кассу (проводка сразу; банк — из выписки)

function IncomeForm({
  active,
  articles,
  wallets,
  articleId,
  onArticleChange,
  onDirty,
  onDone,
  onCancel,
}: {
  active: boolean;
  articles: NewPaymentArticle[];
  wallets: NewPaymentWallet[];
  articleId: string;
  onArticleChange: (id: string) => void;
  onDirty: (value: boolean) => void;
  onDone: () => Promise<void>;
  onCancel: () => void;
}) {
  const [walletId, setWalletId] = useState("");
  const [amount, setAmount] = useState("");
  const [purpose, setPurpose] = useState("");
  const [counterpartyId, setCounterpartyId] = useState("");

  const dirty = amountOf(amount) > 0 || Boolean(counterpartyId) || purpose.trim().length > 0;
  useEffect(() => {
    onDirty(dirty);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty]);

  const selectedArticle = articles.find((item) => item.id === articleId) ?? null;
  // Возврат от поставщика гасит его открытые предоплаты — без контрагента не провести.
  const counterpartyRequired = selectedArticle?.code === "vozvrat_pereplaty_ot_postavschikov";

  const cashWallets = wallets.filter((wallet) => wallet.kind === "cash");
  const safeWallet = cashWallets.find((wallet) => wallet.location === "safe") ?? null;
  useEffect(() => {
    if (!walletId && safeWallet) {
      setWalletId(safeWallet.id);
    }
  }, [walletId, safeWallet]);

  // Контрагент (необязательно) — полезен для «Возврата переплаты от поставщиков».
  const registryQuery = useQuery({
    queryKey: ["cp", "registry"],
    queryFn: () => getRegistry(),
    enabled: active,
  });
  const counterpartyOptions: ComboboxOption[] = useMemo(
    () => [
      { value: "", label: "Не указан" },
      ...(registryQuery.data ?? [])
        .filter((item) => item.relationship !== "barter")
        .sort((a, b) => a.name.localeCompare(b.name, "ru"))
        .map((item) => ({
          value: item.counterparty_id,
          label: item.name,
          keywords: item.inn ?? undefined,
        })),
    ],
    [registryQuery.data],
  );

  const canSubmit =
    Boolean(articleId) &&
    Boolean(walletId) &&
    amountOf(amount) > 0 &&
    (!counterpartyRequired || Boolean(counterpartyId));

  const mutation = useMutation({
    mutationFn: () =>
      createNewPaymentIncome({
        wallet_id: walletId,
        lines: [
          {
            article_id: articleId,
            amount: amountOf(amount),
            purpose: purpose.trim(),
            counterparty_id: counterpartyId || null,
          },
        ],
      }),
    onSuccess: async () => {
      toast.success("Поступление проведено");
      await onDone();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось провести поступление")),
  });

  return (
    <div>
      <FormHeader
        title="Поступление"
        description="Наличный приход на Сейф или в Кассу — проводится сразу. Банковские поступления приходят из выписки автоматически."
      />
      <div className="space-y-3">
        <div className="grid grid-cols-2 gap-3">
          <Label className="block space-y-1">
            <span className="text-sm">Статья</span>
            <ArticleCombobox
              articles={articles}
              onChange={onArticleChange}
              placeholder="Статья поступления"
              value={articleId}
            />
          </Label>
          <Label className="block space-y-1">
            <span className="text-sm">Счёт зачисления</span>
            <Select onValueChange={setWalletId} value={walletId}>
              <SelectTrigger>
                <SelectValue placeholder="Выберите счёт" />
              </SelectTrigger>
              <SelectContent>
                {cashWallets.map((wallet) => (
                  <SelectItem key={wallet.id} value={wallet.id}>
                    {wallet.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Label>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <Label className="block space-y-1">
            <span className="text-sm">Сумма, ₽</span>
            <Input
              className="tabular-nums"
              inputMode="decimal"
              onChange={(event) => setAmount(event.target.value)}
              placeholder="0"
              value={amount}
            />
          </Label>
          <Label className="block space-y-1">
            <span className="text-sm">Назначение</span>
            <Input
              maxLength={210}
              onChange={(event) => setPurpose(event.target.value)}
              placeholder="Необязательно"
              value={purpose}
            />
          </Label>
        </div>

        <div className="space-y-1">
          <Label className="text-sm">
            {counterpartyRequired ? "Контрагент" : "Контрагент (необязательно)"}
          </Label>
          <InlineOptionList
            emptyMessage="Контрагенты не найдены"
            listClassName="max-h-40"
            onChange={setCounterpartyId}
            options={counterpartyOptions}
            searchPlaceholder="Название или ИНН…"
            value={counterpartyId}
          />
          {counterpartyRequired ? (
            <p className="text-xs text-muted-foreground">
              Возврат зачтётся в открытые предоплаты этого поставщика (дебиторка уменьшится);
              излишек останется обычным приходом.
            </p>
          ) : null}
        </div>
      </div>
      <FormFooter
        cancel={onCancel}
        submit={() => mutation.mutate()}
        submitLabel="Провести поступление"
        disabled={!canSubmit}
        pending={mutation.isPending}
      />
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Аванс / заём сотруднику

function AdvanceForm({
  active,
  kind,
  canLoan,
  onKindChange,
  wallets,
  employees,
  onDirty,
  onDone,
  onCancel,
}: {
  active: boolean;
  kind: "advance" | "loan";
  canLoan: boolean;
  onKindChange: (kind: "advance" | "loan") => void;
  wallets: NewPaymentWallet[];
  employees: NewPaymentEmployee[];
  onDirty: (value: boolean) => void;
  onDone: () => Promise<void>;
  onCancel: () => void;
}) {
  const [employeeId, setEmployeeId] = useState("");
  const [amount, setAmount] = useState("");
  const [walletId, setWalletId] = useState("");
  const [installmentAmount, setInstallmentAmount] = useState("");
  const [recoveryStartDate, setRecoveryStartDate] = useState("");
  const [overrideCeiling, setOverrideCeiling] = useState(false);
  const [comment, setComment] = useState("");

  const dirty = amountOf(amount) > 0 || Boolean(employeeId);
  useEffect(() => {
    onDirty(dirty);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty]);

  const tbankWallet = wallets.find((wallet) => wallet.bank_code === "tbank") ?? null;
  useEffect(() => {
    if (!walletId && tbankWallet) {
      setWalletId(tbankWallet.id);
    }
  }, [walletId, tbankWallet]);

  const employeeOptions: ComboboxOption[] = employees.map((employee) => ({
    value: employee.id,
    label: employee.full_name,
  }));

  const availabilityQuery = useQuery({
    queryKey: ["payroll-advance-availability", employeeId],
    queryFn: () => getPayrollAdvanceAvailability(employeeId),
    enabled: active && Boolean(employeeId),
  });
  const available = availabilityQuery.data?.available ?? 0;
  const numericAmount = amountOf(amount);
  const overAvailable =
    kind === "advance" &&
    Boolean(employeeId) &&
    availabilityQuery.data != null &&
    numericAmount > available;

  const isLoan = kind === "loan";
  const canSubmit = Boolean(employeeId) && Boolean(walletId) && numericAmount > 0 && !overAvailable;
  const advanceWallet = wallets.find((wallet) => wallet.id === walletId) ?? null;
  // Единые подписи действий окна: Сейф создаёт резерв выдачи, ТК передаёт кассе,
  // банк — черновик (сама механика выдачи — контур авансов, выбирается счётом).
  const advanceSubmitLabel =
    advanceWallet?.kind !== "cash"
      ? "Создать черновик"
      : advanceWallet.location === "kassa"
        ? "Передать в кассу"
        : "Создать резерв";

  const mutation = useMutation({
    mutationFn: () =>
      createPayrollAdvance({
        employee_id: employeeId,
        // Та же нормализация, что в валидации: «5 000,50» → «5000.50».
        amount: amountStr(amount),
        kind,
        wallet_id: walletId,
        installment_amount:
          isLoan && installmentAmount.trim() ? amountStr(installmentAmount) : undefined,
        recovery_start_date: isLoan && recoveryStartDate ? recoveryStartDate : undefined,
        override_ceiling: isLoan ? overrideCeiling : false,
        comment: comment.trim() ? comment.trim() : null,
      }),
    onSuccess: async () => {
      toast.success(isLoan ? "Заём оформлен" : "Аванс оформлен");
      await onDone();
    },
    onError: (error) =>
      toast.error(
        apiErrorMessage(error, isLoan ? "Не удалось оформить заём" : "Не удалось оформить аванс"),
      ),
  });

  return (
    <div>
      <FormHeader
        title={isLoan ? "Заём сотруднику" : "Аванс сотруднику"}
        description={
          isLoan
            ? "Деньги в долг сверх заработанного — гасится удержаниями из ведомостей."
            : "В пределах заработанного на сегодня — удержится из ближайшей ведомости."
        }
      />
      <div className="space-y-3">
        {canLoan ? (
          <div className="inline-flex w-fit overflow-hidden rounded-md border">
            <button
              className={cn(
                "px-4 py-1.5 text-sm",
                !isLoan && "bg-primary/10 font-medium text-primary",
              )}
              onClick={() => onKindChange("advance")}
              type="button"
            >
              Аванс
            </button>
            <button
              className={cn(
                "px-4 py-1.5 text-sm",
                isLoan && "bg-primary/10 font-medium text-primary",
              )}
              onClick={() => onKindChange("loan")}
              type="button"
            >
              Заём
            </button>
          </div>
        ) : null}

        <div className="space-y-1">
          <Label className="text-sm">Сотрудник</Label>
          <InlineOptionList
            emptyMessage="Сотрудники не найдены"
            listClassName="max-h-48"
            onChange={setEmployeeId}
            options={employeeOptions}
            searchPlaceholder="Поиск по имени…"
            value={employeeId}
          />
        </div>

        {employeeId ? (
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
        {overAvailable ? (
          <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-800">
            Больше заработанного ({formatRub(available)}) — аванс не пройдёт.
            {canLoan ? " Переключите тип на «Заём»." : " Такую сумму выдаёт только заём."}
          </div>
        ) : null}

        <div className="grid grid-cols-2 gap-3">
          <Label className="block space-y-1">
            <span className="text-sm">Счёт списания</span>
            <Select onValueChange={setWalletId} value={walletId}>
              <SelectTrigger>
                <SelectValue placeholder="Выберите счёт" />
              </SelectTrigger>
              <SelectContent>
                {wallets.map((wallet) => {
                  const isCash = wallet.kind === "cash";
                  const disabled = !isCash && wallet.bank_code !== "tbank";
                  let hint = "";
                  if (isCash) {
                    hint =
                      wallet.location === "kassa" ? " — выдача через кассу" : " — наличными с Сейфа";
                  } else if (wallet.bank_code !== "tbank") {
                    hint = " — выдача только из Т-Банка или наличными";
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
          <Label className="block space-y-1">
            <span className="text-sm">Сумма, ₽</span>
            <Input
              className="tabular-nums"
              inputMode="decimal"
              onChange={(event) => setAmount(event.target.value)}
              placeholder="0"
              value={amount}
            />
          </Label>
        </div>

        {isLoan ? (
          <>
            <div className="grid grid-cols-2 gap-3">
              <Label className="block space-y-1">
                <span className="text-sm">Удержание за период, ₽</span>
                <Input
                  inputMode="decimal"
                  onChange={(event) => setInstallmentAmount(event.target.value)}
                  placeholder="Пусто — весь заём разом"
                  value={installmentAmount}
                />
              </Label>
              <Label className="block space-y-1">
                <span className="text-sm">Удерживать с выплаты</span>
                <Input
                  onChange={(event) => setRecoveryStartDate(event.target.value)}
                  type="date"
                  value={recoveryStartDate}
                />
              </Label>
            </div>
            <label className="flex items-center gap-2 text-sm text-muted-foreground">
              <input
                checked={overrideCeiling}
                onChange={(event) => setOverrideCeiling(event.target.checked)}
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
            onChange={(event) => setComment(event.target.value)}
            placeholder="Необязательно"
            value={comment}
          />
        </Label>
      </div>
      <FormFooter
        cancel={onCancel}
        submit={() => mutation.mutate()}
        submitLabel={advanceSubmitLabel}
        disabled={!canSubmit}
        pending={mutation.isPending}
      />
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Долг по ЗП («по востребованию»): выплата накопленного долга, двухшаговая при банке

function PayoutDebtForm({
  active,
  articles,
  wallets,
  employees,
  invalidate,
  onDirty,
  onLinkPending,
  onClose,
}: {
  active: boolean;
  articles: NewPaymentArticle[];
  wallets: NewPaymentWallet[];
  employees: NewPaymentEmployee[];
  invalidate: () => Promise<void>;
  onDirty: (value: boolean) => void;
  onLinkPending: (value: boolean) => void;
  onClose: () => void;
}) {
  const [articleId, setArticleId] = useState("");
  const [walletId, setWalletId] = useState("");
  const [employeeId, setEmployeeId] = useState("");
  const [amount, setAmount] = useState("");
  const [payoutDate, setPayoutDate] = useState(todayInput());
  const [note, setNote] = useState("");
  const [step, setStep] = useState<"form" | "link">("form");
  const [pendingPayout, setPendingPayout] = useState<EmployeePayout | null>(null);
  const [operationId, setOperationId] = useState("");

  const dirty = step === "form" && (amountOf(amount) > 0 || Boolean(employeeId));
  useEffect(() => {
    onDirty(dirty);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty]);

  const tbankWallet = wallets.find((wallet) => wallet.bank_code === "tbank") ?? null;
  const selectedWallet = wallets.find((wallet) => wallet.id === walletId) ?? null;
  useEffect(() => {
    if (!walletId && tbankWallet) {
      setWalletId(tbankWallet.id);
    }
  }, [walletId, tbankWallet]);
  useEffect(() => {
    if (!articleId && articles.length > 0) {
      const preset = articles.find((a) => a.code === "zarplata_administrativnogo_personala");
      setArticleId((preset ?? articles[0]).id);
    }
  }, [articleId, articles]);

  // Остаток долга по выбранному сотруднику — из ручки on-demand (начислено − выплачено).
  const debtQuery = useQuery({
    queryKey: ["payroll", "on-demand-debt"],
    queryFn: getOnDemandEmployees,
    enabled: active,
  });
  const debtInfo = debtQuery.data?.find((item) => item.id === employeeId) ?? null;

  const numericAmount = amountOf(amount);
  const canSubmit =
    Boolean(articleId) &&
    Boolean(walletId) &&
    Boolean(employeeId) &&
    Boolean(payoutDate) &&
    numericAmount > 0;

  const createMutation = useMutation({
    mutationFn: () =>
      createEmployeePayout({
        employee_id: employeeId,
        amount: numericAmount,
        wallet_id: walletId,
        payout_date: payoutDate,
        kind: "owner_salary",
        article_id: articleId,
        note: note.trim() ? note.trim() : null,
      }),
    onSuccess: async (payout) => {
      await invalidate();
      if (payout.status === "pending") {
        setPendingPayout(payout);
        setStep("link");
        onLinkPending(true);
        toast.success("Черновик платежа создан — привяжите операцию из выписки");
        return;
      }
      if (payout.status === "failed") {
        toast.error("Банк отклонил черновик платежа");
        onClose();
        return;
      }
      toast.success("Выплата проведена");
      onClose();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось создать выплату")),
  });

  const operationsQuery = useQuery({
    queryKey: ["new-payment", "payout-operations"],
    queryFn: () => getDdsBankOperations({ from: daysAgoInput(45), to: todayInput(), limit: 100 }),
    enabled: active && step === "link",
  });
  const operations = useMemo(
    () =>
      (operationsQuery.data?.items ?? []).filter(
        (op) => op.direction === "out" && op.cashflow_transaction_id === null,
      ),
    [operationsQuery.data],
  );
  const confirmMutation = useMutation({
    mutationFn: () => confirmEmployeePayout(pendingPayout?.id ?? "", operationId),
    onSuccess: async () => {
      await invalidate();
      onLinkPending(false);
      toast.success("Выплата подтверждена и привязана к операции");
      onClose();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось подтвердить выплату")),
  });

  if (step === "link") {
    return (
      <div>
        <FormHeader
          title="Привязать операцию"
          description="Черновик отправлен в банк. Выберите исходящую операцию из выписки, чтобы подтвердить выплату (заведёт перевод на Сейф с резервом)."
        />
        <div className="max-h-[340px] space-y-2 overflow-y-auto">
          {operationsQuery.isLoading ? (
            <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
              <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden="true" />
              Загрузка операций…
            </div>
          ) : operationsQuery.isError ? (
            <div className="py-6 text-sm text-muted-foreground">
              Не удалось загрузить операции из выписки (возможно, нет права просмотра ДДС).
              Черновик сохранён — привязать можно позже при разборе выписки.
            </div>
          ) : operations.length === 0 ? (
            <div className="py-6 text-sm text-muted-foreground">
              Нет несопоставленных исходящих операций за последние 45 дней. Операция появится
              после импорта выписки — привяжите позже.
            </div>
          ) : (
            operations.map((op) => (
              <button
                className={cn(
                  "w-full rounded-md border p-2 text-left text-sm transition hover:bg-muted/50",
                  operationId === op.id ? "border-primary bg-muted/50" : "border-border",
                )}
                key={op.id}
                onClick={() => setOperationId(op.id)}
                type="button"
              >
                <div className="flex justify-between gap-2">
                  <span className="font-medium tabular-nums">{op.amount} ₽</span>
                  <span className="text-muted-foreground">{op.operation_date}</span>
                </div>
                <div className="truncate text-xs text-muted-foreground">
                  {op.counterparty_name_raw || op.payment_purpose || "—"}
                </div>
              </button>
            ))
          )}
        </div>
        <FormFooter
          cancel={onClose}
          cancelLabel="Позже"
          submit={() => confirmMutation.mutate()}
          submitLabel="Подтвердить выплату"
          disabled={!operationId}
          pending={confirmMutation.isPending}
        />
      </div>
    );
  }

  return (
    <div>
      <FormHeader
        title="Долг по ЗП (по требованию)"
        description="Выплата зарплаты, начисленной в долг («по востребованию»). Банковский счёт — черновик с подтверждением по выписке."
      />
      <div className="space-y-3">
        <Label className="block space-y-1">
          <span className="text-sm">Сотрудник</span>
          <Select onValueChange={setEmployeeId} value={employeeId}>
            <SelectTrigger>
              <SelectValue placeholder="Выберите сотрудника" />
            </SelectTrigger>
            <SelectContent>
              {employees.map((employee) => (
                <SelectItem disabled={!employee.on_demand} key={employee.id} value={employee.id}>
                  {employee.full_name}
                  {!employee.on_demand ? " — доступны аванс или займ" : ""}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <span className="text-xs text-muted-foreground">
            Доступно сотрудникам с окладом «по требованию (долг)» — задаётся в «Исходных данных».
          </span>
        </Label>

        {employeeId && debtInfo ? (
          <div className="grid grid-cols-3 gap-2">
            <div className="rounded-md bg-muted/50 px-3 py-2">
              <div className="text-xs text-muted-foreground">Начислено</div>
              <div className="text-sm font-medium tabular-nums">{formatRub(debtInfo.accrued)}</div>
            </div>
            <div className="rounded-md bg-muted/50 px-3 py-2">
              <div className="text-xs text-muted-foreground">Выплачено</div>
              <div className="text-sm font-medium tabular-nums">{formatRub(debtInfo.paid)}</div>
            </div>
            <div className="rounded-md bg-emerald-50 px-3 py-2">
              <div className="flex items-center justify-between text-xs text-emerald-700">
                Остаток
                {debtInfo.debt > 0 ? (
                  <button
                    className="font-medium hover:underline"
                    onClick={() => setAmount(String(debtInfo.debt))}
                    type="button"
                  >
                    взять всё
                  </button>
                ) : null}
              </div>
              <div className="text-sm font-semibold tabular-nums text-emerald-800">
                {formatRub(debtInfo.debt)}
              </div>
            </div>
          </div>
        ) : null}
        {employeeId && debtInfo && numericAmount > debtInfo.debt ? (
          <p className="text-xs text-amber-700">
            Больше остатка долга ({formatRub(debtInfo.debt)}) — долг уйдёт в минус (переплата).
          </p>
        ) : null}

        <div className="grid grid-cols-2 gap-3">
          <Label className="block space-y-1">
            <span className="text-sm">Счёт списания</span>
            <Select onValueChange={setWalletId} value={walletId}>
              <SelectTrigger>
                <SelectValue placeholder="Выберите счёт" />
              </SelectTrigger>
              <SelectContent>
                {wallets.map((wallet) => (
                  <SelectItem key={wallet.id} value={wallet.id}>
                    {wallet.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Label>
          <Label className="block space-y-1">
            <span className="text-sm">Статья ДДС</span>
            <Select onValueChange={setArticleId} value={articleId}>
              <SelectTrigger>
                <SelectValue placeholder="Выберите статью" />
              </SelectTrigger>
              <SelectContent>
                {articles.map((article) => (
                  <SelectItem key={article.id} value={article.id}>
                    {article.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Label>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <Label className="block space-y-1">
            <span className="text-sm">Сумма, ₽</span>
            <Input
              className="tabular-nums"
              inputMode="decimal"
              onChange={(event) => setAmount(event.target.value)}
              placeholder="0"
              value={amount}
            />
          </Label>
          <Label className="block space-y-1">
            <span className="text-sm">Дата выплаты</span>
            <Input
              onChange={(event) => setPayoutDate(event.target.value)}
              type="date"
              value={payoutDate}
            />
          </Label>
        </div>

        <Label className="block space-y-1">
          <span className="text-sm">Комментарий</span>
          <Input
            maxLength={210}
            onChange={(event) => setNote(event.target.value)}
            placeholder="Необязательно"
            value={note}
          />
        </Label>

        <div className="rounded-md border bg-muted/40 p-3 text-sm text-muted-foreground">
          Наличный счёт — проводка сразу. Банковский — черновик на карту сотрудника.
          {selectedWallet && selectedWallet.kind === "bank" && selectedWallet.bank_code !== "tbank" ? (
            <span className="mt-1 block text-xs">
              Счёт не в Т-Банке: черновик в банке не создаётся — подтвердите выплату привязкой
              операции из выписки.
            </span>
          ) : null}
        </div>
      </div>
      <FormFooter
        cancel={onClose}
        submit={() => createMutation.mutate()}
        submitLabel="Создать выплату"
        disabled={!canSubmit}
        pending={createMutation.isPending}
      />
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Внутренний перевод: направление фиксировано источником — банк → Сейф (черновик
// пополнения), Сейф → Касса, Касса → Сейф. Резервы под цели — расход с наличного счёта.

function TransferPlainForm({
  wallets,
  onDirty,
  onDone,
  onCancel,
}: {
  wallets: NewPaymentWallet[];
  onDirty: (value: boolean) => void;
  onDone: () => Promise<void>;
  onCancel: () => void;
}) {
  const [sourceId, setSourceId] = useState("");
  const [amount, setAmount] = useState("");
  const [purpose, setPurpose] = useState("");

  const dirty = amountOf(amount) > 0;
  useEffect(() => {
    onDirty(dirty);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty]);

  const tbankWallet = wallets.find((wallet) => wallet.bank_code === "tbank") ?? null;
  useEffect(() => {
    if (!sourceId && tbankWallet) {
      setSourceId(tbankWallet.id);
    }
  }, [sourceId, tbankWallet]);

  const safeWallet = wallets.find((w) => w.kind === "cash" && w.location === "safe") ?? null;
  const kassaWallet = wallets.find((w) => w.kind === "cash" && w.location === "kassa") ?? null;
  const sourceWallet = wallets.find((wallet) => wallet.id === sourceId) ?? null;
  const isBankSource = sourceWallet?.kind === "bank";
  const isSafeSource = sourceWallet?.kind === "cash" && sourceWallet.location === "safe";
  // Направление фиксировано: банк → Сейф, Сейф → Касса, Касса → Сейф. Внесение
  // Сейф→банк из окна не проводим — банковская нога приходит выпиской, разметка
  // перевода создала бы вторую ногу Сейфа (задвоение).
  const destWallet =
    sourceWallet == null
      ? null
      : sourceWallet.kind === "bank"
        ? safeWallet
        : isSafeSource
          ? kassaWallet
          : safeWallet;

  const canSubmit = Boolean(sourceId) && destWallet !== null && amountOf(amount) > 0;
  const submitLabel = isBankSource
    ? "Создать черновик"
    : isSafeSource
      ? "Перевести в кассу"
      : "Перевести на Сейф";

  const mutation = useMutation({
    mutationFn: () =>
      createNewPaymentInternalTransfer({
        source_wallet_id: sourceId,
        dest_wallet_id: destWallet?.id ?? "",
        amount: amountOf(amount),
        purpose: purpose.trim() || null,
      }),
    onSuccess: async (result) => {
      toast.success(
        result.kind === "draft"
          ? "Черновик пополнения Сейфа отправлен в банк"
          : "Перевод проведён",
      );
      await onDone();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось выполнить перевод")),
  });

  return (
    <div>
      <FormHeader
        title="Внутренний перевод"
        description="Направление фиксировано: банк → Сейф (черновиком), Сейф → Касса, Касса → Сейф."
      />
      <div className="space-y-3">
        <div className="flex items-end gap-2">
          <Label className="flex-1 space-y-1">
            <span className="text-sm">Откуда</span>
            <Select value={sourceId} onValueChange={setSourceId}>
              <SelectTrigger>
                <SelectValue placeholder="Счёт-источник" />
              </SelectTrigger>
              <SelectContent>
                {wallets.map((wallet) => {
                  const isCash = wallet.kind === "cash";
                  const disabled =
                    !isCash && wallet.bank_code !== "tbank" && wallet.bank_code !== "sber";
                  let hint = "";
                  if (!isCash) {
                    hint =
                      wallet.bank_code === "sber"
                        ? " — черновик через Сбер"
                        : wallet.bank_code === "tbank"
                          ? " — черновик пополнения Сейфа"
                          : " — черновики создаются в Т-Банке";
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
          <ArrowRight className="mb-2.5 shrink-0 text-muted-foreground" size={18} />
          <div className="flex-1 space-y-1">
            <span className="text-sm font-medium">Куда</span>
            <div className="flex h-10 items-center rounded-md border bg-muted/40 px-3 text-sm">
              {destWallet?.name ?? "—"}
            </div>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <Label className="block space-y-1">
            <span className="text-sm">Сумма, ₽</span>
            <Input
              className="tabular-nums"
              inputMode="decimal"
              onChange={(event) => setAmount(event.target.value)}
              placeholder="0"
              value={amount}
            />
          </Label>
          <Label className="block space-y-1">
            <span className="text-sm">Назначение</span>
            <Input
              maxLength={210}
              onChange={(event) => setPurpose(event.target.value)}
              placeholder="Необязательно"
              value={purpose}
            />
          </Label>
        </div>

        <div className="rounded-md border bg-muted/40 p-3 text-sm text-muted-foreground">
          {isBankSource
            ? "Деньги придут на Сейф после оплаты черновика в банке. В Кассу — наличными из Сейфа."
            : "Наличный перевод проводится сразу, без резервов. Внесение наличных на банковский счёт проводите разметкой операции из выписки (журнал ДДС)."}
        </div>
      </div>
      <FormFooter
        cancel={onCancel}
        submit={() => mutation.mutate()}
        submitLabel={submitLabel}
        disabled={!canSubmit}
        pending={mutation.isPending}
      />
    </div>
  );
}
