import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle } from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
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
  confirmEmployeePayout,
  createEmployeePayout,
  createNewPaymentExpenseDraft,
  createPayrollAdvance,
  getDdsBankOperations,
  getNewPaymentContext,
  type EmployeePayout,
  type NewPaymentFlow,
} from "@/lib/api";
import {
  createBankPrepaymentDraft,
  createDraft,
  getInvoices,
  getRegistry,
} from "@/routes/counterparties/api";
import { formatRub } from "@/routes/counterparties/shared";

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

/** Ввод суммы: запятая → точка, как в остальных денежных формах. */
function normalizeAmount(value: string): string {
  return value.trim().replace(",", ".");
}

/** Плашка «Что произойдёт»: жёлтая — Сейф-маршруты, нейтральная — прямые платежи. */
function PlanCard({ tone, children }: { tone: "warning" | "neutral"; children: ReactNode }) {
  return tone === "warning" ? (
    <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-800">
      {children}
    </div>
  ) : (
    <div className="rounded-md border bg-muted/40 p-3 text-sm text-muted-foreground">
      {children}
    </div>
  );
}

// Маршруты, где черновик создаёт только Т-Банк (свободная трата, аванс/займ,
// предоплата, накладные) — счёт списания зафиксирован на расчётном Т-Банка.
const TBANK_ONLY_FLOWS: ReadonlySet<NewPaymentFlow> = new Set([
  "expense",
  "employee_advance",
  "employee_loan",
  "supplier_prepayment",
  "supplier_invoices",
]);

/**
 * Единое окно «Новый платёж» (FAB): драйвер — статья ДДС, форма достраивает поля
 * и показывает плашку «Что произойдёт» ДО создания. Ничего не изобретает —
 * маршрутизирует на существующие механизмы; все маршруты создают банковский
 * черновик, подтверждение всегда в банке (проводки заводит вебхук-контур).
 */
