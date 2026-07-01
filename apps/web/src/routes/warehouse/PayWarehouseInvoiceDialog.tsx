import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
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
import { apiErrorMessage } from "@/lib/api";

import {
  getExpenseArticles,
  getPrepayments,
  getWallets,
  settleInvoiceFromPrepayment,
} from "../counterparties/api";
import { formatDate, formatRub } from "../counterparties/shared";
import {
  confirmInvoiceMatch,
  getInvoiceMatchSuggestions,
  getWarehouseInvoice,
  payInvoiceSplit,
  type MatchCandidate,
} from "./api";

const DEFAULT_ARTICLE = "default";

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

const TIER_LABEL: Record<number, string> = {
  1: "точно по времени",
  2: "тот же день",
  3: "в окне",
  4: "по дате",
};

function tierClass(tier: number): string {
  if (tier <= 2) return "border-emerald-200 bg-emerald-50 text-emerald-700";
  if (tier === 3) return "border-sky-200 bg-sky-50 text-sky-700";
  return "border-amber-200 bg-amber-50 text-amber-700";
}

type CashRow = {
  uid: string;
  wallet_id: string;
  amount: string;
  operation_date: string;
  article_id: string;
};

function emptyCashRow(): CashRow {
  return {
    uid: crypto.randomUUID(),
    wallet_id: "",
    amount: "",
    operation_date: today(),
    article_id: DEFAULT_ARTICLE,
  };
}

