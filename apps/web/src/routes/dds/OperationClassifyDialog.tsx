import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Plus, Trash2 } from "lucide-react";
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
import { InlineOptionList, type ComboboxOption } from "@/components/ui/combobox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ArticleCombobox } from "@/components/ui-app/ArticleCombobox";
import {
  apiErrorMessage,
  classifyCashflowTransaction,
  classifyOperation,
  getDdsArticles,
  getDdsCounterparties,
  getDdsPayoutEmployees,
  getDdsUnpaidInvoices,
  getDdsWallets,
  getPayrollAdvanceAvailability,
  type CashflowClassifyPayload,
  type JournalRow,
  type OperationClassifyPayload,
} from "@/lib/api";
import {
  DdsStatusBadge,
  DirectionBadge,
  compactText,
  formatDate,
  formatDdsMoney,
} from "@/routes/dds/shared";

const PREPAYMENT_ARTICLE_CODE = "advance_to_supplier";
const SUPPLIER_PAYMENT_ARTICLE_CODE = "payment_to_supplier";
// Транзитные статьи «перевод между счетами» — у строки с ними выбираем счёт-получатель (проводка).
const TRANSFER_OUT_ARTICLE_CODE = "vybytie_perevod_mezhdu_schetami";
const TRANSFER_IN_ARTICLE_CODE = "postuplenie_perevod_mezhdu_schetami";
// Зарплатные статьи — у строки с ними выбираем сотрудника-получателя (операция И проводка).
const EMPLOYEE_PAYOUT_ARTICLE_CODES = [
  "zarplata_administrativnogo_personala",
  "zarplata_proizvodstvennogo_personala",
];
// Статьи аванса/займа сотрудника — открывают модалку оформления аванса (создаёт SalaryAdvance).
const EMPLOYEE_ADVANCE_ARTICLE_CODE = "employee_advance";
const EMPLOYEE_LOAN_ARTICLE_CODE = "vydacha_zaymov_sotrudnikam";
const EMPLOYEE_ADVANCE_ARTICLE_CODES = [
  EMPLOYEE_ADVANCE_ARTICLE_CODE,
  EMPLOYEE_LOAN_ARTICLE_CODE,
];

type SplitRow = {
  key: string;
  articleId: string;
  amount: string;
  invoiceId: string;
  employeeId: string;
  transferWalletId: string;
};

const ACTION_TOAST: Record<string, string> = {
  split: "Разнесено по статьям",
  mark_internal_transfer: "Отмечено как внутренний перевод",
  exclude: "Исключено из ДДС",
  mark_safe_topup: "Проведено как пополнение Сейфа",
  employee_advance: "Оформлено как аванс/заём сотруднику",
};

function round2(value: number): number {
  return Math.round(value * 100) / 100;
}

/**
 * Единая модалка разбора движения ДДС — и операции выписки, и ручной проводки (дискриминатор
 * ``row.kind``). Показывает по статье нужные подполя: накладная (поставщик, только операция) ·
 * сотрудник (зарплата, оба) · счёт-получатель (перевод, только проводка). Операционные действия
 * (внутренний перевод, пополнение Сейфа, создание контрагента, «запомнить правило») — у операции.
 */
