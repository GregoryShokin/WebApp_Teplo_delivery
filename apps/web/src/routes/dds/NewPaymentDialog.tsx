import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeftRight,
  ArrowRight,
  Building2,
  HandCoins,
  LoaderCircle,
  MousePointerClick,
  Plus,
  Receipt,
  Search,
  Target,
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
import { createBankPrepaymentDraft, getRegistry } from "@/routes/counterparties/api";
import { formatRub } from "@/routes/counterparties/shared";

/**
 * Окно «Новый платёж» — единая точка создания всех исходящих денег («статья решает всё»):
 * слева палитра операций с поиском (расходные статьи + операции сотрудникам + переводы),
 * справа форма выбранной операции. Заменяет прежнее меню «Создать» из трёх пунктов:
 * целевой перевод и выплата долга по ЗП («по востребованию») теперь операции палитры.
 *
 * Маршрутизация как раньше — по flow статьи из контекста (services/new_payment.py):
 * expense / supplier_prepayment / employee_advance / employee_loan / internal_transfer /
 * employee_payout; целевой перевод — отдельная операция на праве finance.safe.confirm_paid.
 */

type OperationKind =
  | "expense"
  | "supplier_prepayment"
  | "employee_advance"
  | "employee_loan"
  | "employee_payout"
  | "transfer_plain"
  | "transfer_targeted";

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

