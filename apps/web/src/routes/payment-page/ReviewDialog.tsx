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
  fetchIntakeFileUrl,
  type CounterpartyOption,
  type PaymentIntake,
} from "./api";
import { DocumentPreview } from "./DocumentPreview";
import { RequisitesFields } from "./RequisitesFields";
import { useRequisitesForm } from "./requisites";
import { VatFields, type VatValue } from "./VatFields";

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
  const [periodStart, setPeriodStart] = useState(intake.service_period_start ?? "");
  const [periodEnd, setPeriodEnd] = useState(intake.service_period_end ?? "");
  const [vat, setVat] = useState<VatValue>({
    amount: intake.vat_amount ?? "",
    rate: intake.vat_rate ?? "",
  });
  // Реквизиты формы: распознанное из PDF, а чего в счёте нет — из карточки выбранного
  // контрагента (при создании нового карточки ещё нет, поэтому только счёт).
  const {
    values: r,
    sources,
    setValue: setReq,
    applyCandidate,
    cardLoading,
    differsFromCard,
    mismatch,
  } = useRequisitesForm(intake, mode === "existing" ? counterpartyId || null : null);
  const [applyReq, setApplyReq] = useState(true);

  useEffect(() => {
    let url: string | null = null;
    let cancelled = false;
    if (intake.has_pdf) {
      fetchIntakeFileUrl(intake.id)
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

  const canConfirm =
    amount.trim() !== "" &&
    (mode === "existing" ? counterpartyId !== "" : newName.trim() !== "") &&
    ((!intake.service_period_required && intake.service_period_status !== "ambiguous") ||
      Boolean(periodStart && periodEnd));

  const buildPayload = (apply: boolean) => ({
    counterparty_id: mode === "existing" ? counterpartyId : null,
    new_counterparty_name: mode === "new" ? newName.trim() : null,
    new_counterparty_inn: mode === "new" ? newInn.trim() || null : null,
    amount: amount.trim(),
    invoice_number: invoiceNumber.trim() || null,
    invoice_date: invoiceDate || null,
    service_period_start: periodStart || null,
    service_period_end: periodEnd || null,
    // Пустая сумма — не «поле не трогали», а утверждение «налога в счёте нет»: подтверждение
    // разбора и есть тот момент, когда человек за это отвечает.
    vat_amount: vat.amount.trim(),
    vat_rate: vat.rate.trim() || null,
    requisites: r,
    // Переносить нечего, когда в форме ровно то, что уже лежит в карточке: иначе каждое
    // подтверждение молча ставило бы отметку «реквизиты проверены» за человека.
    apply_requisites: apply && differsFromCard,
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
          <DocumentPreview url={pdfUrl} mime={intake.attachment_mime} hasFile={intake.has_pdf} />

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

            <VatFields
              mode={intake.vat_mode}
              value={vat}
              onChange={setVat}
              invoiceAmount={amount}
            />

            <div className="grid gap-2 rounded-md border p-3">
              <div className="flex items-center justify-between gap-2">
                <div className="text-xs font-medium uppercase text-muted-foreground">
                  Период оказания услуги
                </div>
                {intake.service_period_status === "ambiguous" ? (
                  <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[11px] text-amber-800">
                    найдено несколько периодов
                  </span>
                ) : intake.service_period_source?.startsWith("document") ||
                  intake.service_period_source?.startsWith("subject") ? (
                  <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-[11px] text-emerald-700">
                    определён автоматически
                  </span>
                ) : null}
              </div>
              <div className="grid gap-2 sm:grid-cols-2">
                <Field label="С" type="date" value={periodStart} onChange={setPeriodStart} />
                <Field label="По" type="date" value={periodEnd} onChange={setPeriodEnd} />
              </div>
              <p className="text-xs text-muted-foreground">
                Если в счёте указаны разные периоды, выберите правильный вручную. После окончания
                периода расход будет признан автоматически.
              </p>
              {(intake.service_period_required || intake.service_period_status === "ambiguous") &&
              (!periodStart || !periodEnd) ? (
                <p className="text-xs text-amber-600">
                  {intake.service_period_status === "ambiguous"
                    ? "Выберите один правильный период вручную."
                    : "Для этого контрагента период обязателен."}
                </p>
              ) : null}
            </div>

            <div className="grid gap-2">
              <RequisitesFields
                values={r}
                sources={sources}
                onChange={setReq}
                mismatch={mismatch}
                onPickFromHistory={applyCandidate}
                searchQuery={(mode === "new" ? newInn || newName : r.inn || r.recipientName) ?? ""}
                counterpartyId={mode === "existing" ? counterpartyId : undefined}
                loading={cardLoading}
              />
              {differsFromCard ? (
                <label className="flex items-center gap-2 text-sm">
                  <Checkbox
                    checked={applyReq}
                    onChange={(e) => setApplyReq(e.target.checked)}
                  />
                  Перенести реквизиты в карточку контрагента и пометить проверенными
                </label>
              ) : (
                <p className="text-xs text-muted-foreground">
                  В карточке контрагента уже эти реквизиты — переносить нечего.
                </p>
              )}
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
