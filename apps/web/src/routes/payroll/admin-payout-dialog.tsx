import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, Landmark, LoaderCircle } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  apiErrorMessage,
  createRunBankDraft,
  getRunBankDraft,
  getRunFundingSources,
  getRunPayoutAllocation,
  setRunPayoutCash,
  type PayrollBankDraft,
  type PayrollFundingSource,
  type PayrollRun,
} from "@/lib/api";
import { cn } from "@/lib/utils";

import { moneyInputValue, moneyValue, normalizeMoney, parseMoneyInput } from "./admin-payslip-utils";
import { formatMoney } from "./runs";

type ChannelPerms = { safe: boolean; cash_tk: boolean; bank_draft: boolean };

/**
 * Выплата администрации: свёрнутая плашка + компактное модальное окно (тот же размер и
 * компоновка, что «Разбивка выплаты» в «Расчётах»). Одношаговый сабмит: если сплит
 * изменён — сначала сохраняем наличную часть, затем формируем/обновляем банк-черновик
 * одной мутацией. Админ-специфика (выбор банка T-Bank/Сбер, разнесение по статьям ДДС)
 * сохранена; разнесение — компактным сворачиваемым блоком.
 */
export function AdminPayoutDialog({
  channelPerms,
  runId,
  run,
  totalPayable,
}: {
  channelPerms: ChannelPerms;
  runId: string;
  run: PayrollRun;
  totalPayable: number;
}) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);

  const draftQuery = useQuery<PayrollBankDraft | null>({
    queryKey: ["admin-run-bank-draft", runId],
    queryFn: () => getRunBankDraft(runId).catch(() => null),
  });
  const allocationQuery = useQuery({
    queryKey: ["admin-run-payout-allocation", runId],
    queryFn: () => getRunPayoutAllocation(runId),
  });
  const fundingQuery = useQuery({
    queryKey: ["run-funding-sources", runId],
    queryFn: () => getRunFundingSources(runId),
  });

  const draft = draftQuery.data ?? null;
  const savedWalletId = run.payout_cash_wallet_id ?? null;
  const payoutCashTotal = Math.min(moneyValue(run.payout_cash_total ?? 0), totalPayable);
  const savedBankPreview = normalizeMoney(Math.max(0, totalPayable - payoutCashTotal));

  const cashWallets = useMemo<PayrollFundingSource[]>(
    // Только счета, выдача с которых разрешена правами на канал.
    () =>
      (fundingQuery.data?.cash_sources ?? []).filter((wallet) => {
        if (wallet.code === "cash_safe") {
          return channelPerms.safe;
        }
        if (wallet.code === "tk_chernikova") {
          return channelPerms.cash_tk;
        }
        return true;
      }),
    [fundingQuery.data?.cash_sources, channelPerms.safe, channelPerms.cash_tk],
  );

  const [cashValue, setCashValue] = useState(moneyInputValue(payoutCashTotal));
  const [walletCode, setWalletCode] = useState<string>("");
  // Банк для безналичного черновика: Тинькофф (по умолчанию) или Сбербанк — оба через Сейф.
  const [bankProvider, setBankProvider] = useState<"tbank" | "sber">("tbank");

  useEffect(() => {
    setCashValue(moneyInputValue(payoutCashTotal));
  }, [payoutCashTotal]);

  useEffect(() => {
    if (!cashWallets.length) {
      return;
    }
    const current = savedWalletId
      ? cashWallets.find((wallet) => wallet.id === savedWalletId)
      : undefined;
    setWalletCode((prev) => (prev ? prev : (current?.code ?? "")));
  }, [savedWalletId, cashWallets]);

  const cashAmount = parseMoneyInput(cashValue);
  const cashValid = cashAmount !== null && cashAmount >= 0 && cashAmount <= totalPayable;
  const needsWallet = cashValid && cashAmount !== null && cashAmount > 0;
  const walletValid = !needsWallet || walletCode !== "";
  const bankPreview =
    cashValid && cashAmount !== null ? normalizeMoney(Math.max(0, totalPayable - cashAmount)) : null;

  const selectedCashSource = cashWallets.find((wallet) => wallet.code === walletCode);
  const selectedBankSource = fundingQuery.data?.bank_sources.find(
    (source) => source.provider === bankProvider,
  );
  const cashFundsValid =
    !needsWallet ||
    !fundingQuery.isSuccess ||
    (selectedCashSource !== undefined &&
      cashAmount !== null &&
      cashAmount <= moneyValue(selectedCashSource.available));
  const bankFundsValid =
    bankPreview === null ||
    bankPreview <= 0 ||
    !fundingQuery.isSuccess ||
    (selectedBankSource?.is_configured === true &&
      bankPreview <= moneyValue(selectedBankSource.available));
  const currentWalletId = selectedCashSource?.id ?? null;
  const cashDirty =
    cashAmount === null ||
    normalizeMoney(cashAmount) !== normalizeMoney(payoutCashTotal) ||
    (needsWallet && currentWalletId !== savedWalletId);

  // Ориентир: сколько ЗП пойдёт на каждую статью ДДС (фактически проводится по «Выплатить»
  // по выбранным сотрудникам; канал нал/банк определяется через Сейф, не на уровне статьи).
  const previewBuckets = useMemo(
    () =>
      (allocationQuery.data?.buckets ?? []).map((bucket) => ({
        code: bucket.article_code,
        name: bucket.article_name,
        total: moneyValue(bucket.total),
      })),
    [allocationQuery.data?.buckets],
  );

  const invalidatePayoutQueries = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ["payroll-admin-run", runId] }),
      queryClient.invalidateQueries({ queryKey: ["payroll-admin-run-lines", runId] }),
      queryClient.invalidateQueries({ queryKey: ["admin-run-bank-draft", runId] }),
      queryClient.invalidateQueries({ queryKey: ["admin-run-payout-allocation", runId] }),
      queryClient.invalidateQueries({ queryKey: ["run-funding-sources", runId] }),
    ]);

  const hasBank = bankPreview !== null && bankPreview > 0;

  const submitMutation = useMutation({
    mutationFn: async () => {
      if (cashDirty && cashAmount !== null) {
        await setRunPayoutCash(
          runId,
          normalizeMoney(cashAmount),
          needsWallet ? walletCode : null,
          bankProvider,
        );
      }
      return createRunBankDraft(runId, bankProvider);
    },
    onSuccess: async (next) => {
      await invalidatePayoutQueries();
      setOpen(false);
      toast.success(
        hasBank
          ? next
            ? draft
              ? "Черновик обновлён"
              : "Черновик сформирован"
            : "Сплит сохранён"
          : "Сплит сохранён — черновик не требуется",
      );
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сформировать выплату")),
  });

  const canSubmit =
    cashValid &&
    walletValid &&
    cashFundsValid &&
    bankFundsValid &&
    fundingQuery.isSuccess &&
    channelPerms.bank_draft &&
    !submitMutation.isPending;

  const actionLabel = hasBank
    ? draft
      ? "Обновить черновик"
      : "Сформировать черновик"
    : "Провести наличными";

  const chipCls = (active: boolean) =>
    cn(
      "rounded-full border px-3 py-1 text-xs",
      active
        ? "border-emerald-300 bg-emerald-50 text-emerald-800"
        : "border-border text-muted-foreground hover:bg-muted",
    );

  return (
    <>
      <button
        className="flex w-full items-center justify-between gap-2 rounded-lg border bg-card p-3 text-left hover:bg-muted/40"
        onClick={() => setOpen(true)}
        type="button"
      >
        <span className="flex items-center gap-2 text-sm font-medium">
          <Landmark className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
          Выплата администрации — сплит и черновик в банк
        </span>
        <span className="flex items-center gap-2 text-xs text-muted-foreground">
          наличными {formatMoney(payoutCashTotal)} · безнал {formatMoney(savedBankPreview)} ·{" "}
          {draft ? "черновик создан" : "не создан"}
          <ChevronRight className="h-4 w-4" aria-hidden="true" />
        </span>
      </button>

      <Dialog onOpenChange={setOpen} open={open}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Выплата администрации</DialogTitle>
            <DialogDescription>
              К выплате {formatMoney(totalPayable)}. Наличная часть — с выбранного счёта, остаток
              уходит черновиком на счёт ИП. В ДДС зарплата проводится по статьям при «Выплатить».
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="admin-payout-cash">Наличными</Label>
              <Input
                className={cn(!cashFundsValid && "border-destructive focus-visible:ring-destructive")}
                disabled={submitMutation.isPending}
                id="admin-payout-cash"
                inputMode="decimal"
                onChange={(event) => setCashValue(event.target.value)}
                placeholder="0"
                value={cashValue}
              />
              {needsWallet ? (
                <div className="flex flex-wrap gap-2 pt-1">
                  <span className="self-center text-xs text-muted-foreground">Откуда:</span>
                  {cashWallets.map((wallet) => (
                    <button
                      className={chipCls(walletCode === wallet.code)}
                      key={wallet.id}
                      onClick={() => setWalletCode(wallet.code)}
                      type="button"
                    >
                      {wallet.name} · доступно {formatMoney(moneyValue(wallet.available))}
                    </button>
                  ))}
                </div>
              ) : null}
              {!cashValid ? (
                <p className="text-xs text-destructive">
                  Введите сумму от 0 до {formatMoney(totalPayable)}.
                </p>
              ) : needsWallet && !walletValid ? (
                <p className="text-xs text-destructive">
                  Выберите наличный счёт (Сейф или Торговая касса Черникова).
                </p>
              ) : !cashFundsValid && selectedCashSource ? (
                <p className="text-xs text-destructive">
                  На счёте доступно {formatMoney(moneyValue(selectedCashSource.available))}. Уменьшите
                  наличную часть.
                </p>
              ) : null}
            </div>

            <div
              className={cn(
                "flex items-center justify-between rounded-md border bg-muted/30 px-3 py-2 text-sm",
                !bankFundsValid && "border-destructive bg-destructive/5",
              )}
            >
              <span className="text-muted-foreground">Безналичный остаток → черновик на счёт ИП</span>
              <span className="font-medium tabular-nums">
                {bankPreview === null ? "—" : formatMoney(bankPreview)}
              </span>
            </div>

            {hasBank && channelPerms.bank_draft ? (
              <div className="space-y-2">
                <Label>Банк для черновика</Label>
                <div className="flex flex-wrap gap-2">
                  <button
                    className={chipCls(bankProvider === "tbank")}
                    onClick={() => setBankProvider("tbank")}
                    type="button"
                  >
                    Тинькофф
                  </button>
                  <button
                    className={chipCls(bankProvider === "sber")}
                    onClick={() => setBankProvider("sber")}
                    type="button"
                  >
                    Сбербанк → Сейф
                  </button>
                </div>
                {selectedBankSource ? (
                  <p
                    className={cn(
                      "text-xs text-muted-foreground",
                      !bankFundsValid && "text-destructive",
                    )}
                  >
                    Доступно на счёте: {formatMoney(moneyValue(selectedBankSource.available))}
                    {!selectedBankSource.is_configured ? " · счёт не настроен" : ""}
                  </p>
                ) : null}
              </div>
            ) : null}

            {previewBuckets.length > 0 ? (
              <details className="group rounded-md border bg-card text-sm">
                <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-xs text-muted-foreground [&::-webkit-details-marker]:hidden">
                  <ChevronRight
                    className="h-3.5 w-3.5 transition-transform group-open:rotate-90"
                    aria-hidden="true"
                  />
                  Разнесение ЗП по статьям ДДС (по факту «Выплатить»)
                </summary>
                <table className="w-full border-t">
                  <tbody>
                    {previewBuckets.map((bucket) => (
                      <tr key={bucket.code} className="border-b last:border-b-0">
                        <td className="px-3 py-1.5">{bucket.name}</td>
                        <td className="px-3 py-1.5 text-right tabular-nums">
                          {formatMoney(bucket.total)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </details>
            ) : null}

            {draft?.last_error ? (
              <p className="text-xs text-destructive">{draft.last_error}</p>
            ) : null}
          </div>

          <DialogFooter>
            <Button onClick={() => setOpen(false)} type="button" variant="outline">
              Отмена
            </Button>
            <Button
              disabled={!canSubmit}
              onClick={() => submitMutation.mutate()}
              title={channelPerms.bank_draft ? undefined : "Нет права на формирование банк-черновиков"}
              type="button"
            >
              {submitMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
              ) : (
                <Landmark size={15} aria-hidden="true" />
              )}
              {actionLabel}
              {hasBank ? ` · ${formatMoney(bankPreview ?? 0)}` : ""}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