function amountOf(value: string): number {
  return Number(normalizeAmount(value).replace(/\s/g, ""));
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
  const canTargeted = permissions.hasPermission("finance.safe.confirm_paid");

  const [mode, setMode] = useState<OperationKind | null>(null);
  const [search, setSearch] = useState("");
  // Ключ сессии окна: на каждое открытие формы пересоздаются с чистым состоянием.
  const [sessionKey, setSessionKey] = useState(0);

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
  const prepaymentArticle = articles.find((item) => item.flow === "supplier_prepayment") ?? null;
  const advanceArticle = articles.find((item) => item.flow === "employee_advance") ?? null;
  const loanArticle = articles.find((item) => item.flow === "employee_loan") ?? null;
  const payoutArticles = useMemo(
    () => articles.filter((item) => item.flow === "employee_payout"),
    [articles],
  );
  const transferArticle = articles.find((item) => item.flow === "internal_transfer") ?? null;

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
    updateExpenseRow(key, { articleId, counterpartyId: presetCounterparty(article) });
  }

  /** Клик по статье в палитре: расходная — заполняет пустую строку или добавляет новую;
   *  статья-маршрут — переключает операцию. */
  function selectArticle(article: NewPaymentArticle) {
    if (article.flow === "expense") {
      setMode("expense");
      setExpenseRows((prev) => {
        const emptyIndex = prev.findIndex((row) => !row.articleId);
        if (emptyIndex >= 0) {
          return prev.map((row, index) =>
            index === emptyIndex
              ? { ...row, articleId: article.id, counterpartyId: presetCounterparty(article) }
              : row,
          );
        }
        if (prev.some((row) => row.articleId === article.id)) {
          return prev;
        }
        return [...prev, emptyExpenseRow(article.id, presetCounterparty(article))];
      });
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

  // Сброс на каждое открытие.
  useEffect(() => {
    if (!open) {
      return;
    }
    setMode(null);
    setSearch("");
    rowSeq.current = 0;
    setExpenseRows([emptyExpenseRow()]);
    setSessionKey((key) => key + 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

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
  async function handleDone() {
    await invalidateAll();
    onOpenChange(false);
  }
  const close = () => onOpenChange(false);

  // --- Палитра: группы и поиск ---
  const q = search.trim().toLowerCase();
  const matches = (label: string) => !q || label.toLowerCase().includes(q);

  const visibleExpense = expenseArticles.filter((item) => matches(item.name));
  const showPrepayment = prepaymentArticle !== null && matches(prepaymentArticle.name);
  const advanceLabel = "Аванс сотруднику";
  const loanLabel = "Заём сотруднику";
  const payoutLabel = "Долг по ЗП (по требованию)";
  const transferLabel = transferArticle?.name ?? "Внутренний перевод";
  const targetedLabel = "Целевой перевод (Сейф↔Касса)";
  const showAdvance = advanceArticle !== null && matches(advanceLabel);
  const showLoan = loanArticle !== null && matches(loanLabel);
  const showPayout = payoutArticles.length > 0 && matches(payoutLabel);
  const showTransfer = transferArticle !== null && matches(transferLabel);
  const showTargeted = canTargeted && matches(targetedLabel);
  const expenseGroupVisible = visibleExpense.length > 0 || showPrepayment;
  const employeeGroupVisible = showAdvance || showLoan || showPayout;
  const transferGroupVisible = showTransfer || showTargeted;
  const nothingFound = !expenseGroupVisible && !employeeGroupVisible && !transferGroupVisible;

  const context = contextQuery.data ?? null;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex h-[640px] max-h-[88vh] flex-col gap-0 overflow-hidden p-0 sm:max-w-3xl">
        <DialogHeader className="shrink-0 space-y-0 border-b py-4 pl-6 pr-14">
          <DialogTitle>Новый платёж</DialogTitle>
          <DialogDescription className="mt-0.5">
            Выберите операцию — форма подстроится.
          </DialogDescription>
        </DialogHeader>

        <div className="flex min-h-0 flex-1">
          {/* Палитра операций */}
          <aside className="flex w-52 shrink-0 flex-col border-r sm:w-60">
            <div className="shrink-0 p-2.5 pb-1.5">
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
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto px-1.5 pb-3">
              {contextQuery.isLoading ? (
                <div className="flex items-center gap-2 px-2 py-4 text-sm text-muted-foreground">
                  <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden="true" />
                  Загрузка…
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
                  {transferGroupVisible ? (
                    <PaletteGroup title="Переводы">
                      {showTransfer && transferArticle ? (
                        <PaletteItem
                          icon={ArrowLeftRight}
                          label={transferLabel}
                          active={mode === "transfer_plain"}
                          onClick={() => selectArticle(transferArticle)}
                        />
                      ) : null}
                      {showTargeted ? (
                        <PaletteItem
                          icon={Target}
                          label={targetedLabel}
                          active={mode === "transfer_targeted"}
                          onClick={() => setMode("transfer_targeted")}
                        />
                      ) : null}
                    </PaletteGroup>
                  ) : null}
                </>
              )}
            </div>
          </aside>

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
                    active={mode === "expense"}
                    key={`expense-${sessionKey}`}
                    articles={expenseArticles}
                    wallets={wallets}
                    rows={expenseRows}
                    onChangeArticle={changeExpenseArticle}
                    onUpdateRow={updateExpenseRow}
                    onAddRow={() => setExpenseRows((prev) => [...prev, emptyExpenseRow()])}
                    onRemoveRow={(key) =>
                      setExpenseRows((prev) =>
                        prev.length <= 1 ? prev : prev.filter((row) => row.key !== key),
                      )
                    }
                    onDone={handleDone}
                    onCancel={close}
                  />
                </div>
                {prepaymentArticle ? (
                  <div className={cn(mode === "supplier_prepayment" ? "" : "hidden")}>
                    <PrepaymentForm
                      active={mode === "supplier_prepayment"}
                      key={`prepayment-${sessionKey}`}
                      article={prepaymentArticle}
                      onDone={handleDone}
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
                      key={`advance-${sessionKey}`}
                      kind={mode === "employee_loan" ? "loan" : "advance"}
                      canLoan={loanArticle !== null}
                      onKindChange={(kind) =>
                        setMode(kind === "loan" ? "employee_loan" : "employee_advance")
                      }
                      wallets={wallets}
                      employees={employees}
                      onDone={handleDone}
                      onCancel={close}
                    />
                  </div>
                ) : null}
                {payoutArticles.length > 0 ? (
                  <div className={cn(mode === "employee_payout" ? "" : "hidden")}>
                    <PayoutDebtForm
                      active={mode === "employee_payout"}
                      key={`payout-${sessionKey}`}
                      articles={payoutArticles}
                      wallets={wallets}
                      employees={employees}
                      invalidate={invalidateAll}
                      onClose={close}
                    />
                  </div>
                ) : null}
                {transferArticle ? (
                  <div className={cn(mode === "transfer_plain" ? "" : "hidden")}>
                    <TransferPlainForm
                      active={mode === "transfer_plain"}
                      key={`transfer-${sessionKey}`}
                      wallets={wallets}
                      onDone={handleDone}
                      onCancel={close}
                    />
                  </div>
                ) : null}
                {canTargeted ? (
                  <div className={cn(mode === "transfer_targeted" ? "" : "hidden")}>
                    <TransferTargetedForm
                      active={mode === "transfer_targeted"}
                      key={`targeted-${sessionKey}`}
                      wallets={wallets}
                      articles={expenseArticles}
                      onDone={handleDone}
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
  submit,
  submitLabel,
  disabled,
  pending,
}: {
  cancel: () => void;
  submit: () => void;
  submitLabel: string;
  disabled: boolean;
  pending: boolean;
}) {
  return (
    <div className="mt-4 flex justify-end gap-2 border-t pt-3.5">
      <Button onClick={cancel} type="button" variant="outline">
        Отмена
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
// Расход: построчный конструктор (банк → один черновик-транш, наличные → резервы)

function ExpenseForm({
  active,
  articles,
  wallets,
  rows,
  onChangeArticle,
  onUpdateRow,
  onAddRow,
  onRemoveRow,
  onDone,
  onCancel,
}: {
  active: boolean;
  articles: NewPaymentArticle[];
  wallets: NewPaymentWallet[];
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

  const total = rows.reduce((sum, row) => sum + (amountOf(row.amount) > 0 ? amountOf(row.amount) : 0), 0);
  const canSubmit =
    Boolean(walletId) &&
    rows.length > 0 &&
    rows.every((row) => row.articleId && amountOf(row.amount) > 0);

  const mutation = useMutation({
    mutationFn: async () => {
      const lines: NewPaymentExpenseLine[] = rows.map((row) => ({
        article_id: row.articleId,
        amount: amountOf(row.amount),
        purpose: row.purpose.trim(),
        counterparty_id: row.counterpartyId || null,
      }));
      return isCashSource
        ? createExpenseCashReserves({ wallet_id: walletId, lines })
        : createNewPaymentExpenseDraft({ lines, channel });
    },
    onSuccess: async () => {
      toast.success(
        isCashSource
          ? rows.length > 1
            ? "Резервы созданы"
            : "Резерв создан"
          : "Черновик отправлен в банк",
      );
      await onDone();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось создать платёж")),
  });

  if (!active && rows.length === 0) {
    return null;
  }

  return (
    <div>
      <FormHeader
        title="Свободный расход"
        description="Банковский счёт — черновик на карту ИП → Сейф; наличные — сразу резерв."
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
                const disabled = !isCash && wallet.bank_code !== "tbank" && wallet.bank_code !== "sber";
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
            ? "Наличный счёт: суммы сразу резервируются на Сейфе/в Кассе — банк не участвует."
            : "Строки уйдут одним черновиком на карту ИП → Сейф (разнос по статьям при оплате). Подтверждение в банке."}
        </div>
      </div>
      <FormFooter
        cancel={onCancel}
        submit={() => mutation.mutate()}
        submitLabel={isCashSource ? "Создать резерв" : "Создать черновик"}
        disabled={!canSubmit}
        pending={mutation.isPending}
      />
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Предоплата поставщику (только банк: черновик в Т-Банке на счёт поставщика)

function PrepaymentForm({
  active,
  article,
  onDone,
  onCancel,
}: {
  active: boolean;
  article: NewPaymentArticle;
  onDone: () => Promise<void>;
  onCancel: () => void;
}) {
  const [counterpartyId, setCounterpartyId] = useState("");
  const [amount, setAmount] = useState("");

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
  const canSubmit = Boolean(counterpartyId) && !isInformal && amountOf(amount) > 0;

  const mutation = useMutation({
    mutationFn: () =>
      createBankPrepaymentDraft({
        counterparty_id: counterpartyId,
        amount: amountOf(amount),
        article_id: article.id,
      }),
    onSuccess: async () => {
      toast.success("Черновик предоплаты отправлен в банк");
      await onDone();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось создать предоплату")),
  });

  return (
    <div>
      <FormHeader
        title={article.name}
        description="Черновик в Т-Банке на счёт поставщика. После оплаты создастся предоплата (дебиторка) — погасится будущими накладными."
      />
      <div className="space-y-3">
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
        {isInformal ? (
          <p className="text-sm text-amber-600">
            Неофициальный поставщик: предоплата в банк недоступна — аванс выдаётся наличными
            через кассу. Выберите другого контрагента.
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
      <FormFooter
        cancel={onCancel}
        submit={() => mutation.mutate()}
        submitLabel="Отправить в банк"
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
  onDone,
  onCancel,
}: {
  active: boolean;
  kind: "advance" | "loan";
  canLoan: boolean;
  onKindChange: (kind: "advance" | "loan") => void;
  wallets: NewPaymentWallet[];
  employees: NewPaymentEmployee[];
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
    kind === "advance" && Boolean(employeeId) && availabilityQuery.data != null && numericAmount > available;

  const isLoan = kind === "loan";
  const canSubmit = Boolean(employeeId) && Boolean(walletId) && numericAmount > 0 && !overAvailable;

  const mutation = useMutation({
    mutationFn: () =>
      createPayrollAdvance({
        employee_id: employeeId,
        amount: normalizeAmount(amount),
        kind,
        wallet_id: walletId,
        installment_amount:
          isLoan && installmentAmount.trim() ? normalizeAmount(installmentAmount) : undefined,
        recovery_start_date: isLoan && recoveryStartDate ? recoveryStartDate : undefined,
        override_ceiling: isLoan ? overrideCeiling : false,
        comment: comment.trim() ? comment.trim() : null,
      }),
    onSuccess: async () => {
      toast.success(isLoan ? "Заём оформлен" : "Аванс оформлен");
      await onDone();
    },
    onError: (error) =>
      toast.error(apiErrorMessage(error, isLoan ? "Не удалось оформить заём" : "Не удалось оформить аванс")),
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
              className={cn("px-4 py-1.5 text-sm", !isLoan && "bg-primary/10 font-medium text-primary")}
              onClick={() => onKindChange("advance")}
              type="button"
            >
              Аванс
            </button>
            <button
              className={cn("px-4 py-1.5 text-sm", isLoan && "bg-primary/10 font-medium text-primary")}
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
                    hint = wallet.location === "kassa" ? " — выдача через кассу" : " — наличными с Сейфа";
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
        submitLabel="Выдать"
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
  onClose,
}: {
  active: boolean;
  articles: NewPaymentArticle[];
  wallets: NewPaymentWallet[];
  employees: NewPaymentEmployee[];
  invalidate: () => Promise<void>;
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
// Внутренний перевод (обычный): наличные — мгновенно, банк → черновик пополнения Сейфа

function TransferPlainForm({
  active: _active,
  wallets,
  onDone,
  onCancel,
}: {
  active: boolean;
  wallets: NewPaymentWallet[];
  onDone: () => Promise<void>;
  onCancel: () => void;
}) {
  const [sourceId, setSourceId] = useState("");
  const [destId, setDestId] = useState("");
  const [amount, setAmount] = useState("");
  const [purpose, setPurpose] = useState("");

  const tbankWallet = wallets.find((wallet) => wallet.bank_code === "tbank") ?? null;
  useEffect(() => {
    if (!sourceId && tbankWallet) {
      setSourceId(tbankWallet.id);
    }
  }, [sourceId, tbankWallet]);

  const sourceWallet = wallets.find((wallet) => wallet.id === sourceId) ?? null;
  const isBankSource = sourceWallet?.kind === "bank";
  const destOptions = wallets.filter(
    (wallet) =>
      wallet.kind === "cash" &&
      wallet.id !== sourceId &&
      (!isBankSource || wallet.location === "safe"),
  );
  // Смена источника может сделать получателя недопустимым (банк → только Сейф).
  useEffect(() => {
    if (destId && !destOptions.some((wallet) => wallet.id === destId)) {
      setDestId("");
    }
    if (!destId && destOptions.length === 1) {
      setDestId(destOptions[0].id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceId, destId, wallets.length]);

  const canSubmit = Boolean(sourceId) && Boolean(destId) && amountOf(amount) > 0;

  const mutation = useMutation({
    mutationFn: () =>
      createNewPaymentInternalTransfer({
        source_wallet_id: sourceId,
        dest_wallet_id: destId,
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
        description="Между своими счетами: наличные — мгновенно, с банка — черновиком пополнения Сейфа."
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
                  const disabled = !isCash && wallet.bank_code !== "tbank" && wallet.bank_code !== "sber";
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
          <Label className="flex-1 space-y-1">
            <span className="text-sm">Куда</span>
            <Select value={destId} onValueChange={setDestId} disabled={!sourceId}>
              <SelectTrigger>
                <SelectValue placeholder="Счёт-получатель" />
              </SelectTrigger>
              <SelectContent>
                {destOptions.map((wallet) => (
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

        <div className="rounded-md border bg-muted/40 p-3 text-sm text-muted-foreground">
          {isBankSource
            ? "С банковского счёта — только на Сейф (в Кассу — наличными из Сейфа). Деньги придут после оплаты черновика."
            : "Наличный перевод проводится сразу, без резервов. Резерв под цели — операция «Целевой перевод»."}
        </div>
      </div>
      <FormFooter
        cancel={onCancel}
        submit={() => mutation.mutate()}
        submitLabel={isBankSource ? "Создать черновик" : "Перевести"}
        disabled={!canSubmit}
        pending={mutation.isPending}
      />
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Целевой перевод (Сейф↔Касса): перемещение наличных с резервированием под цели

type TargetRow = { key: string; articleId: string; amount: string; purpose: string };

function TransferTargetedForm({
  active: _active,
  wallets,
  articles,
  onDone,
  onCancel,
}: {
  active: boolean;
  wallets: NewPaymentWallet[];
  articles: NewPaymentArticle[];
  onDone: () => Promise<void>;
  onCancel: () => void;
}) {
  const rowSeq = useRef(0);
  const newRow = (): TargetRow => {
    rowSeq.current += 1;
    return { key: `t${rowSeq.current}`, articleId: "", amount: "", purpose: "" };
  };
  const [sourceId, setSourceId] = useState("");
  const [destId, setDestId] = useState("");
  const [rows, setRows] = useState<TargetRow[]>(() => [newRow()]);

  const cashWallets = wallets.filter((wallet) => wallet.kind === "cash");
  const destOptions = cashWallets.filter((wallet) => wallet.id !== sourceId);
  useEffect(() => {
    if (destId && destId === sourceId) {
      setDestId("");
    }
  }, [sourceId, destId]);

  function updateRow(key: string, patch: Partial<TargetRow>) {
    setRows((prev) => prev.map((row) => (row.key === key ? { ...row, ...patch } : row)));
  }

  const total = rows.reduce((acc, row) => acc + (amountOf(row.amount) || 0), 0);
  const canSubmit = Boolean(
    sourceId &&
      destId &&
      rows.length > 0 &&
      rows.every((row) => row.articleId && amountOf(row.amount) > 0),
  );

  const mutation = useMutation({
    mutationFn: () => {
      const lines: NewPaymentExpenseLine[] = rows.map((row) => ({
        article_id: row.articleId,
        amount: amountOf(row.amount),
        purpose: row.purpose.trim(),
      }));
      return createInternalTransfer({
        source_wallet_id: sourceId,
        dest_wallet_id: destId,
        mode: "targeted",
        lines,
      });
    },
    onSuccess: async (result) => {
      toast.success(`Переведено ${formatRub(result.amount)}, резервов: ${result.reserves}`);
      await onDone();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось выполнить перевод")),
  });

  return (
    <div>
      <FormHeader
        title="Целевой перевод (Сейф↔Касса)"
        description="Перемещение наличных с резервированием под конкретные цели на счёте-получателе."
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
                {cashWallets.map((wallet) => (
                  <SelectItem key={wallet.id} value={wallet.id}>
                    {wallet.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Label>
          <ArrowRight className="mb-2.5 shrink-0 text-muted-foreground" size={18} />
          <Label className="flex-1 space-y-1">
            <span className="text-sm">Куда</span>
            <Select value={destId} onValueChange={setDestId} disabled={!sourceId}>
              <SelectTrigger>
                <SelectValue placeholder="Счёт-получатель" />
              </SelectTrigger>
              <SelectContent>
                {destOptions.map((wallet) => (
                  <SelectItem key={wallet.id} value={wallet.id}>
                    {wallet.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Label>
        </div>

        <div className="space-y-2">
          <div className="text-sm text-muted-foreground">
            Каждая цель станет резервом на счёте-получателе (появится в «Платежах»).
          </div>
          {rows.map((row) => (
            <div key={row.key} className="flex items-start gap-2">
              <div className="flex-1 space-y-1.5">
                <ArticleCombobox
                  articles={articles}
                  onChange={(value) => updateRow(row.key, { articleId: value })}
                  placeholder="Статья"
                  value={row.articleId}
                />
                <Input
                  value={row.purpose}
                  onChange={(event) => updateRow(row.key, { purpose: event.target.value })}
                  placeholder="Назначение (необязательно)"
                  className="h-9"
                />
              </div>
              <Input
                inputMode="decimal"
                value={row.amount}
                onChange={(event) => updateRow(row.key, { amount: event.target.value })}
                placeholder="0"
                className="h-9 w-28 tabular-nums"
              />
              <Button
                size="icon"
                variant="ghost"
                className="h-9 w-9 shrink-0"
                disabled={rows.length === 1}
                onClick={() => setRows((prev) => prev.filter((r) => r.key !== row.key))}
              >
                <Trash2 size={15} />
              </Button>
            </div>
          ))}
          <div className="flex items-center justify-between">
            <Button size="sm" variant="outline" onClick={() => setRows((prev) => [...prev, newRow()])}>
              <Plus size={15} className="mr-1" /> Добавить цель
            </Button>
            <span className="text-sm text-muted-foreground">
              Итого: <span className="font-semibold text-foreground">{formatRub(total)}</span>
            </span>
          </div>
        </div>
      </div>
      <FormFooter
        cancel={onCancel}
        submit={() => mutation.mutate()}
        submitLabel="Перевести"
        disabled={!canSubmit}
        pending={mutation.isPending}
      />
    </div>
  );
}