export function OperationClassifyDialog({
  row,
  canClassify,
  onClose,
}: {
  row: JournalRow | null;
  canClassify: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const isOperation = row?.kind === "operation";
  const targetId = isOperation ? row?.bank_operation_id ?? "" : row?.id ?? "";

  const articlesQuery = useQuery({ queryKey: ["dds", "articles"], queryFn: getDdsArticles });
  const counterpartiesQuery = useQuery({
    queryKey: ["dds", "counterparties"],
    queryFn: () => getDdsCounterparties(),
  });
  const walletsQuery = useQuery({ queryKey: ["dds", "wallets"], queryFn: getDdsWallets });
  const [rows, setRows] = useState<SplitRow[]>([]);
  const [counterpartyId, setCounterpartyId] = useState("");
  const [createNewCounterparty, setCreateNewCounterparty] = useState(false);
  const [rememberAsRule, setRememberAsRule] = useState(false);
  // Явное подтверждение ручной привязки карт-оплаты к накладной (см. bindsInvoiceOnCard ниже).
  const [cardBindAck, setCardBindAck] = useState(false);
  // Ключ строки, для которой открыта под-модалка деталей (сотрудник/счёт-получатель/контрагент+
  // накладная) — деталь живёт прямо в строке статьи, без отдельных inline-дропдаунов и блоков.
  const [detailRowKey, setDetailRowKey] = useState<string | null>(null);
  // Форма аванса/займа (для статей аванса) — сотрудник берётся из detailRow.employeeId.
  const [advanceKind, setAdvanceKind] = useState<"advance" | "loan">("advance");
  const [advanceInstallment, setAdvanceInstallment] = useState("");
  const [advanceRecoveryDate, setAdvanceRecoveryDate] = useState("");
  const [advanceOverride, setAdvanceOverride] = useState(false);

  // Существующий контрагент по ИНН из выписки (только операция) — предзаполняем, чтобы не плодить дубль.
  const matchedByInn = row?.counterparty_inn_raw
    ? (counterpartiesQuery.data ?? []).find((cp) => cp.inn === row.counterparty_inn_raw)
    : undefined;

  // Сброс формы при смене строки: одна доля на всю сумму (у проводки — с её текущей статьёй).
  useEffect(() => {
    if (row) {
      setRows([
        {
          key: crypto.randomUUID(),
          articleId: isOperation ? "none" : row.article_id ?? "none",
          amount: row.amount,
          invoiceId: "",
          employeeId: "",
          transferWalletId: "",
        },
      ]);
      setCounterpartyId(row.counterparty_id ?? matchedByInn?.id ?? "");
      setCreateNewCounterparty(false);
      setRememberAsRule(false);
      setCardBindAck(false);
    }
  }, [row?.id, matchedByInn?.id]);

  const total = row ? Number(row.amount) : 0;
  const allocated = round2(rows.reduce((sum, item) => sum + (Number(item.amount) || 0), 0));
  const remainder = round2(total - allocated);
  const balanced = Math.abs(remainder) < 0.005 && rows.every((item) => Number(item.amount) > 0);

  const invalidate = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["dds", "operations"] }),
      queryClient.invalidateQueries({ queryKey: ["dds", "journal"] }),
      queryClient.invalidateQueries({ queryKey: ["dds", "cashflow"] }),
      queryClient.invalidateQueries({ queryKey: ["dds", "owner-review"] }),
      queryClient.invalidateQueries({ queryKey: ["dds", "wallets"] }),
      queryClient.invalidateQueries({ queryKey: ["dds", "classification-rules"] }),
    ]);
  };

  const mutation = useMutation({
    mutationFn: (payload: OperationClassifyPayload | CashflowClassifyPayload) =>
      isOperation
        ? classifyOperation(targetId, payload as OperationClassifyPayload)
        : classifyCashflowTransaction(targetId, payload as CashflowClassifyPayload),
    onSuccess: async (_data, payload) => {
      await invalidate();
      toast.success(ACTION_TOAST[payload.action] ?? "Готово");
      onClose();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить разбор")),
  });

  const articles = articlesQuery.data ?? [];
  const supplierPaymentArticleId = articles.find(
    (a) => a.code === SUPPLIER_PAYMENT_ARTICLE_CODE,
  )?.id;
  const advanceArticleId = articles.find((a) => a.code === PREPAYMENT_ARTICLE_CODE)?.id;
  const salaryArticleIds = new Set(
    articles.filter((a) => EMPLOYEE_PAYOUT_ARTICLE_CODES.includes(a.code)).map((a) => a.id),
  );
  const employeeAdvanceArticleIds = new Set(
    articles.filter((a) => EMPLOYEE_ADVANCE_ARTICLE_CODES.includes(a.code)).map((a) => a.id),
  );
  const loanArticleId = articles.find((a) => a.code === EMPLOYEE_LOAN_ARTICLE_CODE)?.id;
  const transferArticleIds = new Set(
    articles
      .filter(
        (a) => a.code === TRANSFER_OUT_ARTICLE_CODE || a.code === TRANSFER_IN_ARTICLE_CODE,
      )
      .map((a) => a.id),
  );
  const usesSupplierPayment =
    Boolean(supplierPaymentArticleId) &&
    rows.some((item) => item.articleId === supplierPaymentArticleId);
  const usesAdvance =
    Boolean(advanceArticleId) && rows.some((item) => item.articleId === advanceArticleId);
  const usesSalaryArticle = rows.some((item) => salaryArticleIds.has(item.articleId));

  // Карт-операция (получатель в банке — эквайер, не поставщик): её оплату не привязывают к
  // накладной автоматически. Ручная привязка допустима, но требует явного подтверждения оператора
  // (allow_card) — иначе backend-guard режет карт-шум. bindsInvoiceOnCard = выбрана накладная в
  // строке «Оплата поставщикам» именно у карт-операции.
  const isCardOperation = isOperation && Boolean(row?.is_card);
  const bindsInvoiceOnCard =
    isCardOperation &&
    Boolean(supplierPaymentArticleId) &&
    rows.some((item) => item.articleId === supplierPaymentArticleId && Boolean(item.invoiceId));

  // Неоплаченные накладные контрагента (привязка оплаты) — только для операции выписки.
  const invoicesQuery = useQuery({
    queryKey: ["dds", "cp-unpaid-invoices", counterpartyId],
    queryFn: () => getDdsUnpaidInvoices(counterpartyId),
    enabled: isOperation && Boolean(counterpartyId) && usesSupplierPayment,
  });
  // Сотрудники для зарплатной строки — активные + увольняемые (обходит запрет /staff кассиру).
  const usesAdvanceArticle = rows.some((item) => employeeAdvanceArticleIds.has(item.articleId));
  const payoutEmployeesQuery = useQuery({
    queryKey: ["dds", "payout-employees"],
    queryFn: getDdsPayoutEmployees,
    enabled: usesSalaryArticle || usesAdvanceArticle,
  });

  // «Доступно к авансу» на дату операции — для формы аванса (сотрудник берётся из строки).
  const advanceEmployeeId = detailRowKey
    ? rows.find((item) => item.key === detailRowKey)?.employeeId ?? ""
    : "";
  const advanceAvailabilityQuery = useQuery({
    queryKey: ["dds", "advance-availability", advanceEmployeeId, row?.operation_date],
    // Реконсиляция уже прошедшей операции: без отсечки «день выплаты» (apply_payout_gate=false),
    // иначе на дату операции = день выплаты показалось бы ложное «доступно 0».
    queryFn: () => getPayrollAdvanceAvailability(advanceEmployeeId, row!.operation_date, false),
    enabled: Boolean(isOperation && advanceEmployeeId && row?.operation_date),
  });

  // Сброс формы аванса при открытии строки аванса: тип по статье (заём/аванс), поля пусты.
  useEffect(() => {
    const item = detailRowKey ? rows.find((r) => r.key === detailRowKey) : undefined;
    if (item && employeeAdvanceArticleIds.has(item.articleId)) {
      setAdvanceKind(item.articleId === loanArticleId ? "loan" : "advance");
      setAdvanceInstallment("");
      setAdvanceRecoveryDate("");
      setAdvanceOverride(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detailRowKey]);

  // Снять подтверждение карт-привязки, если накладная у карт-операции больше не выбрана —
  // иначе allow_card «залип» бы на следующий выбор без нового согласия оператора.
  useEffect(() => {
    if (!bindsInvoiceOnCard) setCardBindAck(false);
  }, [bindsInvoiceOnCard]);

  if (!row) {
    return null;
  }

  const isTransferRow = (articleId: string) => transferArticleIds.has(articleId);

  function updateRow(key: string, patch: Partial<SplitRow>) {
    setRows((current) => current.map((item) => (item.key === key ? { ...item, ...patch } : item)));
  }

  function addRow() {
    setRows((current) => [
      ...current,
      {
        key: crypto.randomUUID(),
        articleId: "none",
        amount: remainder > 0 ? String(remainder) : "",
        invoiceId: "",
        employeeId: "",
        transferWalletId: "",
      },
    ]);
  }

  function removeRow(key: string) {
    setRows((current) => (current.length > 1 ? current.filter((item) => item.key !== key) : current));
  }

  function submitSplit() {
    if (rows.some((item) => item.articleId === "none")) {
      toast.error("Выберите статью в каждой строке");
      return;
    }
    if (!balanced) {
      toast.error("Сумма по статьям должна равняться сумме");
      return;
    }
    if (usesAdvance && !counterpartyId && !createNewCounterparty) {
      toast.error("Для статьи «Авансы поставщикам» выберите контрагента");
      return;
    }
    if (rows.some((item) => salaryArticleIds.has(item.articleId) && !item.employeeId)) {
      toast.error("Для зарплатной статьи выберите сотрудника-получателя");
      return;
    }
    if (!isOperation && rows.some((item) => isTransferRow(item.articleId) && !item.transferWalletId)) {
      toast.error("Выберите счёт-получатель для строки перевода между счетами");
      return;
    }
    if (bindsInvoiceOnCard && !cardBindAck) {
      toast.error("Подтвердите ручную привязку карт-оплаты к накладной");
      return;
    }
    if (isOperation) {
      mutation.mutate({
        action: "split",
        splits: rows.map((item) => ({
          article_id: item.articleId,
          amount: item.amount,
          invoice_id:
            item.articleId === supplierPaymentArticleId && item.invoiceId ? item.invoiceId : null,
          employee_id:
            salaryArticleIds.has(item.articleId) && item.employeeId ? item.employeeId : null,
        })),
        counterparty_id: createNewCounterparty ? null : counterpartyId || null,
        new_counterparty_name: createNewCounterparty ? row.counterparty_name_raw : null,
        new_counterparty_inn: createNewCounterparty ? row.counterparty_inn_raw : null,
        // Правило не запоминаем при карт-привязке (backend его тоже отклонит) — чекбокс скрыт.
        remember_as_rule: rememberAsRule && rows.length === 1 && !bindsInvoiceOnCard,
        // Карт-операция + привязанная накладная: разрешаем guard пропустить карт-шум.
        allow_card: bindsInvoiceOnCard ? true : undefined,
      });
    } else {
      mutation.mutate({
        action: "split",
        splits: rows.map((item) => ({
          article_id: item.articleId,
          amount: item.amount,
          transfer_wallet_id: isTransferRow(item.articleId) ? item.transferWalletId || null : null,
          employee_id:
            salaryArticleIds.has(item.articleId) && item.employeeId ? item.employeeId : null,
        })),
        counterparty_id: counterpartyId || null,
      });
    }
  }

  function submitExclude() {
    if (isOperation) {
      mutation.mutate({ action: "exclude" });
      return;
    }
    const confirmed = window.confirm(
      "Исключить проводку из ДДС? Баланс счёта изменится на её сумму. Действие обратимо — повторный разбор вернёт проводку.",
    );
    if (confirmed) {
      mutation.mutate({ action: "exclude" });
    }
  }

  const counterparties = counterpartiesQuery.data ?? [];
  const counterpartyOptions: ComboboxOption[] = [
    { value: "", label: "Не указан" },
    ...counterparties.map((cp) => ({ value: cp.id, label: cp.name, keywords: cp.inn ?? undefined })),
  ];
  const invoiceOptions: ComboboxOption[] = [
    { value: "", label: "Не привязывать" },
    ...(invoicesQuery.data ?? []).map((inv) => ({
      value: inv.id,
      label: `№ ${inv.number ?? "б/н"} · остаток ${formatDdsMoney(inv.remaining)}`,
      keywords: inv.number ?? undefined,
    })),
  ];
  const employeeOptions: ComboboxOption[] = [
    { value: "", label: "Не выбран" },
    ...(payoutEmployeesQuery.data ?? []).map((emp) => ({
      value: emp.id,
      label:
        emp.status === "active"
          ? emp.full_name
          : `${emp.full_name} · ${emp.status === "dismissing" ? "увольняется" : "уволен"}`,
      keywords: emp.position ?? undefined,
    })),
  ];
  const transferOptions: ComboboxOption[] = (walletsQuery.data ?? [])
    .filter((wallet) => wallet.id !== row.wallet_id && wallet.status === "active")
    .map((wallet) => ({ value: wallet.id, label: wallet.name, keywords: wallet.code }));

  // Деталь строки (открывается по клику на строку) — зависит от статьи: сотрудник (зарплата) ·
  // счёт-получатель (перевод, проводка) · контрагент + накладная (поставщик/аванс/прочее).
  // Контрагента НЕ выносим отдельным блоком — он живёт прямо в строке нужной статьи.
  const rowDetailKind = (
    item: SplitRow,
  ): "employee" | "advance" | "transfer" | "counterparty" | null => {
    if (item.articleId === "none") return null;
    if (isOperation && employeeAdvanceArticleIds.has(item.articleId)) return "advance";
    if (salaryArticleIds.has(item.articleId)) return "employee";
    if (!isOperation && isTransferRow(item.articleId)) return "transfer";
    return "counterparty";
  };
  const selectedCounterpartyName = createNewCounterparty
    ? `Будет создан: ${row.counterparty_name_raw ?? ""}`
    : counterpartyId
      ? counterparties.find((cp) => cp.id === counterpartyId)?.name ?? ""
      : "";
  const rowDetailSummary = (item: SplitRow): { text: string; missing: boolean } => {
    switch (rowDetailKind(item)) {
      case "employee": {
        const emp = (payoutEmployeesQuery.data ?? []).find((e) => e.id === item.employeeId);
        return emp
          ? { text: `Сотрудник: ${emp.full_name}`, missing: false }
          : { text: "нужен сотрудник", missing: true };
      }
      case "advance": {
        const emp = (payoutEmployeesQuery.data ?? []).find((e) => e.id === item.employeeId);
        const label = item.articleId === loanArticleId ? "заём" : "аванс";
        return emp
          ? { text: `${label}: ${emp.full_name}`, missing: false }
          : { text: `нужен сотрудник (${label})`, missing: true };
      }
      case "transfer": {
        const w = (walletsQuery.data ?? []).find((x) => x.id === item.transferWalletId);
        return w
          ? { text: `Счёт: ${w.name}`, missing: false }
          : { text: "нужен счёт-получатель", missing: true };
      }
      case "counterparty": {
        const isAdvanceRow = item.articleId === advanceArticleId;
        if (selectedCounterpartyName) {
          const inv = (invoicesQuery.data ?? []).find((x) => x.id === item.invoiceId);
          const invText =
            item.articleId === supplierPaymentArticleId && inv
              ? ` · накладная № ${inv.number ?? "б/н"}`
              : "";
          return { text: `Контрагент: ${selectedCounterpartyName}${invText}`, missing: false };
        }
        return isAdvanceRow
          ? { text: "нужен контрагент", missing: true }
          : { text: "контрагент (необязательно)", missing: false };
      }
      default:
        return { text: "", missing: false };
    }
  };
  const detailRow = detailRowKey ? rows.find((item) => item.key === detailRowKey) ?? null : null;

  const salaryRowMissingEmployee = rows.some(
    (item) => salaryArticleIds.has(item.articleId) && !item.employeeId,
  );

  // «Пополнение Сейфа»: если строки размечены статьёй/получателем — это уже целёвки-резервы
  // (спрашивать отдельно не нужно); голое пополнение без разметки — просто транзит р/с→Сейф.
  function submitSafeTopup() {
    const marked =
      rows.every((item) => item.articleId !== "none") && balanced && !salaryRowMissingEmployee;
    if (marked) {
      mutation.mutate({
        action: "mark_safe_topup",
        splits: rows.map((item) => ({
          article_id: item.articleId,
          amount: item.amount,
          employee_id:
            salaryArticleIds.has(item.articleId) && item.employeeId ? item.employeeId : null,
        })),
        counterparty_id: counterpartyId || null,
      });
    } else {
      mutation.mutate({ action: "mark_safe_topup" });
    }
  }
  const transferRowMissingWallet =
    !isOperation && rows.some((item) => isTransferRow(item.articleId) && !item.transferWalletId);

  // Аванс/заём оформляется НЕ «Разнести», а формой аванса в строке (создаёт SalaryAdvance).
  function submitAdvance() {
    if (!detailRow || !detailRow.employeeId) {
      toast.error("Выберите сотрудника");
      return;
    }
    mutation.mutate({
      action: "employee_advance",
      splits: [
        { article_id: detailRow.articleId, amount: detailRow.amount, employee_id: detailRow.employeeId },
      ],
      advance_kind: advanceKind,
      advance_installment_amount:
        advanceKind === "loan" && advanceInstallment ? advanceInstallment : null,
      advance_recovery_start_date:
        advanceKind === "loan" && advanceRecoveryDate ? advanceRecoveryDate : null,
      advance_override_ceiling: advanceKind === "loan" ? advanceOverride : false,
    });
  }

  const isBusy = mutation.isPending;

  return (
    <>
    <Dialog open={Boolean(row)} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="overflow-visible sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle className="flex flex-wrap items-center gap-3">
            <span>{isOperation ? "Разбор операции" : "Разбор проводки"}</span>
            <DirectionBadge direction={row.direction} />
            <DdsStatusBadge status={row.status} />
          </DialogTitle>
          <DialogDescription className="text-base font-medium text-foreground">
            {formatDate(row.operation_date)} · {formatDdsMoney(row.amount)}
          </DialogDescription>
        </DialogHeader>

        <div className="rounded-lg border bg-muted/20 p-3">
          <div className="text-xs font-medium uppercase text-muted-foreground">
            Назначение платежа
          </div>
          <div className="mt-1 break-words text-sm">{compactText(row.payment_purpose)}</div>
        </div>

        {canClassify ? (
          <div className="grid gap-3 border-t pt-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <Label className="text-base font-semibold">Разнести по статьям ДДС</Label>
              <div className="text-sm tabular-nums text-muted-foreground">
                Распределено {formatDdsMoney(allocated)} из {formatDdsMoney(total)}
                {Math.abs(remainder) >= 0.005 ? (
                  <span className="ml-2 font-medium text-amber-600">
                    остаток {formatDdsMoney(remainder)}
                  </span>
                ) : (
                  <span className="ml-2 font-medium text-emerald-600">сходится ✓</span>
                )}
              </div>
            </div>

            <div className="grid gap-2">
              {rows.map((item) => (
                <div key={item.key} className="grid gap-1.5">
                  <div className="grid grid-cols-[minmax(0,1fr)_140px_auto] items-center gap-2">
                    <ArticleCombobox
                      articles={articles}
                      value={item.articleId}
                      onChange={(value) => updateRow(item.key, { articleId: value })}
                    />
                    <Input
                      className="text-right tabular-nums"
                      inputMode="decimal"
                      value={item.amount}
                      onChange={(event) => updateRow(item.key, { amount: event.target.value })}
                    />
                    <Button
                      disabled={rows.length === 1}
                      onClick={() => removeRow(item.key)}
                      size="icon"
                      title="Удалить строку"
                      type="button"
                      variant="ghost"
                    >
                      <Trash2 size={16} aria-hidden="true" />
                    </Button>
                  </div>
                  {rowDetailKind(item) ? (
                    <button
                      className={`self-start pl-1 text-left text-xs underline-offset-2 hover:underline ${
                        rowDetailSummary(item).missing
                          ? "font-medium text-amber-700"
                          : "text-muted-foreground"
                      }`}
                      onClick={() => setDetailRowKey(item.key)}
                      type="button"
                    >
                      {rowDetailSummary(item).text}
                    </button>
                  ) : null}
                </div>
              ))}
            </div>

            <div className="flex flex-wrap items-center justify-between gap-3">
              <Button onClick={addRow} size="sm" type="button" variant="outline">
                <Plus size={16} aria-hidden="true" />
                Добавить статью
              </Button>
              {isOperation && rows.length === 1 && !bindsInvoiceOnCard ? (
                <label className="flex items-center gap-2 text-sm">
                  <input
                    checked={rememberAsRule}
                    className="h-4 w-4"
                    onChange={(event) => setRememberAsRule(event.target.checked)}
                    type="checkbox"
                  />
                  Запомнить как правило
                </label>
              ) : null}
            </div>

            {bindsInvoiceOnCard ? (
              <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-800">
                <p>
                  Это карт-операция: в банке получатель — эквайер, не поставщик. Обычно такую оплату
                  не привязывают к накладной автоматически. Привязать оплату к накладной вручную?
                </p>
                <label className="mt-2 flex items-center gap-2 font-medium">
                  <input
                    checked={cardBindAck}
                    className="h-4 w-4"
                    onChange={(event) => setCardBindAck(event.target.checked)}
                    type="checkbox"
                  />
                  Привязать оплату к накладной вручную (подтверждаю)
                </label>
              </div>
            ) : null}

            <div className="flex flex-wrap gap-2 border-t pt-4">
              <Button
                disabled={
                  isBusy ||
                  !balanced ||
                  (usesAdvance && !counterpartyId && !createNewCounterparty) ||
                  salaryRowMissingEmployee ||
                  transferRowMissingWallet ||
                  usesAdvanceArticle ||
                  (bindsInvoiceOnCard && !cardBindAck)
                }
                onClick={submitSplit}
                title={
                  usesAdvanceArticle
                    ? "Аванс/заём оформляется в строке статьи — нажмите на неё"
                    : undefined
                }
              >
                {isBusy ? (
                  <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                ) : null}
                Разнести
              </Button>
              {isOperation ? (
                <Button
                  disabled={isBusy}
                  onClick={() => mutation.mutate({ action: "mark_internal_transfer" })}
                  variant="outline"
                >
                  Внутренний перевод
                </Button>
              ) : null}
              {isOperation && row.direction === "out" ? (
                <Button
                  disabled={isBusy || salaryRowMissingEmployee}
                  onClick={submitSafeTopup}
                  variant="outline"
                  title="Перевод на карту «Сейф». Со статьёй и получателем — целёвка-резерв (выплата и учёт в ЗП по «Выплачено»); без разметки — просто пополнение."
                >
                  Пополнение Сейфа
                </Button>
              ) : null}
              <Button disabled={isBusy} onClick={submitExclude} variant="outline">
                Исключить
              </Button>
            </div>
          </div>
        ) : (
          <div className="rounded-md border bg-muted/40 px-3 py-2 text-sm text-muted-foreground">
            Режим просмотра. Разбор недоступен.
          </div>
        )}
      </DialogContent>
    </Dialog>

    {detailRow ? (
      <Dialog open onOpenChange={(open) => !open && setDetailRowKey(null)}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>
              {rowDetailKind(detailRow) === "advance"
                ? detailRow.articleId === loanArticleId
                  ? "Заём сотруднику"
                  : "Аванс сотруднику"
                : rowDetailKind(detailRow) === "employee"
                  ? "Сотрудник-получатель"
                  : rowDetailKind(detailRow) === "transfer"
                    ? "Счёт-получатель перевода"
                    : "Контрагент"}
            </DialogTitle>
            <DialogDescription>
              {rowDetailKind(detailRow) === "advance"
                ? "Аванс — в счёт зарплаты; излишек над заработанным вернётся из следующей выплаты."
                : articles.find((a) => a.id === detailRow.articleId)?.name ?? "Данные для строки"}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            {rowDetailKind(detailRow) === "advance" ? (
              <div className="space-y-3">
                <div className="inline-flex w-fit overflow-hidden rounded-md border">
                  <button
                    type="button"
                    className={`px-4 py-1.5 text-sm ${advanceKind === "advance" ? "bg-primary/10 font-medium text-primary" : ""}`}
                    onClick={() => setAdvanceKind("advance")}
                  >
                    Аванс
                  </button>
                  <button
                    type="button"
                    className={`px-4 py-1.5 text-sm ${advanceKind === "loan" ? "bg-primary/10 font-medium text-primary" : ""}`}
                    onClick={() => setAdvanceKind("loan")}
                  >
                    Заём
                  </button>
                </div>
                <div className="rounded-md border bg-muted/40 p-2.5 text-sm text-muted-foreground">
                  Сумма <b className="text-foreground">{formatDdsMoney(detailRow.amount)}</b> · дата{" "}
                  {formatDate(row.operation_date)} — из операции (деньги уже ушли банком).
                </div>
                <div className="space-y-1">
                  <Label className="text-sm">Сотрудник</Label>
                  <InlineOptionList
                    options={employeeOptions}
                    value={detailRow.employeeId}
                    onChange={(value) => updateRow(detailRow.key, { employeeId: value })}
                    searchPlaceholder="Поиск по имени…"
                    emptyMessage={
                      payoutEmployeesQuery.isLoading ? "Загрузка сотрудников…" : "Сотрудники не найдены"
                    }
                    listClassName="max-h-56"
                  />
                </div>
                {detailRow.employeeId ? (
                  <div className="rounded-md border bg-muted/40 p-2.5 text-sm">
                    {advanceAvailabilityQuery.isLoading ? (
                      "Считаем доступное…"
                    ) : advanceAvailabilityQuery.data ? (
                      <>
                        Доступно к авансу на {formatDate(row.operation_date)}:{" "}
                        <b>{formatDdsMoney(advanceAvailabilityQuery.data.available)}</b>
                      </>
                    ) : (
                      "—"
                    )}
                  </div>
                ) : null}
                {advanceKind === "advance" &&
                detailRow.employeeId &&
                advanceAvailabilityQuery.data &&
                Number(detailRow.amount) > advanceAvailabilityQuery.data.available ? (
                  <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-800">
                    Больше заработанного (
                    {formatDdsMoney(advanceAvailabilityQuery.data.available)}). Остаток удержится из
                    следующей выплаты.
                  </div>
                ) : null}
                {advanceKind === "loan" ? (
                  <>
                    <div className="space-y-1">
                      <Label className="text-sm">Сумма удержания за период, ₽</Label>
                      <Input
                        type="number"
                        inputMode="decimal"
                        value={advanceInstallment}
                        onChange={(event) => setAdvanceInstallment(event.target.value)}
                        placeholder="Пусто — весь заём одной ведомостью"
                      />
                    </div>
                    <div className="space-y-1">
                      <Label className="text-sm">Удерживать с выплаты</Label>
                      <Input
                        type="date"
                        value={advanceRecoveryDate}
                        onChange={(event) => setAdvanceRecoveryDate(event.target.value)}
                      />
                    </div>
                    <label className="flex items-center gap-2 text-sm text-muted-foreground">
                      <input
                        type="checkbox"
                        checked={advanceOverride}
                        onChange={(event) => setAdvanceOverride(event.target.checked)}
                      />
                      Превысить потолок займа (подтверждаю)
                    </label>
                  </>
                ) : null}
              </div>
            ) : null}
            {rowDetailKind(detailRow) === "employee" ? (
              <InlineOptionList
                options={employeeOptions}
                value={detailRow.employeeId}
                onChange={(value) => updateRow(detailRow.key, { employeeId: value })}
                searchPlaceholder="Поиск по имени…"
                emptyMessage={
                  payoutEmployeesQuery.isLoading ? "Загрузка сотрудников…" : "Сотрудники не найдены"
                }
                listClassName="max-h-[22rem]"
              />
            ) : null}
            {rowDetailKind(detailRow) === "transfer" ? (
              <InlineOptionList
                options={transferOptions}
                value={detailRow.transferWalletId}
                onChange={(value) => updateRow(detailRow.key, { transferWalletId: value })}
                searchPlaceholder="Поиск счёта…"
                emptyMessage="Счета не найдены"
                listClassName="max-h-[22rem]"
              />
            ) : null}
            {rowDetailKind(detailRow) === "counterparty" ? (
              <>
                {isOperation && createNewCounterparty ? (
                  <div className="flex items-center justify-between gap-2 rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm">
                    <span>
                      Будет создан: <span className="font-medium">{row.counterparty_name_raw}</span>
                      {row.counterparty_inn_raw ? ` · ИНН ${row.counterparty_inn_raw}` : ""}
                    </span>
                    <Button
                      onClick={() => setCreateNewCounterparty(false)}
                      size="sm"
                      type="button"
                      variant="ghost"
                    >
                      Отмена
                    </Button>
                  </div>
                ) : (
                  <>
                    {isOperation && row.counterparty_name_raw && !counterpartyId && !matchedByInn ? (
                      <button
                        className="self-start text-left text-sm font-medium text-emerald-700 hover:underline"
                        onClick={() => setCreateNewCounterparty(true)}
                        type="button"
                      >
                        + Создать контрагента из операции: {row.counterparty_name_raw}
                        {row.counterparty_inn_raw ? ` (ИНН ${row.counterparty_inn_raw})` : ""}
                      </button>
                    ) : null}
                    <InlineOptionList
                      options={counterpartyOptions}
                      value={counterpartyId}
                      onChange={setCounterpartyId}
                      searchPlaceholder="Поиск по названию или ИНН…"
                      emptyMessage="Контрагенты не найдены"
                      listClassName="max-h-[20rem]"
                    />
                  </>
                )}
                {isOperation && detailRow.articleId === supplierPaymentArticleId ? (
                  <div className="space-y-1">
                    <Label className="text-sm">Накладная для гашения</Label>
                    <InlineOptionList
                      options={invoiceOptions}
                      value={detailRow.invoiceId}
                      onChange={(value) => updateRow(detailRow.key, { invoiceId: value })}
                      searchPlaceholder="Поиск по номеру…"
                      emptyMessage={counterpartyId ? "Накладных нет" : "Сначала выберите контрагента"}
                      listClassName="max-h-48"
                      autoFocus={false}
                    />
                  </div>
                ) : null}
              </>
            ) : null}
          </div>
          <DialogFooter>
            {rowDetailKind(detailRow) === "advance" ? (
              <>
                <Button
                  onClick={() => setDetailRowKey(null)}
                  type="button"
                  variant="outline"
                >
                  Отмена
                </Button>
                <Button
                  disabled={isBusy || !detailRow.employeeId}
                  onClick={submitAdvance}
                  type="button"
                >
                  {isBusy ? (
                    <LoaderCircle className="mr-2 animate-spin" size={16} aria-hidden="true" />
                  ) : null}
                  {advanceKind === "loan" ? "Оформить заём" : "Оформить аванс"}
                </Button>
              </>
            ) : (
              <Button onClick={() => setDetailRowKey(null)} type="button">
                Готово
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    ) : null}
    </>
  );
}
