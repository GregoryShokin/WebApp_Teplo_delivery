import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Send, Sparkles } from "lucide-react";
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
import { apiErrorMessage, getDdsArticles } from "@/lib/api";
import { ArticleCombobox } from "@/components/ui-app/ArticleCombobox";
import { formatDate, formatRub } from "@/routes/counterparties/shared";

import {
  confirmIntake,
  fetchIntakeFileUrl,
  getCounterpartyRequisites,
  recognizeAgain,
  sendToBank,
  type CounterpartyOption,
  type PaymentIntake,
} from "./api";
import { DocumentPreview } from "./DocumentPreview";
import { RequisitesFields } from "./RequisitesFields";
import { useRequisitesForm } from "./requisites";
import { VatFields, vatSuffixPreview, type VatValue } from "./VatFields";
import { calendarServicePeriod } from "./service-period";

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
  const id = useId();
  return (
    <div className="grid gap-1">
      <Label htmlFor={id} className="text-xs text-muted-foreground">
        {label}
      </Label>
      <Input
        id={id}
        type={type}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        onInput={type === "month" ? (e) => onChange(e.currentTarget.value) : undefined}
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
  const [recognized, setRecognized] = useState(intake);
  const req = useMemo(() => intake.requisites ?? {}, [intake.requisites]);

  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const [mode, setMode] = useState<"existing" | "new">(intake.counterparty_id ? "existing" : "new");
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
  } = useRequisitesForm(recognized, mode === "existing" ? counterpartyId || null : null);
  const [applyReq, setApplyReq] = useState(true);
  const [duration, setDuration] = useState(String(intake.service_duration_months ?? ""));
  const [startMonth, setStartMonth] = useState("");
  const [ddsArticleId, setDdsArticleId] = useState(
    intake.invoice_dds_article_id ?? intake.default_dds_article_id ?? "",
  );
  const [billingMode, setBillingMode] = useState<
    "fixed_tariff" | "per_invoice" | "one_off" | "goods"
  >(intake.service_duration_months ? "fixed_tariff" : "per_invoice");
  const [documentKind, setDocumentKind] = useState<"invoice" | "act" | "upd">(
    intake.document_kind === "act" || intake.document_kind === "upd"
      ? intake.document_kind
      : "invoice",
  );
  const [sendError, setSendError] = useState<string | null>(null);
  const [noVatChecked, setNoVatChecked] = useState(intake.vat_mode === "none");
  const touched = useRef(new Set<string>());
  const touch = (key: string, setter: (v: string) => void) => (v: string) => {
    touched.current.add(key);
    setter(v);
  };
  const articles = useQuery({ queryKey: ["dds", "articles"], queryFn: getDdsArticles });
  const card = useQuery({
    queryKey: ["payment-page", "counterparty-requisites", counterpartyId],
    queryFn: () => getCounterpartyRequisites(counterpartyId),
    enabled: mode === "existing" && Boolean(counterpartyId),
  });
  const matching = counterpartyOptions.find((cp) => cp.inn && cp.inn === newInn.trim());
  const needsSetup =
    mode === "new"
      ? !matching || matching.status === "requires_setup"
      : card.data?.status === "requires_setup";
  const periodRequired =
    (needsSetup ? billingMode === "fixed_tariff" : recognized.service_period_required) ||
    Boolean(duration || recognized.service_duration_months) ||
    recognized.service_period_status === "ambiguous";
  useEffect(() => {
    if (card.data?.default_dds_article_id && !touched.current.has("article"))
      setDdsArticleId(card.data.default_dds_article_id);
  }, [card.data]);

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

  const numericAmount = Number(amount.replace(/\s/g, "").replace(",", "."));
  const numericVat = Number(vat.amount.replace(/\s/g, "").replace(",", "."));
  const vatReady = vat.amount.trim()
    ? Number.isFinite(numericVat) && numericVat > 0 && numericVat < numericAmount
    : noVatChecked;
  const requisitesReady = [
    r.recipientName,
    r.inn,
    r.bankAcnt,
    r.bankBik,
    r.recipientCorrAccountNumber,
  ].every((v) => v.trim());
  const canConfirm =
    (!duration ||
      (Number.isInteger(Number(duration)) && Number(duration) >= 1 && Number(duration) <= 120)) &&
    Number.isFinite(numericAmount) &&
    numericAmount > 0 &&
    vatReady &&
    (mode === "existing" ? counterpartyId !== "" : newName.trim() !== "") &&
    (!(periodRequired || periodStart || periodEnd) ||
      Boolean(periodStart && periodEnd && periodEnd >= periodStart)) &&
    (!needsSetup || Boolean(ddsArticleId && requisitesReady && applyReq));

  const buildPayload = (apply: boolean) => ({
    counterparty_id: mode === "existing" ? counterpartyId : (matching?.value ?? null),
    new_counterparty_name: mode === "new" && !matching ? newName.trim() : null,
    new_counterparty_inn: mode === "new" && !matching ? newInn.trim() || null : null,
    amount: numericAmount.toFixed(2),
    invoice_number: invoiceNumber.trim(),
    invoice_date: invoiceDate,
    service_period_start: periodStart,
    service_period_end: periodEnd,
    document_kind: documentKind,
    service_duration_months: duration ? Number(duration) : null,
    setup_counterparty: needsSetup,
    dds_article_id: ddsArticleId || null,
    service_billing_mode: needsSetup ? billingMode : null,
    // Пустая сумма — не «поле не трогали», а утверждение «налога в счёте нет»: подтверждение
    // разбора и есть тот момент, когда человек за это отвечает.
    vat_amount: vat.amount.trim(),
    vat_rate: vat.rate.trim() || null,
    requisites: r,
    // Переносить нечего, когда в форме ровно то, что уже лежит в карточке: иначе каждое
    // подтверждение молча ставило бы отметку «реквизиты проверены» за человека.
    apply_requisites: apply && (differsFromCard || !recognized.requisites_verified),
  });

  const invalidate = () =>
    void queryClient.invalidateQueries({ queryKey: ["payment-page", "intakes"] });

  const reparse = useMutation({
    mutationFn: () => recognizeAgain(intake.id),
    onSuccess: (item) => {
      setRecognized(item);
      if (!touched.current.has("counterparty") && item.counterparty_id) {
        setMode("existing");
        setCounterpartyId(item.counterparty_id);
      }
      const fields: Array<[string, string, (v: string) => void]> = [
        ["name", item.recipient_name ?? "", setNewName],
        ["inn", item.inn ?? "", setNewInn],
        ["amount", item.amount ?? "", setAmount],
        ["number", item.invoice_number ?? "", setInvoiceNumber],
        ["date", item.invoice_date ?? "", setInvoiceDate],
        ["duration", String(item.service_duration_months ?? ""), setDuration],
        ["start", item.service_period_start ?? "", setPeriodStart],
        ["end", item.service_period_end ?? "", setPeriodEnd],
      ];
      fields.forEach(([key, value, setter]) => {
        if (!touched.current.has(key)) setter(value);
      });
      if (!touched.current.has("vat")) {
        setVat({ amount: item.vat_amount ?? "", rate: item.vat_rate ?? "" });
        setNoVatChecked(item.vat_mode === "none");
      }
      invalidate();
      toast.success("Разбор обновлён — проверьте предложения");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось повторить разбор")),
  });
  const mutation = useMutation({
    mutationFn: async (send: boolean) => {
      setSendError(null);
      const item = await confirmIntake(intake.id, buildPayload(applyReq));
      setRecognized(item);
      invalidate();
      if (send && item.status === "linked" && !item.invoice_in_draft) {
        try {
          await sendToBank(item.id, { dds_article_id: ddsArticleId || null });
        } catch (error) {
          setSendError(apiErrorMessage(error, "Банк недоступен"));
          throw error;
        }
      }
      return { item, send };
    },
    onSuccess: ({ item, send }) => {
      invalidate();
      toast.success(
        item.status === "duplicate"
          ? "Это дубль существующей накладной"
          : send && item.status === "linked"
            ? "Отправлено в банк — ожидает подтверждения"
            : documentKind === "invoice"
              ? "Счёт подтверждён — готов к оплате"
              : "Закрывающий документ сохранён",
      );
      onClose();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось подтвердить")),
  });

  const busy = mutation.isPending || reparse.isPending;
  const locked = recognized.invoice_in_draft || recognized.invoice_payment_status === "paid";

  return (
    <Dialog open onOpenChange={(open) => (!open && !busy ? onClose() : undefined)}>
      <DialogContent className="max-w-6xl max-h-[94vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Разбор счёта на оплату</DialogTitle>
          <DialogDescription>
            Проверьте документ, подтвердите получателя и отправьте счёт в банк из этого окна.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4 md:grid-cols-2">
          <DocumentPreview url={pdfUrl} mime={intake.attachment_mime} hasFile={intake.has_pdf} />

          <div className="grid content-start max-h-[67vh] gap-4 overflow-auto pr-2">
            <div className="rounded-lg border border-sky-200 bg-sky-50/60 p-3">
              <div className="flex items-center justify-between gap-2">
                <span className="flex items-center gap-2 text-sm font-medium text-sky-900">
                  <Sparkles className="h-4 w-4" />
                  Предложения для проверки
                </span>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={busy || locked}
                  onClick={() => reparse.mutate()}
                >
                  <RefreshCw
                    className={`mr-1 h-3.5 w-3.5 ${reparse.isPending ? "animate-spin" : ""}`}
                  />
                  {reparse.isPending ? "Разбираем…" : "Повторить разбор"}
                </Button>
              </div>
              {recognized.service_duration_months ? (
                <p className="mt-2 text-sm">
                  В счёте <strong>{recognized.service_duration_months} мес.</strong>
                  {!recognized.service_period_start
                    ? " Выберите месяц начала — окончание рассчитаем."
                    : ""}
                </p>
              ) : null}
              {recognized.field_evidence?.service_duration_months ? (
                <p className="mt-1 text-xs text-sky-800">
                  В документе: «{recognized.field_evidence.service_duration_months}»
                </p>
              ) : null}
            </div>
            {recognized.recognition_notes?.map((note) => (
              <p key={note} className="rounded-md bg-amber-50 p-2 text-xs text-amber-800">
                {note}
              </p>
            ))}
            {recognized.line_items?.length ? (
              <details className="rounded-lg border p-3">
                <summary className="cursor-pointer text-xs font-medium text-muted-foreground">
                  Строки счёта · {recognized.line_items.length}
                </summary>
                <div className="mt-3 grid gap-2">
                  {recognized.line_items.map((item, i) => (
                    <div key={i} className="flex justify-between gap-3 text-xs">
                      <div>
                        {item.description}
                        <div className="text-muted-foreground">
                          {item.quantity} {item.unit} × {item.unit_price} ₽
                        </div>
                      </div>
                      <span className="shrink-0 tabular-nums">{item.amount} ₽</span>
                    </div>
                  ))}
                </div>
              </details>
            ) : null}
            <div className="grid gap-1">
              <Label className="text-xs text-muted-foreground">Контрагент</Label>
              <div className="flex gap-2">
                <Button
                  type="button"
                  size="sm"
                  variant={mode === "existing" ? "default" : "outline"}
                  aria-pressed={mode === "existing"}
                  onClick={() => {
                    touched.current.add("counterparty");
                    setMode("existing");
                  }}
                >
                  Существующий
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant={mode === "new" ? "default" : "outline"}
                  aria-pressed={mode === "new"}
                  onClick={() => {
                    touched.current.add("counterparty");
                    setMode("new");
                  }}
                >
                  Новый контрагент
                </Button>
              </div>
              {mode === "existing" ? (
                <Combobox
                  options={counterpartyOptions}
                  value={counterpartyId}
                  onChange={touch("counterparty", setCounterpartyId)}
                  placeholder="Выберите контрагента…"
                  className="mt-1"
                />
              ) : (
                <div className="mt-1 grid gap-2 sm:grid-cols-2">
                  <Field label="Название" value={newName} onChange={touch("name", setNewName)} />
                  <Field
                    label="ИНН организации"
                    value={newInn}
                    onChange={(v) => {
                      touch("inn", setNewInn)(v);
                      setReq("inn", v);
                    }}
                  />
                </div>
              )}
              {mode === "new" && matching ? (
                <p className="text-xs text-emerald-700">
                  По этому ИНН уже есть {matching.label}. Используем существующую карточку.
                </p>
              ) : needsSetup ? (
                <p className="text-xs text-muted-foreground">
                  Карточка будет создана и настроена при подтверждении.
                </p>
              ) : null}
            </div>

            <div className="grid gap-2 sm:grid-cols-3">
              <Field label="Сумма к оплате" value={amount} onChange={touch("amount", setAmount)} />
              <Field
                label="№ счёта"
                value={invoiceNumber}
                onChange={touch("number", setInvoiceNumber)}
              />
              <Field
                label="Дата счёта"
                type="date"
                value={invoiceDate}
                onChange={touch("date", setInvoiceDate)}
              />
            </div>
            <label className="grid gap-1 text-xs text-muted-foreground">
              Вид документа
              <select
                className="h-9 rounded-md border bg-background px-2 text-sm text-foreground"
                value={documentKind}
                onChange={(e) => setDocumentKind(e.target.value as typeof documentKind)}
              >
                <option value="invoice">Счёт на оплату</option>
                <option value="act">Акт оказанных услуг</option>
                <option value="upd">УПД</option>
              </select>
            </label>

            <VatFields
              mode={recognized.vat_mode}
              value={vat}
              onChange={(v) => {
                touched.current.add("vat");
                setVat(v);
              }}
              invoiceAmount={amount}
            />
            {recognized.vat_mode !== "none" && !vat.amount.trim() ? (
              <label className="flex items-center gap-2 text-xs">
                <Checkbox
                  checked={noVatChecked}
                  onChange={(e) => {
                    touched.current.add("vat");
                    setNoVatChecked(e.target.checked);
                  }}
                />
                Проверил документ: платёж без НДС
              </label>
            ) : null}

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
              {duration || recognized.service_duration_months ? (
                <div className="grid gap-2 sm:grid-cols-2">
                  <Field
                    label="Длительность, мес."
                    type="number"
                    value={duration}
                    onChange={(v) => {
                      touch("duration", setDuration)(v);
                      const p = calendarServicePeriod(startMonth, Number(v));
                      if (p) {
                        setPeriodStart(p.start);
                        setPeriodEnd(p.end);
                      }
                    }}
                  />
                  <Field
                    label="Месяц начала"
                    type="month"
                    value={startMonth}
                    onChange={(v) => {
                      setStartMonth(v);
                      const p = calendarServicePeriod(v, Number(duration));
                      touched.current.add("start");
                      touched.current.add("end");
                      setPeriodStart(p?.start ?? "");
                      setPeriodEnd(p?.end ?? "");
                    }}
                  />
                </div>
              ) : null}
              <div className="grid gap-2 sm:grid-cols-2">
                <Field
                  label="С"
                  type="date"
                  value={periodStart}
                  onChange={touch("start", setPeriodStart)}
                />
                <Field
                  label="По"
                  type="date"
                  value={periodEnd}
                  onChange={touch("end", setPeriodEnd)}
                />
              </div>
              {recognized.service_period_status === "ambiguous" &&
              recognized.service_period_candidates?.length ? (
                <div className="grid gap-1">
                  <p className="text-xs text-muted-foreground">
                    Выберите подходящий период из документа:
                  </p>
                  {recognized.service_period_candidates.map((period) => (
                    <Button
                      key={`${period.start}:${period.end}`}
                      variant="outline"
                      size="sm"
                      onClick={() => {
                        touch("start", setPeriodStart)(period.start);
                        touch("end", setPeriodEnd)(period.end);
                      }}
                    >
                      {formatDate(period.start)} — {formatDate(period.end)}
                    </Button>
                  ))}
                </div>
              ) : null}
              <p className="text-xs text-muted-foreground">
                {periodStart && periodEnd
                  ? `${formatDate(periodStart)} — ${formatDate(periodEnd)}`
                  : "Укажите начало услуги. Дата счёта не определяет начало периода."}
              </p>
              {periodRequired && (!periodStart || !periodEnd) ? (
                <p className="text-xs text-amber-600">
                  {intake.service_period_status === "ambiguous"
                    ? "Выберите один правильный период вручную."
                    : "Для счёта за период укажите начало и окончание услуги."}
                </p>
              ) : null}
            </div>
            <div className="grid gap-2 rounded-lg border p-3">
              <div className="text-sm font-semibold">Учёт и статья платежа</div>
              {needsSetup ? (
                <>
                  <label className="grid gap-1 text-xs text-muted-foreground">
                    Что оплачиваем
                    <select
                      className="h-9 rounded-md border bg-background px-2 text-sm text-foreground"
                      value={billingMode}
                      onChange={(e) => setBillingMode(e.target.value as typeof billingMode)}
                    >
                      <option value="fixed_tariff">Подписка / услуга за период</option>
                      <option value="per_invoice">Услуги — ждём акт или УПД</option>
                      <option value="one_off">Разовая услуга</option>
                      <option value="goods">Товары</option>
                    </select>
                  </label>
                  <p className="text-xs text-muted-foreground">
                    Способ учёта сохранится в карточке контрагента.
                  </p>
                </>
              ) : null}
              <ArticleCombobox
                articles={(articles.data ?? []).filter(
                  (a) => a.is_active && a.movement_type === "outflow",
                )}
                value={ddsArticleId}
                onChange={touch("article", setDdsArticleId)}
                placeholder="Выберите статью ДДС"
              />
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
              {differsFromCard || !recognized.requisites_verified ? (
                <label className="flex items-center gap-2 text-sm">
                  <Checkbox checked={applyReq} onChange={(e) => setApplyReq(e.target.checked)} />
                  Подтверждаю реквизиты и сохраняю их в карточке
                </label>
              ) : (
                <p className="text-xs text-muted-foreground">
                  В карточке контрагента уже эти реквизиты — переносить нечего.
                </p>
              )}
            </div>
            <div className="rounded-lg border bg-muted/30 p-3">
              <div className="flex justify-between gap-2 text-sm font-semibold">
                <span>К отправке в банк</span>
                <span>
                  {Number.isFinite(numericAmount) ? formatRub(numericAmount.toFixed(2)) : "—"}
                </span>
              </div>
              <p className="mt-2 text-xs text-muted-foreground">
                Получатель: {r.recipientName || newName || "не выбран"}
              </p>
              <p className="mt-2 break-words text-xs">
                Оплата поставщику {mode === "new" ? newName : r.recipientName}
                {invoiceNumber ? ` по счетам ${invoiceNumber}` : ""}. {vatSuffixPreview(vat)}
              </p>
              <p className="mt-2 text-xs text-muted-foreground">
                В банк уйдёт черновик для подтверждения. Оплата будет отмечена после ответа банка.
              </p>
            </div>
            {sendError ? (
              <div
                role="alert"
                className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm"
              >
                <strong>Счёт сохранён. Отправка не завершена.</strong>
                <p className="mt-1">{sendError}</p>
                <p className="mt-1 text-xs">Можно повторить отправку из этого окна.</p>
              </div>
            ) : null}
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={busy}>
            Отмена
          </Button>
          <Button
            variant="outline"
            onClick={() => mutation.mutate(false)}
            disabled={!canConfirm || busy || locked}
          >
            {documentKind === "invoice" ? "Сохранить — готов к оплате" : "Сохранить документ"}
          </Button>
          <Button
            onClick={() => mutation.mutate(true)}
            disabled={
              !canConfirm ||
              !requisitesReady ||
              !applyReq ||
              busy ||
              locked ||
              documentKind !== "invoice"
            }
          >
            <Send className="mr-2 h-4 w-4" />
            {mutation.isPending
              ? "Сохраняем и отправляем…"
              : sendError
                ? "Повторить отправку в банк"
                : "Подтвердить и отправить в банк"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
