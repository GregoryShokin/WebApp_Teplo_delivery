import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Banknote, Landmark, LoaderCircle } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { apiErrorMessage } from "@/lib/api";

import { payInvoiceSplit } from "../warehouse/api";
import {
  CASH_PAYMENT_WALLET_CODES,
  createDraft,
  getWallets,
  type CounterpartyInvoice,
} from "./api";
import { formatRub } from "./shared";
import { todayIso } from "@/lib/date";


type PayMethod = "bank" | "cash";

/** Массовая оплата выбранных накладных: в банк (черновик Т-Банк, один контрагент)
 *  или наличными с выбранного счёта (Сейф / ТК Черникова) — по накладной за раз
 *  через проверенный pay-split (ДДС-проводки создаёт бэк). */
export function BulkPayDialog({
  invoices,
  open,
  onOpenChange,
  onDone,
}: {
  invoices: CounterpartyInvoice[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDone: () => void;
}) {
  const queryClient = useQueryClient();
  const [method, setMethod] = useState<PayMethod>("bank");
  const [walletId, setWalletId] = useState<string>("");
  const [progress, setProgress] = useState<string | null>(null);

  // Оплачиваемые: без банковского черновика и не оплаченные (draft/paid отсеяны и в inbox,
  // но страхуемся — диалог не должен слать уже уехавшее).
  const payable = useMemo(
    () => invoices.filter((item) => !item.draft_id && item.payment_status !== "paid"),
    [invoices],
  );
  const total = payable.reduce((sum, item) => sum + (item.remaining || item.amount), 0);
  const counterparties = new Set(payable.map((item) => item.counterparty_id));
  const servicePeriods = new Set(
    payable.map(
      (item) => `${item.service_period_start ?? "none"}:${item.service_period_end ?? "none"}`,
    ),
  );
  const bankAllowed =
    payable.length > 0 && counterparties.size === 1 && servicePeriods.size === 1;

  const walletsQuery = useQuery({ queryKey: ["cp", "wallets"], queryFn: getWallets, enabled: open });
  const cashWallets = (walletsQuery.data ?? []).filter((wallet) =>
    CASH_PAYMENT_WALLET_CODES.has(wallet.code),
  );

  useEffect(() => {
    if (!open) {
      setMethod("bank");
      setWalletId("");
      setProgress(null);
    }
  }, [open]);

  const bankMutation = useMutation({
    mutationFn: () => createDraft(payable.map((item) => item.id)),
    onSuccess: async (draft) => {
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      await queryClient.invalidateQueries({ queryKey: ["wh"] });
      toast.success(
        draft.pays_via_safe
          ? `Черновик выплаты на карту ИП создан (неофициальный поставщик) — ${formatRub(draft.amount)}`
          : `Черновик на ${formatRub(draft.amount)} отправлен в банк (Т-Банк)`,
      );
      onDone();
      onOpenChange(false);
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось отправить в банк")),
  });

  const cashMutation = useMutation({
    mutationFn: async () => {
      const date = todayIso();
      const failed: string[] = [];
      let paid = 0;
      for (const [index, invoice] of payable.entries()) {
        setProgress(`Оплата ${index + 1} из ${payable.length}…`);
        try {
          await payInvoiceSplit(invoice.id, {
            cash_parts: [
              {
                wallet_id: walletId,
                amount: invoice.remaining || invoice.amount,
                operation_date: date,
              },
            ],
          });
          paid += 1;
        } catch (error) {
          failed.push(
            `№${invoice.number ?? "—"} (${invoice.counterparty_name}): ${apiErrorMessage(
              error,
              "ошибка",
            )}`,
          );
        }
      }
      return { paid, failed };
    },
    onSuccess: async ({ paid, failed }) => {
      setProgress(null);
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      await queryClient.invalidateQueries({ queryKey: ["wh"] });
      if (paid > 0) toast.success(`Оплачено наличными: ${paid} накл.`);
      if (failed.length > 0) {
        toast.error(`Не оплачено ${failed.length}: ${failed[0]}${failed.length > 1 ? "…" : ""}`);
      }
      if (failed.length === 0) {
        onDone();
        onOpenChange(false);
      }
    },
    onError: (error) => {
      setProgress(null);
      toast.error(apiErrorMessage(error, "Не удалось оплатить наличными"));
    },
  });

  const busy = bankMutation.isPending || cashMutation.isPending;

  return (
    <Dialog open={open} onOpenChange={(next) => !busy && onOpenChange(next)}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>
            Оплатить {payable.length} накл. на {formatRub(total)}
          </DialogTitle>
        </DialogHeader>

        <div className="grid gap-2">
          <button
            type="button"
            className={`flex items-start gap-3 rounded-md border p-3 text-left transition-colors ${
              method === "bank" ? "border-primary bg-primary/5" : "hover:bg-muted/50"
            } ${bankAllowed ? "" : "cursor-not-allowed opacity-50"}`}
            onClick={() => bankAllowed && setMethod("bank")}
            disabled={busy}
          >
            <Landmark size={18} className="mt-0.5 shrink-0" aria-hidden="true" />
            <span>
              <span className="font-medium">Отправить в банк</span>
              <span className="block text-xs text-muted-foreground">
                Черновик платежа в Т-Банке — останется подписать в банке.
                {bankAllowed
                  ? ""
                  : counterparties.size > 1
                    ? " Доступно для накладных одного контрагента."
                    : " Счета с разными периодами услуг нужно отправить отдельно."}
              </span>
            </span>
          </button>
          <button
            type="button"
            className={`flex items-start gap-3 rounded-md border p-3 text-left transition-colors ${
              method === "cash" ? "border-primary bg-primary/5" : "hover:bg-muted/50"
            }`}
            onClick={() => setMethod("cash")}
            disabled={busy}
          >
            <Banknote size={18} className="mt-0.5 shrink-0" aria-hidden="true" />
            <span>
              <span className="font-medium">Наличными со счёта</span>
              <span className="block text-xs text-muted-foreground">
                Сразу проведёт оплату и спишет из выбранного счёта (Сейф или торговая касса).
              </span>
            </span>
          </button>
        </div>

        {method === "cash" ? (
          <div className="grid gap-2">
            <Label>Счёт списания</Label>
            <Select value={walletId} onValueChange={setWalletId} disabled={busy}>
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
          </div>
        ) : null}

        {progress ? <p className="text-sm text-muted-foreground">{progress}</p> : null}

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={busy}>
            Отмена
          </Button>
          {method === "bank" ? (
            <Button
              disabled={!bankAllowed || busy}
              onClick={() => bankMutation.mutate()}
            >
              {bankMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
              ) : (
                <Landmark size={15} aria-hidden="true" />
              )}
              Отправить в банк
            </Button>
          ) : (
            <Button
              disabled={!walletId || payable.length === 0 || busy}
              onClick={() => cashMutation.mutate()}
            >
              {cashMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={15} aria-hidden="true" />
              ) : (
                <Banknote size={15} aria-hidden="true" />
              )}
              Оплатить наличными
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