export function PayWarehouseInvoiceDialog({
  invoiceId,
  onOpenChange,
}: {
  invoiceId: string | null;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const open = Boolean(invoiceId);

  const detailQuery = useQuery({
    queryKey: ["wh", "invoice", invoiceId],
    queryFn: () => getWarehouseInvoice(invoiceId!),
    enabled: open,
  });
  const walletsQuery = useQuery({ queryKey: ["cp", "wallets"], queryFn: getWallets, enabled: open });
  const articlesQuery = useQuery({
    queryKey: ["cp", "expense-articles"],
    queryFn: getExpenseArticles,
    enabled: open,
  });
  const matchQuery = useQuery({
    queryKey: ["wh", "match", invoiceId],
    queryFn: () => getInvoiceMatchSuggestions(invoiceId!),
    enabled: open,
  });

  const detail = detailQuery.data;
  const counterpartyId = detail?.counterparty_id;
  const prepaymentsQuery = useQuery({
    queryKey: ["cp", "prepayments", counterpartyId],
    queryFn: () => getPrepayments({ counterparty_id: counterpartyId!, only_open: true }),
    enabled: open && Boolean(counterpartyId),
  });
  // Гасим из самой ранней открытой предоплаты (FIFO). Сумма = min(остаток, предоплата) — берёт бэк.
  const openPrepayments = [...(prepaymentsQuery.data ?? [])].sort((a, b) =>
    a.created_at.localeCompare(b.created_at),
  );
  const prepayAvailable = openPrepayments.reduce(
    (sum, p) => sum + (p.amount - p.amount_settled),
    0,
  );
  const oldestPrepayment = openPrepayments[0];

  const remaining = detail?.remaining ?? 0;
  const staffAmount = detail?.staff_amount ?? 0;
  const productionAmount = detail?.production_amount ?? 0;
  const candidates = matchQuery.data?.candidates ?? [];
  // Персонал-разнесение разносит ПОЛНУЮ сумму накладной → доступно только если ещё ничего
  // не оплачено (иначе Σ частей > остаток и бэк отдаёт 409).
  const isFullyUnpaid = remaining + 0.005 >= (detail?.amount ?? 0);
  const canSplitStaff = staffAmount > 0 && isFullyUnpaid;

  const [selectedBankOps, setSelectedBankOps] = useState<Set<string>>(new Set());
  const [cashRows, setCashRows] = useState<CashRow[]>([]);
  const [splitStaff, setSplitStaff] = useState(false);
  const [enrich, setEnrich] = useState(true);

  useEffect(() => {
    setSelectedBankOps(new Set());
    setCashRows([]);
    setSplitStaff(false);
    setEnrich(true);
  }, [invoiceId]);

  // Накладную с «тратами на персонал» по умолчанию разносим: иначе персональная часть
  // легла бы одной статьёй «Оплата поставщикам» (баг, из-за которого 7744 ушла лумпом).
  useEffect(() => {
    if (!detail) return;
    const hasStaff = (detail.staff_amount ?? 0) > 0;
    const fullyUnpaid = (detail.remaining ?? 0) + 0.005 >= (detail.amount ?? 0);
    setSplitStaff(hasStaff && fullyUnpaid);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail?.id]);

  function toggleBankOp(id: string) {
    setSelectedBankOps((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const candidateById = new Map(candidates.map((c) => [c.bank_operation_id, c]));
  const bankTotal = [...selectedBankOps].reduce(
    (sum, id) => sum + (candidateById.get(id)?.amount ?? 0),
    0,
  );
  const cashTotal = cashRows.reduce((sum, row) => sum + (Number(row.amount) || 0), 0);
  const selectedTotal = bankTotal + cashTotal;

  const cashRowsValid = cashRows.every(
    (row) => row.wallet_id && Number(row.amount) > 0 && row.operation_date,
  );

  const payMutation = useMutation({
    mutationFn: async () => {
      if (splitStaff) {
        // Полная оплата налом одним кошельком, бэк разносит на производство/персонал.
        const row = cashRows[0];
        return payInvoiceSplit(invoiceId!, {
          cash_parts: [
            {
              wallet_id: row.wallet_id,
              amount: remaining,
              operation_date: row.operation_date,
            },
          ],
          split_staff: true,
        });
      }
      const bankOps = [...selectedBankOps];
      const cashParts = cashRows.map((row) => ({
        wallet_id: row.wallet_id,
        amount: Number(row.amount),
        operation_date: row.operation_date,
        article_id: row.article_id === DEFAULT_ARTICLE ? null : row.article_id,
      }));
      // Один банк-кандидат целиком, без налички, с обогащением → быстрый банк-мэтч
      // (он подтянет реквизиты контрагента; сплит этого не делает).
      if (bankOps.length === 1 && cashParts.length === 0 && enrich) {
        await confirmInvoiceMatch({
          invoice_id: invoiceId!,
          bank_operation_id: bankOps[0],
          enrich: true,
        });
        return getWarehouseInvoice(invoiceId!);
      }
      return payInvoiceSplit(invoiceId!, {
        bank_parts: bankOps.map((id) => ({ bank_operation_id: id })),
        cash_parts: cashParts,
      });
    },
    onSuccess: async (updated) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["cp"] }),
        queryClient.invalidateQueries({ queryKey: ["wh"] }),
      ]);
      onOpenChange(false);
      toast.success(
        updated.payment_status === "paid"
          ? "Накладная оплачена"
          : `Оплачено частично, остаток ${formatRub(updated.remaining)}`,
      );
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось провести оплату")),
  });

  const settleMutation = useMutation({
    mutationFn: () =>
      settleInvoiceFromPrepayment(invoiceId!, { prepayment_id: oldestPrepayment!.id }),
    onSuccess: async (updated) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["cp"] }),
        queryClient.invalidateQueries({ queryKey: ["wh"] }),
      ]);
      onOpenChange(false);
      toast.success(
        updated.payment_status === "paid"
          ? "Накладная погашена из предоплаты"
          : `Погашено из предоплаты, остаток ${formatRub(updated.remaining)}`,
      );
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось погасить из предоплаты")),
  });

  const canSubmit = splitStaff
    ? cashRows.length === 1 && Boolean(cashRows[0]?.wallet_id) && canSplitStaff
    : selectedTotal > 0 && selectedTotal <= remaining + 0.005 && cashRowsValid;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Оплата накладной</DialogTitle>
        </DialogHeader>
        {detail ? (
          <div className="grid gap-4">
            <div className="text-sm text-muted-foreground">
              {detail.counterparty_name} · накладная {detail.number ?? "—"} · к оплате{" "}
              <span className="font-medium tabular-nums text-foreground">
                {formatRub(remaining)}
              </span>
              {staffAmount > 0 ? (
                <span className="ml-1">
                  (производство {formatRub(productionAmount)} · персонал {formatRub(staffAmount)})
                </span>
              ) : null}
            </div>

            {prepayAvailable > 0 && remaining > 0 ? (
              <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-sky-200 bg-sky-50/60 p-3 text-sm">
                <span className="text-sky-900">
                  Доступна предоплата поставщика:{" "}
                  <span className="font-medium tabular-nums">{formatRub(prepayAvailable)}</span>
                  {" — "}можно погасить без движения денег
                </span>
                <Button
                  size="sm"
                  variant="outline"
                  className="border-sky-300"
                  disabled={settleMutation.isPending || !oldestPrepayment}
                  onClick={() => settleMutation.mutate()}
                >
                  {settleMutation.isPending ? (
                    <LoaderCircle className="animate-spin" size={14} aria-hidden="true" />
                  ) : null}
                  Погасить из предоплаты
                </Button>
              </div>
            ) : null}

            {canSplitStaff ? (
              <label className="flex items-start gap-2 rounded-md border border-violet-200 bg-violet-50/50 p-3 text-sm">
                <Checkbox
                  checked={splitStaff}
                  onChange={() => setSplitStaff((v) => !v)}
                  aria-label="Разнести персонал"
                />
                <span>
                  Разнести персонал отдельной статьёй ДДС
                  <span className="block text-xs text-muted-foreground">
                    Оплата полностью наличными из одного кошелька: производство и персонал уйдут
                    разными статьями. Банковские источники при этом недоступны.
                  </span>
                </span>
              </label>
            ) : null}

            {canSplitStaff && !splitStaff ? (
              <p className="rounded-md border border-amber-200 bg-amber-50 p-2 text-xs text-amber-700">
                ⚠️ Без разнесения персональная часть ({formatRub(staffAmount)}) ляжет статьёй
                «Оплата поставщикам». Для трат на персонал включите разнесение выше.
              </p>
            ) : null}

            {splitStaff ? (
              <div className="grid gap-2">
                <Label>Счёт (кошелёк ДДС)</Label>
                <div className="grid grid-cols-2 gap-3">
                  <Select
                    value={cashRows[0]?.wallet_id ?? ""}
                    onValueChange={(value) =>
                      setCashRows([{ ...emptyCashRow(), ...cashRows[0], wallet_id: value }])
                    }
                  >
                    <SelectTrigger>
                      <SelectValue placeholder="Выберите счёт" />
                    </SelectTrigger>
                    <SelectContent>
                      {(walletsQuery.data ?? []).map((wallet) => (
                        <SelectItem key={wallet.id} value={wallet.id}>
                          {wallet.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <Input
                    type="date"
                    value={cashRows[0]?.operation_date ?? today()}
                    onChange={(event) =>
                      setCashRows([
                        { ...emptyCashRow(), ...cashRows[0], operation_date: event.target.value },
                      ])
                    }
                  />
                </div>
              </div>
            ) : (
              <>
                <div className="grid gap-2">
                  <div className="flex items-center justify-between">
                    <Label>Из банка (T-Bank)</Label>
                    {matchQuery.data?.confident ? (
                      <Badge className="border-emerald-200 bg-emerald-50 text-emerald-700">
                        уверенное совпадение
                      </Badge>
                    ) : null}
                  </div>
                  {candidates.length === 0 ? (
                    <p className="text-xs text-muted-foreground">
                      {matchQuery.isLoading
                        ? "Подбираем операции…"
                        : "Подходящих банковских операций не найдено."}
                    </p>
                  ) : (
                    <div className="grid gap-2">
                      {candidates.map((candidate: MatchCandidate) => (
                        <label
                          key={candidate.bank_operation_id}
                          className="flex items-center gap-3 rounded-md border border-sky-200 bg-sky-50/40 p-2 text-sm"
                        >
                          <Checkbox
                            checked={selectedBankOps.has(candidate.bank_operation_id)}
                            onChange={() => toggleBankOp(candidate.bank_operation_id)}
                            aria-label="Выбрать операцию"
                          />
                          <span className="font-medium tabular-nums">
                            {formatRub(candidate.amount)}
                          </span>
                          <Badge variant="outline" className={tierClass(candidate.tier)}>
                            {TIER_LABEL[candidate.tier] ?? "совпадение"}
                            {candidate.minutes_delta != null
                              ? ` · ±${candidate.minutes_delta} мин`
                              : ""}
                          </Badge>
                          <span className="text-muted-foreground">
                            {candidate.official_name ?? "получатель не указан"}
                            {candidate.inn ? ` · ИНН ${candidate.inn}` : ""}
                          </span>
                          <span className="ml-auto text-xs text-muted-foreground">
                            {formatDate(candidate.operation_date)}
                          </span>
                        </label>
                      ))}
                      <label className="flex items-center gap-2 text-xs text-muted-foreground">
                        <Checkbox
                          checked={enrich}
                          onChange={() => setEnrich((v) => !v)}
                          aria-label="Обновить реквизиты"
                        />
                        Обновить имя и реквизиты контрагента по банковской операции
                      </label>
                    </div>
                  )}
                </div>

                <div className="grid gap-2">
                  <div className="flex items-center justify-between">
                    <Label>Наличными / со счёта</Label>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => setCashRows((rows) => [...rows, emptyCashRow()])}
                    >
                      <Plus size={14} aria-hidden="true" />
                      Источник
                    </Button>
                  </div>
                  {cashRows.map((row, index) => (
                    <div
                      key={row.uid}
                      className="grid grid-cols-[1fr_1fr_auto] items-end gap-2 rounded-md border p-2"
                    >
                      <div className="grid gap-1">
                        <span className="text-xs text-muted-foreground">Счёт</span>
                        <Select
                          value={row.wallet_id}
                          onValueChange={(value) =>
                            setCashRows((rows) =>
                              rows.map((r, i) => (i === index ? { ...r, wallet_id: value } : r)),
                            )
                          }
                        >
                          <SelectTrigger>
                            <SelectValue placeholder="Счёт" />
                          </SelectTrigger>
                          <SelectContent>
                            {(walletsQuery.data ?? []).map((wallet) => (
                              <SelectItem key={wallet.id} value={wallet.id}>
                                {wallet.name}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </div>
                      <div className="grid gap-1">
                        <span className="text-xs text-muted-foreground">Статья ДДС</span>
                        <Select
                          value={row.article_id}
                          onValueChange={(value) =>
                            setCashRows((rows) =>
                              rows.map((r, i) => (i === index ? { ...r, article_id: value } : r)),
                            )
                          }
                        >
                          <SelectTrigger>
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value={DEFAULT_ARTICLE}>
                              Оплата поставщикам (по умолчанию)
                            </SelectItem>
                            {(articlesQuery.data ?? []).map((article) => (
                              <SelectItem key={article.id} value={article.id}>
                                {article.name}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </div>
                      <Button
                        size="icon"
                        variant="ghost"
                        onClick={() =>
                          setCashRows((rows) => rows.filter((_, i) => i !== index))
                        }
                        aria-label="Удалить источник"
                      >
                        <Trash2 size={15} aria-hidden="true" />
                      </Button>
                      <div className="grid gap-1">
                        <span className="text-xs text-muted-foreground">Сумма, ₽</span>
                        <Input
                          type="number"
                          value={row.amount}
                          onChange={(event) =>
                            setCashRows((rows) =>
                              rows.map((r, i) =>
                                i === index ? { ...r, amount: event.target.value } : r,
                              ),
                            )
                          }
                        />
                      </div>
                      <div className="grid gap-1">
                        <span className="text-xs text-muted-foreground">Дата</span>
                        <Input
                          type="date"
                          value={row.operation_date}
                          onChange={(event) =>
                            setCashRows((rows) =>
                              rows.map((r, i) =>
                                i === index ? { ...r, operation_date: event.target.value } : r,
                              ),
                            )
                          }
                        />
                      </div>
                    </div>
                  ))}
                </div>

                <div className="flex items-center justify-between border-t pt-2 text-sm">
                  <span className="text-muted-foreground">Выбрано к оплате</span>
                  <span
                    className={
                      selectedTotal > remaining + 0.005
                        ? "font-medium tabular-nums text-red-600"
                        : "font-medium tabular-nums"
                    }
                  >
                    {formatRub(selectedTotal)} из {formatRub(remaining)}
                  </span>
                </div>
                {selectedTotal > remaining + 0.005 ? (
                  <p className="text-xs text-red-600">Сумма источников превышает остаток.</p>
                ) : null}
              </>
            )}
          </div>
        ) : (
          <div className="flex justify-center py-8">
            <LoaderCircle className="animate-spin text-muted-foreground" aria-hidden="true" />
          </div>
        )}
        <DialogFooter>
          <Button
            disabled={!canSubmit || payMutation.isPending}
            onClick={() => payMutation.mutate()}
          >
            {payMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : null}
            Оплатить
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
