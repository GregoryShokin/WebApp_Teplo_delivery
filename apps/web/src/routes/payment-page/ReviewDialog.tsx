import { useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Combobox } from "@/components/ui/combobox";
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
import { apiErrorMessage } from "@/lib/api";

import {
  confirmIntake,
  fetchIntakePdfUrl,
  type CounterpartyOption,
  type PaymentIntake,
} from "./api";

function Field({
  label,
  value,
  onChange,
  type = "text",
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  type?: string;
  placeholder?: string;
}) {
  return (
    <div className="grid gap-1">
      <Label className="text-xs text-muted-foreground">{label}</Label>
      <Input
        type={type}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}

export function ReviewDialog({
  intake,
  counterpartyOptions,
  onClose,
}: {
  intake: PaymentIntake;
  counterpartyOptions: CounterpartyOption[];
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const req = useMemo(() => intake.requisites ?? {}, [intake.requisites]);

  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const [mode, setMode] = useState<"existing" | "new">(
    intake.counterparty_id ? "existing" : "new",
  );
  const [counterpartyId, setCounterpartyId] = useState(intake.counterparty_id ?? "");
  const [newName, setNewName] = useState(intake.recipient_name ?? req.recipientName ?? "");
  const [newInn, setNewInn] = useState(intake.inn ?? req.inn ?? "");
  const [amount, setAmount] = useState(intake.amount ?? "");
  const [invoiceNumber, setInvoiceNumber] = useState(intake.invoice_number ?? "");
  const [invoiceDate, setInvoiceDate] = useState(intake.invoice_date ?? "");
  const [r, setR] = useState({
    recipientName: req.recipientName ?? intake.recipient_name ?? "",
    inn: req.inn ?? intake.inn ?? "",
    kpp: req.kpp ?? "",
    bankAcnt: req.bankAcnt ?? "",
    bankBik: req.bankBik ?? "",
    recipientCorrAccountNumber: req.recipientCorrAccountNumber ?? "",
  });
  const [applyReq, setApplyReq] = useState(true);

  useEffect(() => {
    let url: string | null = null;
    let cancelled = false;
    if (intake.has_pdf) {
      fetchIntakePdfUrl(intake.id)
        .then((u) => {
          if (cancelled) {
            URL.revokeObjectURL(u);
            return;
          }
          url = u;
          setPdfUrl(u);
        })
        .catch(() => undefined);
    }
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [intake.id, intake.has_pdf]);

  const setReq = (key: keyof typeof r, value: string) => setR((prev) => ({ ...prev, [key]: value }));

  const canConfirm =
    amount.trim() !== "" && (mode === "existing" ? counterpartyId !== "" : newName.trim() !== "");

  const buildPayload = (apply: boolean) => ({
    counterparty_id: mode === "existing" ? counterpartyId : null,
    new_counterparty_name: mode === "new" ? newName.trim() : null,
    new_counterparty_inn: mode === "new" ? newInn.trim() || null : null,
    amount: amount.trim(),
    invoice_number: invoiceNumber.trim() || null,
    invoice_date: invoiceDate || null,
    requisites: r,
    apply_requisites: apply,
  });

  const invalidate = () =>
    void queryClient.invalidateQueries({ queryKey: ["payment-page", "intakes"] });

  const mutation = useMutation({
    mutationFn: () => confirmIntake(intake.id, buildPayload(applyReq)),
    onSuccess: (item) => {
      invalidate();
      toast.success(
        item.status === "duplicate"
          ? "Это дубль существующей накладной"
          : "Счёт подтверждён — готов к оплате",
      );
      onClose();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось подтвердить")),
  });

  const busy = mutation.isPending;

  return (
    <Dialog open onOpenChange={(open) => (!open ? onClose() : undefined)}>
      <DialogContent className="max-w-5xl">
        <DialogHeader>
          <DialogTitle>Разбор счёта на оплату</DialogTitle>
          <DialogDescription>
            Сверьте контрагента и сумму с PDF, при необходимости поправьте. После подтверждения
            счёт станет готов к оплате в банк.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4 md:grid-cols-2">
          <div className="min-h-[60vh] rounded-md border bg-muted/30">
            {pdfUrl ? (
              <iframe title="PDF счёта" src={pdfUrl} className="h-[60vh] w-full rounded-md" />
            ) : (
              <div className="flex h-[60vh] items-center justify-center text-sm text-muted-foreground">
                {intake.has_pdf ? "Загрузка PDF…" : "PDF недоступен"}
              </div>
            )}
          </div>

          <div className="grid max-h-[60vh] gap-3 overflow-auto pr-1">
            <div className="grid gap-1">
              <Label className="text-xs text-muted-foreground">Контрагент</Label>
              <div className="flex gap-2">
                <Button
                  type="button"
                  size="sm"
                  variant={mode === "existing" ? "default" : "outline"}
                  onClick={() => setMode("existing")}
                >
                  Существующий
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant={mode === "new" ? "default" : "outline"}
                  onClick={() => setMode("new")}
                >
                  Создать нового
                </Button>
              </div>
              {mode === "existing" ? (
                <Combobox
                  options={counterpartyOptions}
                  value={counterpartyId}
                  onChange={setCounterpartyId}
                  placeholder="Выберите контрагента…"
                  className="mt-1"
                />
              ) : (
                <div className="mt-1 grid gap-2 sm:grid-cols-2">
                  <Field label="Название" value={newName} onChange={setNewName} />
                  <Field label="ИНН" value={newInn} onChange={setNewInn} />
                </div>
              )}
            </div>

            <div className="grid gap-2 sm:grid-cols-3">
              <Field label="Сумма к оплате" value={amount} onChange={setAmount} />
              <Field label="№ счёта" value={invoiceNumber} onChange={setInvoiceNumber} />
              <Field label="Дата" type="date" value={invoiceDate} onChange={setInvoiceDate} />
            </div>

            <div className="grid gap-2 rounded-md border p-3">
              <div className="text-xs font-medium uppercase text-muted-foreground">Реквизиты</div>
              <Field
                label="Получатель"
                value={r.recipientName}
                onChange={(v) => setReq("recipientName", v)}
              />
              <div className="grid gap-2 sm:grid-cols-2">
                <Field label="ИНН" value={r.inn} onChange={(v) => setReq("inn", v)} />
                <Field label="КПП" value={r.kpp} onChange={(v) => setReq("kpp", v)} />
              </div>
              <Field label="Расчётный счёт" value={r.bankAcnt} onChange={(v) => setReq("bankAcnt", v)} />
              <div className="grid gap-2 sm:grid-cols-2">
                <Field label="БИК" value={r.bankBik} onChange={(v) => setReq("bankBik", v)} />
                <Field
                  label="Корр. счёт"
                  value={r.recipientCorrAccountNumber}
                  onChange={(v) => setReq("recipientCorrAccountNumber", v)}
                />
              </div>
              <label className="mt-1 flex items-center gap-2 text-sm">
                <Checkbox
                  checked={applyReq}
                  onChange={(e) => setApplyReq(e.target.checked)}
                />
                Перенести реквизиты в карточку контрагента и пометить проверенными
              </label>
            </div>
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={busy}>
            Отмена
          </Button>
          <Button onClick={() => mutation.mutate()} disabled={!canConfirm || busy}>
            {intake.status === "linked" ? "Сохранить реквизиты" : "Подтвердить — готов к оплате"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
