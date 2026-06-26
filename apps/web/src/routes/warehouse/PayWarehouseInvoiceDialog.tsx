import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Plus, Trash2 } from "lucide-react";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { apiErrorMessage } from "@/lib/api";

import { getPrepayments, getWallets, settleInvoiceFromPrepayment } from "../counterparties/api";
import { formatRub } from "../counterparties/shared";
import { getWarehouseInvoice, payInvoiceSplit } from "./api";

// Накладные оплачиваем только наличными / со счёта: Сейф и Торговая касса. Банк — через
// «Отправить в банк»; по Сберу накладные не платим. Фильтруем кошельки по типу.
const CASH_WALLET_TYPES = new Set(["cash_safe", "store_cash"]);

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

type CashRow = {
  uid: string;
  wallet_id: string;
  amount: string;
  operation_date: string;
};

function emptyCashRow(amount = ""): CashRow {
  return { uid: crypto.randomUUID(), wallet_id: "", amount, operation_date: today() };
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
  const walletsQuery = useQuery({
    queryKey: ["cp", "wallets"],
    queryFn: getWallets,
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
  const cashWallets = (walletsQuery.data ?? []).filter((w) => CASH_WALLET_TYPES.has(w.type));

  const [cashRows, setCashRows] = useState<CashRow[]>([]);

  // Первая строка открыта сразу и предзаполнена остатком + сегодняшней датой.
  useEffect(() => {
    if (!detail) return;
    setCashRows([emptyCashRow(detail.remaining > 0 ? String(detail.remaining) : "")]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail?.id]);

  const cashTotal = cashRows.reduce((sum, row) => sum + (Number(row.amount) || 0), 0);
  const rowsValid =
    cashRows.length > 0 &&
    cashRows.every((row) => row.wallet_id && Number(row.amount) > 0 && row.operation_date);
  const canSubmit = rowsValid && cashTotal > 0 && cashTotal <= remaining + 0.005;

  function patchRow(index: number, patch: Partial<CashRow>) {
    setCashRows((rows) => rows.map((r, i) => (i === index ? { ...r, ...patch } : r)));
  }

  const payMutation = useMutation({
    mutationFn: () =>
      payInvoiceSplit(invoiceId!, {
        split_by_lines: true,
        cash_parts: cashRows.map((row) => ({
          wallet_id: row.wallet_id,
          amount: Number(row.amount),
          operation_date: row.operation_date,
        })),
      }),
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

            <div className="grid gap-2">
              {cashRows.map((row, index) => (
                <div
                  key={row.uid}
                  className="grid grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_minmax(0,1fr)_auto] items-end gap-2"
                >
                  <div className="grid gap-1">
                    <span className="text-xs text-muted-foreground">Счёт</span>
                    <Select
                      value={row.wallet_id}
                      onValueChange={(value) => patchRow(index, { wallet_id: value })}
                    >
                      <SelectTrigger>
                        <SelectValue placeholder="Сейф / Торговая касса" />
                      </SelectTrigger>
                      <SelectContent>
                        {cashWallets.map((wallet) => (
                          <SelectItem key={wallet.id} value={wallet.id}>
                            {wallet.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="grid gap-1">
                    <span className="text-xs text-muted-foreground">Сумма, ₽</span>
                    <Input
                      type="number"
                      inputMode="decimal"
                      value={row.amount}
                      onChange={(event) => patchRow(index, { amount: event.target.value })}
                    />
                  </div>
                  <div className="grid gap-1">
                    <span className="text-xs text-muted-foreground">Дата</span>
                    <Input
                      type="date"
                      value={row.operation_date}
                      onChange={(event) => patchRow(index, { operation_date: event.target.value })}
                    />
                  </div>
                  {cashRows.length > 1 ? (
                    <Button
                      size="icon"
                      variant="ghost"
                      onClick={() => setCashRows((rows) => rows.filter((_, i) => i !== index))}
                      aria-label="Удалить источник"
                    >
                      <Trash2 size={15} aria-hidden="true" />
                    </Button>
                  ) : (
                    <span className="w-9" />
                  )}
                </div>
              ))}
              <Button
                size="sm"
                variant="ghost"
                className="justify-self-start"
                aria-label="Добавить источник"
                onClick={() => setCashRows((rows) => [...rows, emptyCashRow()])}
              >
                <Plus size={15} aria-hidden="true" />
              </Button>
            </div>

            <div className="flex items-center justify-between border-t pt-2 text-sm">
              <span className="text-muted-foreground">Выбрано к оплате</span>
              <span
                className={
                  cashTotal > remaining + 0.005
                    ? "font-medium tabular-nums text-red-600"
                    : "font-medium tabular-nums"
                }
              >
                {formatRub(cashTotal)} из {formatRub(remaining)}
              </span>
            </div>
            {cashTotal > remaining + 0.005 ? (
              <p className="text-xs text-red-600">Сумма источников превышает остаток.</p>
            ) : null}
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