export function NewPaymentDialog({
  open,
  onOpenChange,
  presetArticleCode = null,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Пресет FAB-пункта: код статьи, предвыбранной при открытии. */
  presetArticleCode?: string | null;
}) {
  const queryClient = useQueryClient();

  const [articleId, setArticleId] = useState("");
  const [walletId, setWalletId] = useState("");
  const [amount, setAmount] = useState("");
  const [purpose, setPurpose] = useState("");
  const [employeeId, setEmployeeId] = useState("");
  const [payoutDate, setPayoutDate] = useState(todayInput());
  const [counterpartyId, setCounterpartyId] = useState("");
  const [invoiceIds, setInvoiceIds] = useState<string[]>([]);
  // Двухшаговый маршрут выплаты сотруднику: после pending-черновика — привязка операции.
  const [step, setStep] = useState<"form" | "link">("form");
  const [pendingPayout, setPendingPayout] = useState<EmployeePayout | null>(null);
  const [operationId, setOperationId] = useState("");

  const contextQuery = useQuery({
    queryKey: ["new-payment", "context"],
    queryFn: getNewPaymentContext,
    enabled: open,
  });
  const articles = useMemo(() => contextQuery.data?.articles ?? [], [contextQuery.data]);
  const wallets = useMemo(() => contextQuery.data?.wallets ?? [], [contextQuery.data]);
  const employees = useMemo(() => contextQuery.data?.employees ?? [], [contextQuery.data]);

  const article = articles.find((item) => item.id === articleId) ?? null;
  const flow: NewPaymentFlow | null = article?.flow ?? null;
  const needsCounterparty = flow === "supplier_prepayment" || flow === "supplier_invoices";

  const registryQuery = useQuery({
    queryKey: ["cp", "registry"],
    queryFn: () => getRegistry(),
    enabled: open && needsCounterparty,
  });
  // Бартер в банк не отправляется (свой контур сведения) — в списках его нет.
  const counterparties = useMemo(
    () =>
      (registryQuery.data ?? [])
        .filter((item) => item.relationship !== "barter")
        .sort((a, b) => a.name.localeCompare(b.name, "ru")),
    [registryQuery.data],
  );
  const counterparty = counterparties.find((item) => item.counterparty_id === counterpartyId) ?? null;
  const isInformal = counterparty?.relationship === "informal";

  const invoicesQuery = useQuery({
    queryKey: ["cp", "invoices", "new-payment", counterpartyId],
    queryFn: () =>
      getInvoices({
        counterparty_id: counterpartyId,
        status: "unpaid,partially_paid",
        in_draft: false,
        direction: "payable",
      }),
    enabled: open && flow === "supplier_invoices" && Boolean(counterpartyId),
  });
  const invoices = useMemo(() => invoicesQuery.data ?? [], [invoicesQuery.data]);
  const selectedInvoices = invoices.filter((invoice) => invoiceIds.includes(invoice.id));
  const invoicesTotal = selectedInvoices.reduce((sum, invoice) => sum + invoice.remaining, 0);

  const tbankWallet = wallets.find((wallet) => wallet.bank_code === "tbank") ?? null;
  const walletRestricted = flow !== null && TBANK_ONLY_FLOWS.has(flow);
  const selectedWallet = wallets.find((wallet) => wallet.id === walletId) ?? null;

  // Сброс формы на каждое открытие (пресет статьи применяется отдельным эффектом ниже).
  useEffect(() => {
    if (open) {
      setArticleId("");
      setWalletId("");
      setAmount("");
      setPurpose("");
      setEmployeeId("");
      setPayoutDate(todayInput());
      setCounterpartyId("");
      setInvoiceIds([]);
      setStep("form");
      setPendingPayout(null);
      setOperationId("");
    }
  }, [open]);

  // Пресет FAB-пункта: предвыбранная статья, пока пользователь не выбрал свою.
  useEffect(() => {
    if (!open || !presetArticleCode || articleId) {
      return;
    }
    const preset = articles.find((item) => item.code === presetArticleCode);
    if (preset) {
      setArticleId(preset.id);
    }
  }, [open, presetArticleCode, articles, articleId]);

  // Счёт списания: дефолт — расчётный Т-Банка; на Т-Банк-only маршрутах Сбер недоступен.
  useEffect(() => {
    if (!open || wallets.length === 0) {
      return;
    }
    if (!walletId && tbankWallet) {
      setWalletId(tbankWallet.id);
      return;
    }
    if (walletRestricted && selectedWallet && selectedWallet.bank_code !== "tbank" && tbankWallet) {
      setWalletId(tbankWallet.id);
    }
  }, [open, wallets, walletId, tbankWallet, walletRestricted, selectedWallet]);

  /** Смена статьи сбрасывает динамические поля — плашка и форма не залипают. */
  function handleArticleChange(nextId: string) {
    setArticleId(nextId);
    setEmployeeId("");
    setCounterpartyId("");
    setInvoiceIds([]);
    const next = articles.find((item) => item.id === nextId) ?? null;
    if (next?.flow === "supplier_invoices") {
      setAmount("");
    }
  }

  function handleCounterpartyChange(nextId: string) {
    setCounterpartyId(nextId);
    setInvoiceIds([]);
  }

  function toggleInvoice(id: string) {
    setInvoiceIds((prev) =>
      prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id],
    );
  }

  async function invalidate() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["dds"] }),
      queryClient.invalidateQueries({ queryKey: ["cashflow"] }),
      queryClient.invalidateQueries({ queryKey: ["cp"] }),
      queryClient.invalidateQueries({ queryKey: ["payroll-advances"] }),
      queryClient.invalidateQueries({ queryKey: ["new-payment"] }),
    ]);
  }

  const numericAmount = Number(normalizeAmount(amount));
  // NaN > 0 === false, поэтому пустая/кривая сумма гасит кнопку через effectiveAmount.
  const effectiveAmount = flow === "supplier_invoices" ? invoicesTotal : numericAmount;

  const createMutation = useMutation({
    mutationFn: async () => {
      switch (flow) {
        case "expense":
          await createNewPaymentExpenseDraft({
            article_id: articleId,
            amount: numericAmount,
            purpose: purpose.trim(),
          });
          return { kind: "expense" as const };
        case "employee_payout": {
          const payout = await createEmployeePayout({
            employee_id: employeeId,
            amount: numericAmount,
            wallet_id: walletId,
            payout_date: payoutDate,
            kind: "owner_salary",
            article_id: articleId,
            note: purpose.trim() ? purpose.trim() : null,
          });
          return { kind: "employee_payout" as const, payout };
        }
        case "employee_advance":
        case "employee_loan":
          await createPayrollAdvance({
            employee_id: employeeId,
            amount: normalizeAmount(amount),
            kind: flow === "employee_loan" ? "loan" : "advance",
            wallet_id: walletId,
            comment: purpose.trim() ? purpose.trim() : null,
          });
          return { kind: "advance" as const };
        case "supplier_prepayment":
          await createBankPrepaymentDraft({
            counterparty_id: counterpartyId,
            amount: numericAmount,
            article_id: articleId,
          });
          return { kind: "supplier_prepayment" as const };
        case "supplier_invoices": {
          const draft = await createDraft(invoiceIds);
          return { kind: "supplier_invoices" as const, viaSafe: draft.pays_via_safe };
        }
        default:
          throw new Error("Выберите статью ДДС");
      }
    },
    onSuccess: async (result) => {
      await invalidate();
      if (result.kind === "employee_payout" && result.payout) {
        if (result.payout.status === "pending") {
          // Черновик создан — предлагаем сразу привязать операцию из выписки.
          setPendingPayout(result.payout);
          setStep("link");
          toast.success("Черновик платежа создан — привяжите операцию из выписки");
          return;
        }
        if (result.payout.status === "failed") {
          toast.error("Банк отклонил черновик платежа");
          onOpenChange(false);
          return;
        }
        toast.success("Выплата проведена");
        onOpenChange(false);
        return;
      }
      const message =
        result.kind === "expense"
          ? "Черновик отправлен в банк — после оплаты появится целёвка на Сейфе"
          : result.kind === "advance"
            ? "Черновик отправлен в банк — резерв выдачи появится на Сейфе после оплаты"
            : result.kind === "supplier_prepayment"
              ? "Платёж отправлен в банк — при оплате появится предоплата"
              : result.viaSafe
                ? "Черновик на карту ИП создан — после оплаты деньги лягут на Сейф целёвкой"
                : "Накладные отправлены в банк одним платежом";
      toast.success(message);
      onOpenChange(false);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось создать платёж"));
    },
  });

  // Шаг «привязать операцию» (маршрут выплаты сотруднику, как в прежнем диалоге).
  const operationsQuery = useQuery({
    queryKey: ["new-payment", "payout-operations"],
    queryFn: () => getDdsBankOperations({ from: daysAgoInput(45), to: todayInput(), limit: 100 }),
    enabled: open && step === "link",
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
      onOpenChange(false);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось подтвердить выплату"));
    },
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

  const purposeAuto = flow === "supplier_prepayment" || flow === "supplier_invoices";
  const informalRefusal = flow === "supplier_prepayment" && isInformal;

  const canSubmit =
    Boolean(article) &&
    Boolean(walletId) &&
    effectiveAmount > 0 &&
    (flow !== "expense" || Boolean(purpose.trim())) &&
    (flow !== "employee_payout" || (Boolean(employeeId) && Boolean(payoutDate))) &&
    (flow !== "employee_advance" || Boolean(employeeId)) &&
    (flow !== "employee_loan" || Boolean(employeeId)) &&
    (flow !== "supplier_prepayment" || (Boolean(counterpartyId) && !isInformal)) &&
    (flow !== "supplier_invoices" || (Boolean(counterpartyId) && invoiceIds.length > 0)) &&
    !createMutation.isPending;

  /** Плашка «Что произойдёт» — маршрут виден до нажатия кнопки. */
  function renderPlan(): ReactNode {
    if (!article || !flow) {
      return (
        <PlanCard tone="neutral">
          Выберите статью ДДС — форма достроит нужные поля и покажет, что произойдёт.
        </PlanCard>
      );
    }
    switch (flow) {
      case "expense":
        return (
          <PlanCard tone="warning">
            Черновик в Т-Банке на карту ИП. После подтверждения оплаты — перевод на Сейф и
            целёвка «{article.name}»: её можно оплатить с Сейфа или передать в кассу на
            выдачу.
          </PlanCard>
        );
      case "employee_payout":
        return (
          <PlanCard tone="neutral">
            Черновик на карту сотрудника. После оплаты выплата свяжется с зарплатной
            ведомостью и уменьшит сумму к выдаче.
            {selectedWallet && selectedWallet.bank_code !== "tbank" ? (
              <span className="mt-1 block text-xs">
                Счёт не в Т-Банке: черновик в банке не создаётся — подтвердите выплату
                привязкой операции из выписки.
              </span>
            ) : null}
          </PlanCard>
        );
      case "employee_advance":
      case "employee_loan":
        return (
          <PlanCard tone="warning">
            Черновик на карту ИП. После оплаты на Сейфе появится резерв выдачи —{" "}
            {flow === "employee_loan" ? "займ" : "аванс"} активируется при выплате резерва.
          </PlanCard>
        );
      case "supplier_prepayment":
        if (isInformal) {
          return (
            <PlanCard tone="warning">
              Неофициальный поставщик: предоплата в банк недоступна — аванс выдаётся
              наличными через кассу.
            </PlanCard>
          );
        }
        return (
          <PlanCard tone="neutral">
            Черновик по реквизитам {counterparty ? `«${counterparty.name}»` : "поставщика"}.
            После оплаты создастся предоплата (дебиторка) — погасится будущими накладными.
          </PlanCard>
        );
      case "supplier_invoices":
        if (!counterparty) {
          return (
            <PlanCard tone="neutral">
              Выберите поставщика и его накладные — они уйдут в банк одним платежом.
            </PlanCard>
          );
        }
        if (isInformal) {
          return (
            <PlanCard tone="warning">
              Черновик в Т-Банке на карту ИП. После подтверждения оплаты деньги лягут на
              Сейф целёвкой «Закуп: {counterparty.name}» — дальше передача в кассу и выдача
              наличными.
            </PlanCard>
          );
        }
        return (
          <PlanCard tone="neutral">
            Черновик по реквизитам «{counterparty.name}». Накладные погасятся при
            подтверждении оплаты банком.
          </PlanCard>
        );
      default:
        return null;
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        {step === "form" ? (
          <>
            <DialogHeader>
              <DialogTitle>Новый платёж</DialogTitle>
              <DialogDescription>
                Статья ДДС определяет маршрут платежа. Все маршруты создают банковский
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
                        disabled={walletRestricted && wallet.bank_code !== "tbank"}
                        key={wallet.id}
                        value={wallet.id}
                      >
                        {wallet.name}
                        {walletRestricted && wallet.bank_code !== "tbank"
                          ? " — черновики создаются в Т-Банке"
                          : ""}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Label>

              <div className="space-y-1">
                <Label className="text-sm" htmlFor="new-payment-article">
                  Статья ДДС
                </Label>
                <Combobox
                  emptyMessage="Статьи не найдены"
                  id="new-payment-article"
                  onChange={handleArticleChange}
                  options={articleOptions}
                  placeholder="Выберите статью"
                  searchPlaceholder="Поиск статьи…"
                  value={articleId}
                />
              </div>

              {flow === "employee_payout" ? (
                <>
                  <Label className="block space-y-1">
                    <span className="text-sm">Сотрудник</span>
                    <Select onValueChange={setEmployeeId} value={employeeId}>
                      <SelectTrigger>
                        <SelectValue placeholder="Выберите сотрудника" />
                      </SelectTrigger>
                      <SelectContent>
                        {employees.map((employee) => (
                          <SelectItem
                            disabled={!employee.on_demand}
                            key={employee.id}
                            value={employee.id}
                          >
                            {employee.full_name}
                            {!employee.on_demand ? " — доступны аванс или займ" : ""}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <span className="text-xs text-muted-foreground">
                      Выплата доступна сотрудникам с окладом «по востребованию».
                    </span>
                  </Label>
                  <Label className="block space-y-1">
                    <span className="text-sm">Дата выплаты</span>
                    <Input
                      onChange={(event) => setPayoutDate(event.target.value)}
                      type="date"
                      value={payoutDate}
                    />
                  </Label>
                </>
              ) : null}

              {flow === "employee_advance" || flow === "employee_loan" ? (
                <Label className="block space-y-1">
                  <span className="text-sm">Сотрудник</span>
                  <Select onValueChange={setEmployeeId} value={employeeId}>
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
              ) : null}

              {needsCounterparty ? (
                <div className="space-y-1">
                  <Label className="text-sm" htmlFor="new-payment-counterparty">
                    {flow === "supplier_invoices" ? "Поставщик" : "Контрагент"}
                  </Label>
                  <Combobox
                    emptyMessage="Контрагенты не найдены"
                    id="new-payment-counterparty"
                    onChange={handleCounterpartyChange}
                    options={counterpartyOptions}
                    placeholder="Выберите контрагента"
                    searchPlaceholder="Название или ИНН…"
                    value={counterpartyId}
                  />
                </div>
              ) : null}

              {flow === "supplier_invoices" && counterpartyId ? (
                <div className="space-y-1">
                  <span className="text-sm">Неоплаченные накладные</span>
                  <div className="max-h-[180px] space-y-1 overflow-y-auto rounded-md border p-2">
                    {invoicesQuery.isLoading ? (
                      <div className="flex items-center gap-2 py-2 text-sm text-muted-foreground">
                        <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden="true" />
                        Загрузка накладных…
                      </div>
                    ) : invoices.length === 0 ? (
                      <div className="py-2 text-sm text-muted-foreground">
                        Нет неоплаченных накладных (или все уже отправлены в банк).
                      </div>
                    ) : (
                      invoices.map((invoice) => (
                        <label
                          className="flex cursor-pointer items-center gap-2 rounded px-1 py-1 text-sm hover:bg-muted/50"
                          key={invoice.id}
                        >
                          <input
                            checked={invoiceIds.includes(invoice.id)}
                            onChange={() => toggleInvoice(invoice.id)}
                            type="checkbox"
                          />
                          <span className="flex-1 truncate">
                            {invoice.number ? `№${invoice.number}` : "Без номера"}
                            {invoice.invoice_date ? ` · ${invoice.invoice_date}` : ""}
                          </span>
                          <span className="tabular-nums text-muted-foreground">
                            {formatRub(invoice.remaining)}
                          </span>
                        </label>
                      ))
                    )}
                  </div>
                  {invoiceIds.length > 0 ? (
                    <span className="block text-xs text-muted-foreground">
                      Выбрано {invoiceIds.length} — итого {formatRub(invoicesTotal)}
                    </span>
                  ) : null}
                </div>
              ) : null}

              <Label className="block space-y-1">
                <span className="text-sm">Сумма, ₽</span>
                {flow === "supplier_invoices" ? (
                  <Input
                    readOnly
                    value={invoiceIds.length > 0 ? invoicesTotal.toFixed(2) : ""}
                    placeholder="Сумма выбранных накладных"
                  />
                ) : (
                  <Input
                    inputMode="decimal"
                    onChange={(event) => setAmount(event.target.value)}
                    placeholder="0"
                    value={amount}
                  />
                )}
              </Label>

              <Label className="block space-y-1">
                <span className="text-sm">
                  Назначение
                  {flow && flow !== "expense" && !purposeAuto ? " (комментарий)" : ""}
                </span>
                <Input
                  disabled={purposeAuto}
                  maxLength={210}
                  onChange={(event) => setPurpose(event.target.value)}
                  placeholder={
                    purposeAuto
                      ? "Сформируется автоматически"
                      : "За что платим — уйдёт в банк и в целёвку"
                  }
                  value={purposeAuto ? "" : purpose}
                />
              </Label>

              {renderPlan()}
            </div>

            <DialogFooter>
              <Button onClick={() => onOpenChange(false)} type="button" variant="outline">
                Отмена
              </Button>
              <Button
                disabled={!canSubmit || informalRefusal}
                onClick={() => createMutation.mutate()}
                type="button"
              >
                {createMutation.isPending ? (
                  <LoaderCircle className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
                ) : null}
                Создать платёж
              </Button>
            </DialogFooter>
          </>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>Привязать операцию</DialogTitle>
              <DialogDescription>
                Черновик отправлен в банк. Выберите исходящую операцию из выписки, чтобы
                подтвердить выплату (заведёт перевод на Сейф с резервом).
              </DialogDescription>
            </DialogHeader>

            <div className="max-h-[320px] space-y-2 overflow-y-auto">
              {operationsQuery.isLoading ? (
                <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
                  <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden="true" />
                  Загрузка операций…
                </div>
              ) : operations.length === 0 ? (
                <div className="py-6 text-sm text-muted-foreground">
                  Нет несопоставленных исходящих операций за последние 45 дней. Операция
                  появится после импорта выписки — привяжите позже.
                </div>
              ) : (
                operations.map((op) => (
                  <button
                    className={`w-full rounded-md border p-2 text-left text-sm transition hover:bg-muted/50 ${
                      operationId === op.id ? "border-primary bg-muted/50" : "border-border"
                    }`}
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

            <DialogFooter>
              <Button onClick={() => onOpenChange(false)} type="button" variant="outline">
                Позже
              </Button>
              <Button
                disabled={!operationId || confirmMutation.isPending}
                onClick={() => confirmMutation.mutate()}
                type="button"
              >
                {confirmMutation.isPending ? (
                  <LoaderCircle className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
                ) : null}
                Подтвердить выплату
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
