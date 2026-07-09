import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Combobox, type ComboboxOption } from "@/components/ui/combobox";
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
  salary_via_safe: "Проведено через Сейф — резерв под сотрудника",
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

  // Неоплаченные накладные контрагента (привязка оплаты) — только для операции выписки.
  const invoicesQuery = useQuery({
    queryKey: ["dds", "cp-unpaid-invoices", counterpartyId],
    queryFn: () => getDdsUnpaidInvoices(counterpartyId),
    enabled: isOperation && Boolean(counterpartyId) && usesSupplierPayment,
  });
  // Сотрудники для зарплатной строки — активные + увольняемые (обходит запрет /staff кассиру).
  const payoutEmployeesQuery = useQuery({
    queryKey: ["dds", "payout-employees"],
    queryFn: getDdsPayoutEmployees,
    enabled: usesSalaryArticle,
  });

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
        remember_as_rule: rememberAsRule && rows.length === 1,
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

  // «Через Сейф» доступно, когда вся исходящая операция — одна зарплатная строка с сотрудником.
  const viaSafeEligible =
    isOperation &&
    row.direction === "out" &&
    rows.length === 1 &&
    salaryArticleIds.has(rows[0].articleId) &&
    Boolean(rows[0].employeeId);

  function submitSalaryViaSafe() {
    const item = rows[0];
    mutation.mutate({
      action: "salary_via_safe",
      splits: [{ article_id: item.articleId, amount: item.amount, employee_id: item.employeeId }],
    });
  }

  const salaryRowMissingEmployee = rows.some(
    (item) => salaryArticleIds.has(item.articleId) && !item.employeeId,
  );
  const transferRowMissingWallet =
    !isOperation && rows.some((item) => isTransferRow(item.articleId) && !item.transferWalletId);
  const isBusy = mutation.isPending;

  return (
    <Dialog open={Boolean(row)} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
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
                  {isOperation && item.articleId === supplierPaymentArticleId ? (
                    <Combobox
                      options={invoiceOptions}
                      value={item.invoiceId}
                      onChange={(value) => updateRow(item.key, { invoiceId: value })}
                      placeholder={
                        counterpartyId
                          ? "Накладная для гашения (необязательно)"
                          : "Сначала выберите контрагента ниже"
                      }
                      searchPlaceholder="Поиск по номеру…"
                    />
                  ) : null}
                  {salaryArticleIds.has(item.articleId) ? (
                    <Combobox
                      options={employeeOptions}
                      value={item.employeeId}
                      onChange={(value) => updateRow(item.key, { employeeId: value })}
                      placeholder={
                        payoutEmployeesQuery.isLoading
                          ? "Загрузка сотрудников…"
                          : "Сотрудник-получатель (обязательно)"
                      }
                      searchPlaceholder="Поиск по имени…"
                    />
                  ) : null}
                  {!isOperation && isTransferRow(item.articleId) ? (
                    <Combobox
                      options={transferOptions}
                      value={item.transferWalletId}
                      onChange={(value) => updateRow(item.key, { transferWalletId: value })}
                      placeholder="Счёт-получатель перевода…"
                      searchPlaceholder="Поиск счёта…"
                      emptyMessage="Счета не найдены"
                    />
                  ) : null}
                </div>
              ))}
            </div>

            <div className="flex flex-wrap items-center justify-between gap-3">
              <Button onClick={addRow} size="sm" type="button" variant="outline">
                <Plus size={16} aria-hidden="true" />
                Добавить статью
              </Button>
              {isOperation && rows.length === 1 ? (
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

            <div className="grid gap-1.5">
              <Label className="text-sm">
                Контрагент{usesAdvance ? <span className="text-red-600"> *</span> : null}
              </Label>
              {isOperation && createNewCounterparty ? (
                <div className="flex items-center justify-between gap-2 rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm">
                  <span>
                    Будет создан:{" "}
                    <span className="font-medium">{row.counterparty_name_raw}</span>
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
                  <Combobox
                    options={counterpartyOptions}
                    value={counterpartyId}
                    onChange={setCounterpartyId}
                    placeholder="Не указан"
                    searchPlaceholder="Поиск по названию или ИНН…"
                  />
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
                </>
              )}
              {usesAdvance ? (
                <span className="text-xs text-muted-foreground">
                  Статья «Авансы поставщикам» создаст предоплату (дебиторку) на этого контрагента.
                </span>
              ) : null}
            </div>

            <div className="flex flex-wrap gap-2 border-t pt-4">
              <Button
                disabled={
                  isBusy ||
                  !balanced ||
                  (usesAdvance && !counterpartyId && !createNewCounterparty) ||
                  salaryRowMissingEmployee ||
                  transferRowMissingWallet
                }
                onClick={submitSplit}
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
                  disabled={isBusy}
                  onClick={() => mutation.mutate({ action: "mark_safe_topup" })}
                  variant="outline"
                  title="Перевод на личную карту «Сейф» — учесть как пополнение Сейфа, а не расход"
                >
                  Пополнение Сейфа
                </Button>
              ) : null}
              {viaSafeEligible ? (
                <Button
                  disabled={isBusy}
                  onClick={submitSalaryViaSafe}
                  variant="outline"
                  title="Транзит р/с→Сейф + целёвка-резерв под сотрудника. Выплата и учёт в ЗП — по «Выплачено» на резерве"
                >
                  Через Сейф (резерв)
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
  );
}
