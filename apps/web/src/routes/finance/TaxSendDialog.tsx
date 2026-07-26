import { Loader2, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
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
import { apiErrorMessage } from "@/lib/api";
import { sendTaxPaymentDraftToBank } from "@/routes/taxes/api";

import type { PaymentRow } from "./payments-api";

const money = new Intl.NumberFormat("ru-RU", {
  style: "currency",
  currency: "RUB",
  maximumFractionDigits: 2,
});

type Requisites = {
  recipientName?: string;
  inn?: string;
  kpp?: string;
  bankAcnt?: string;
  bankBik?: string;
  bankName?: string;
  kbk?: string;
};

// Отправка налогового платежа (ЕНП) в банк из окна активных платежей. Реквизиты получателя —
// фиксированная константа ФНС (owner-locked): показываем их ТОЛЬКО для сверки, менять нельзя.
// Редактируемы лишь сумма и назначение — как и договаривались с владельцем.
export function TaxSendDialog({
  row,
  onClose,
}: {
  row: PaymentRow | null;
  onClose: () => void;
}) {
  const [amount, setAmount] = useState("");
  const [purpose, setPurpose] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (row) {
      setAmount(String(row.amount));
      setPurpose(
        typeof row.extra.purpose === "string" ? row.extra.purpose : "Единый налоговый платеж",
      );
    }
    // сброс полей при смене платежа
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [row?.id]);

  const reqs = (row?.extra.requisites ?? {}) as Requisites;
  const lastError = typeof row?.extra.last_error === "string" ? row.extra.last_error : null;
  const value = Number(amount.replace(",", ".").replace(/\s/g, ""));
  const valid = value > 0;

  async function submit() {
    if (!row || !valid) return;
    setSubmitting(true);
    try {
      await sendTaxPaymentDraftToBank(row.ref_id, {
        amount: value,
        purpose: purpose.trim() || undefined,
      });
      toast.success("Платёжка отправлена в Т-Банк", {
        description: "Подтвердите её в банк-клиенте — деньги уйдут только после этого.",
      });
      onClose();
    } catch (error) {
      toast.error(apiErrorMessage(error, "Не удалось отправить платёж в банк"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog
      open={row !== null}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Отправить налог в банк</DialogTitle>
          <DialogDescription>{row?.title}</DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          {lastError ? (
            <div className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
              Прошлая попытка отклонена банком: {lastError}
            </div>
          ) : null}

          <div className="rounded-md border bg-muted/40 p-3 text-sm">
            <div className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
              <ShieldCheck size={13} /> Получатель — реквизиты ФНС, менять нельзя
            </div>
            <ReqRow label="Получатель" value={reqs.recipientName} />
            <ReqRow label="ИНН / КПП" value={[reqs.inn, reqs.kpp].filter(Boolean).join(" / ")} />
            <ReqRow label="Счёт" value={reqs.bankAcnt} />
            <ReqRow label="БИК" value={reqs.bankBik} />
            <ReqRow label="КБК" value={reqs.kbk} />
          </div>

          <div className="space-y-1.5">
            <label className="text-sm font-medium" htmlFor="tax-amount">
              Сумма платежа
            </label>
            <Input
              id="tax-amount"
              inputMode="decimal"
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              className="tabular-nums"
            />
            {amount && !valid ? (
              <p className="text-xs text-rose-600">Сумма должна быть больше 0.</p>
            ) : null}
          </div>

          <div className="space-y-1.5">
            <label className="text-sm font-medium" htmlFor="tax-purpose">
              Назначение платежа
            </label>
            <Input
              id="tax-purpose"
              value={purpose}
              onChange={(e) => setPurpose(e.target.value)}
            />
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={submitting}>
            Отмена
          </Button>
          <Button onClick={submit} disabled={!valid || submitting}>
            {submitting ? <Loader2 className="mr-1.5 animate-spin" size={15} /> : null}
            Отправить в банк {valid ? money.format(value) : ""}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ReqRow({ label, value }: { label: string; value?: string | null }) {
  return (
    <div className="flex justify-between gap-3 py-0.5">
      <span className="shrink-0 text-muted-foreground">{label}</span>
      <span className="truncate text-right font-medium tabular-nums">{value || "—"}</span>
    </div>
  );
}
