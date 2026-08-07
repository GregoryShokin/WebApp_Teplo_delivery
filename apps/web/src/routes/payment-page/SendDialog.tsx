import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
import { ArticleCombobox } from "@/components/ui-app/ArticleCombobox";
import { apiErrorMessage, getDdsArticles } from "@/lib/api";
import { todayIso } from "@/lib/date";
import { formatDate, formatRub } from "@/routes/counterparties/shared";

import {
  cancelSchedule,
  confirmIntake,
  fetchIntakeFileUrl,
  scheduleSend,
  sendToBank,
  type PaymentIntake,
} from "./api";
import { DocumentPreview } from "./DocumentPreview";
import { RequisitesFields } from "./RequisitesFields";
import { useRequisitesForm } from "./requisites";
import { VatFields, type VatValue } from "./VatFields";

// Окно отправки счёта в банк: сверка реквизитов получателя + выбор даты (дефолт — сегодня).
// Сегодня → уходит сразу; будущая дата → запланированная авто-отправка. Реквизиты подтверждаются
// здесь же (apply_requisites), чтобы не было отдельного скрытого барьера.
export function SendDialog({
  intake,
  onClose,
  onEditRecognition,
}: {
  intake: PaymentIntake;
  onClose: () => void;
  /** «Поправить разбор» — окно отправки правит реквизиты, период и статью, но не сумму,
   *  номер и контрагента. Если сверка с документом показала, что не то, — путь назад. */
  onEditRecognition?: (intake: PaymentIntake) => void;
}) {
  const queryClient = useQueryClient();
  const today = todayIso();

  const [date, setDate] = useState(intake.scheduled_send_date ?? today);
  const [periodStart, setPeriodStart] = useState(intake.service_period_start ?? "");
  const [periodEnd, setPeriodEnd] = useState(intake.service_period_end ?? "");
  // НДС правится и здесь: это последняя точка перед деньгами, и текст назначения платежа
  // собирается ровно из этой цифры. Уходит тем же confirm, что и реквизиты с периодом.
  const [vat, setVat] = useState<VatValue>({
    amount: intake.vat_amount ?? "",
    rate: intake.vat_rate ?? "",
  });
  const vatParsed = Number(vat.amount.replace(",", ".").replace(/\s/g, ""));
  const invoiceTotal = Number((intake.amount ?? "").replace(",", ".").replace(/\s/g, ""));
  const vatReady =
    !vat.amount.trim() ||
    (Number.isFinite(vatParsed) &&
      vatParsed > 0 &&
      (!Number.isFinite(invoiceTotal) || invoiceTotal <= 0 || vatParsed < invoiceTotal));
  // Статья ДДС оплаты: уже выбранная на счёте → закреплённая за контрагентом → пусто (на бэке
  // дефолт «Оплата поставщикам»). Чекбокс закрепляет выбранную статью за контрагентом на будущее.
  const [ddsArticleId, setDdsArticleId] = useState(
    intake.invoice_dds_article_id ?? intake.default_dds_article_id ?? "",
  );
  const [rememberForCp, setRememberForCp] = useState(false);
  const articlesQuery = useQuery({ queryKey: ["dds", "articles"], queryFn: getDdsArticles });
  // Реквизиты: распознанное из счёта, а чего в нём нет — из карточки контрагента. Раньше
  // окно показывало только распознанное, и по нераспознанному счёту кнопка «Отправить»
  // оставалась заблокированной, пока человек не перебьёт с бумажки то, что в системе есть.
  const {
    values: r,
    sources,
    setValue: setField,
    applyCandidate,
    cardRequisites,
    cardLoading,
    mismatch,
  } = useRequisitesForm(intake, intake.counterparty_id);

  // «У получателя нет реквизитов» — арендодатель с возмещением коммуналки, физлицо со счётом
  // на бумаге: платить надо, а платёжку собрать не из чего. Тогда платёж выписывается на карту
  // ИП (как неофициальному поставщику), деньги приходят на Сейф, и счёт закрывается, когда
  // наличные выданы получателю. Выбор предлагаем ТОЛЬКО когда карточка контрагента пуста:
  // там, где счёт получателя известен, платим по нему — ровно это правило и на бэке.
  const [payViaIpCard, setPayViaIpCard] = useState(intake.scheduled_pays_via_safe);
  const cardEmpty =
    !cardRequisites ||
    Object.values(cardRequisites).every((value) => !String(value ?? "").trim());
  const canPayViaIpCard = !cardLoading && cardEmpty;
  const viaIpCard = canPayViaIpCard && payViaIpCard;

  const invalidate = () =>
    void queryClient.invalidateQueries({ queryKey: ["payment-page", "intakes"] });

  const ready =
    viaIpCard ||
    (r.recipientName.trim() !== "" &&
      r.bankAcnt.trim() !== "" &&
      r.bankBik.trim() !== "" &&
      r.inn.trim() !== "" &&
      r.recipientCorrAccountNumber.trim() !== "");
  // Наполовину заполненный период — не «пусто», а незаконченный ввод: одна дата уходит в
  // confirm, где период без второй границы просто отбрасывается. Платёж уходил, период
  // терялся молча, и этот же счёт возвращался в очередь «период не указан».
  const periodHalfFilled = Boolean(periodStart) !== Boolean(periodEnd);
  const periodReady =
    !periodHalfFilled && (!intake.service_period_required || Boolean(periodStart && periodEnd));
  // Спрашиваем у всех, кроме режима «счёт + УПД»: там расход и его период приносит документ,
  // а плательщик их не знает. Уже заполненный период показываем в любом случае.
  const askPeriod =
    intake.service_billing_mode !== "per_invoice" || Boolean(periodStart || periodEnd);
  const isNow = date <= today;

  const send = useMutation({
    mutationFn: async () => {
      // Обычный счёт подтверждаем здесь: так реквизиты попадают в карточку и получают статус
      // verified. Коммунальная строка к этому моменту УЖЕ подтверждена специальным окном
      // (поток → арендодатель → помещение → период). Повторный вызов обычного /confirm сервер
      // намеренно отвергает: он провёл бы квитанцию как счёт ресурсника. Поэтому ей остаётся
      // только создать банковский черновик.
      if (!intake.utility_kind) {
        // При выводе на карту ИП реквизиты не трогаем вовсе: занеси мы сейчас в карточку то,
        // что распозналось в бумаге (у коммуналки там ресурсник, а платим арендодателю),
        // следующий платёж ушёл бы по ним молча и мимо получателя.
        await confirmIntake(intake.id, {
          requisites: viaIpCard ? undefined : r,
          apply_requisites: !viaIpCard,
          service_period_start: periodStart || null,
          service_period_end: periodEnd || null,
          vat_amount: vat.amount.trim(),
          vat_rate: vat.rate.trim() || null,
        });
      }
      const choice = {
        dds_article_id: ddsArticleId || null,
        remember_for_counterparty: rememberForCp,
        pays_via_safe: viaIpCard,
      };
      if (isNow) await sendToBank(intake.id, choice);
      else await scheduleSend(intake.id, date, choice);
    },
    onSuccess: () => {
      invalidate();
      toast.success(
        isNow
          ? viaIpCard
            ? "Отправлено на карту ИП — деньги придут на Сейф"
            : "Отправлено в банк — ожидает подтверждения"
          : `Запланировано на ${formatDate(date)} — уйдёт в банк автоматически`,
      );
      onClose();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось отправить в банк")),
  });

  const cancel = useMutation({
    mutationFn: () => cancelSchedule(intake.id),
    onSuccess: () => {
      invalidate();
      toast.success("План отправки отменён");
      onClose();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось отменить")),
  });

  const busy = send.isPending || cancel.isPending;

  // Документ рядом с формой: у готового счёта это единственная возможность сверить платёж
  // с бумагой — отдельной кнопки «Разобрать» на строке больше нет. Ссылка живёт ровно
  // столько, сколько открыто окно.
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!intake.has_pdf) return;
    let revoked: string | null = null;
    let cancelled = false;
    fetchIntakeFileUrl(intake.id)
      .then((url) => {
        if (cancelled) {
          URL.revokeObjectURL(url);
          return;
        }
        revoked = url;
        setPdfUrl(url);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
      if (revoked) URL.revokeObjectURL(revoked);
    };
  }, [intake.id, intake.has_pdf]);

  return (
    <Dialog open onOpenChange={(open) => (!open ? onClose() : undefined)}>
      {/* Шире прежнего: документ и форма стоят рядом, как в окне разбора — иначе сверять
          реквизиты приходилось бы по памяти. */}
      <DialogContent className="max-h-[92vh] max-w-5xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Отправка счёта в банк</DialogTitle>
          <DialogDescription>
            Сверьте реквизиты получателя и выберите дату. Уйдёт банковский черновик — деньги не
            списываются, окончательное подтверждение в банке.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,420px)]">
          <DocumentPreview
            url={pdfUrl}
            mime={intake.attachment_mime}
            hasFile={intake.has_pdf}
          />

          <div className="grid content-start gap-3">
          <div className="rounded-md border bg-muted/30 px-3 py-2">
            <div className="flex items-baseline justify-between gap-2">
              <div className="font-medium">
                {r.recipientName || intake.counterparty_name || "—"}
                {intake.invoice_number ? (
                  <span className="ml-2 text-sm text-muted-foreground">
                    № {intake.invoice_number}
                  </span>
                ) : null}
              </div>
              <div className="text-lg font-semibold tabular-nums">
                {intake.amount ? formatRub(intake.amount) : "—"}
              </div>
            </div>
            {/* Сумму, номер и контрагента это окно не правит — если сверка с документом
                показала, что распознано не то, отсюда есть ход в разбор. */}
            {onEditRecognition ? (
              <button
                type="button"
                className="mt-1 text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground"
                onClick={() => onEditRecognition(intake)}
              >
                Сумма или контрагент не те — поправить разбор
              </button>
            ) : null}
          </div>

          {/* Коммунальная платёжка идёт своим контуром: там назначение платежа собирает
              utility_recognition, и это окно его не правит. */}
          {!intake.utility_kind ? (
            <VatFields
              mode={intake.vat_mode}
              value={vat}
              onChange={setVat}
              invoiceAmount={intake.amount}
            />
          ) : null}

          <div className="grid gap-2">
            <RequisitesFields
              values={r}
              sources={sources}
              onChange={setField}
              mismatch={mismatch}
              onPickFromHistory={applyCandidate}
              searchQuery={r.inn || r.recipientName || intake.counterparty_name || ""}
              counterpartyId={intake.counterparty_id}
              highlightMissing={!viaIpCard}
              loading={cardLoading}
            />
            {!ready ? (
              <p className="text-xs text-amber-600">
                Заполните название, ИНН, БИК, расчётный и корреспондентский счета.
              </p>
            ) : null}
            {canPayViaIpCard ? (
              <label className="flex items-start gap-2 text-sm">
                <input
                  checked={payViaIpCard}
                  className="mt-0.5 h-4 w-4"
                  onChange={(event) => setPayViaIpCard(event.target.checked)}
                  type="checkbox"
                />
                <span>
                  У получателя нет реквизитов — вывести на карту ИП
                  <span className="block text-xs text-muted-foreground">
                    {viaIpCard
                      ? "Платёж уйдёт на карту ИП, деньги придут на Сейф. Счёт закроется, когда наличные выданы получателю."
                      : "Для тех, кому платим не по счёту в банке: реквизиты спрашивать не будем."}
                  </span>
                </span>
              </label>
            ) : null}
          </div>

          {/* Период спрашиваем ВСЕГДА, а не только у контрагентов с обязательным периодом:
              оплата — единственный момент, когда человек держит счёт перед глазами и знает,
              за что платит. Без этого вопроса период не появлялся вовсе — на проде 01.08.2026
              он не был указан у ВСЕХ 19 открытых предоплат. Исключение одно: режим «счёт +
              УПД», где сумму расхода и его период приносит документ. */}
          {askPeriod ? (
            <div className="grid gap-2 rounded-md border p-3">
              <div className="flex items-center justify-between gap-2">
                <div className="text-xs font-medium uppercase text-muted-foreground">
                  Период оказания услуги
                  {!intake.service_period_required ? (
                    <span className="ml-1 normal-case text-muted-foreground/70">
                      — если платите за период
                    </span>
                  ) : null}
                </div>
                {intake.service_period_source?.startsWith("document") ||
                intake.service_period_source?.startsWith("subject") ? (
                  <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-[11px] text-emerald-700">
                    определён автоматически
                  </span>
                ) : null}
              </div>
              <div className="grid grid-cols-2 gap-2">
                <div className="grid gap-1">
                  <Label className="text-xs text-muted-foreground">С</Label>
                  <Input
                    type="date"
                    value={periodStart}
                    onChange={(event) => setPeriodStart(event.target.value)}
                    className={!periodStart && intake.service_period_required ? "border-amber-400" : undefined}
                  />
                </div>
                <div className="grid gap-1">
                  <Label className="text-xs text-muted-foreground">По</Label>
                  <Input
                    type="date"
                    min={periodStart || undefined}
                    value={periodEnd}
                    onChange={(event) => setPeriodEnd(event.target.value)}
                    className={!periodEnd && intake.service_period_required ? "border-amber-400" : undefined}
                  />
                </div>
              </div>
              <p className="text-xs text-muted-foreground">
                Период говорит, за что заплачено: по нему считается срок закрывающего документа,
                а если документа не будет — расход признаётся по окончании периода.
              </p>
              {periodHalfFilled ? (
                <p className="text-xs text-amber-600">
                  Укажите обе даты — период с одной границей не сохранится.
                </p>
              ) : !periodReady ? (
                <p className="text-xs text-amber-600">
                  Для этого контрагента период обязателен — без него отправка недоступна.
                </p>
              ) : null}
            </div>
          ) : null}

          <div className="grid gap-1">
            <Label className="text-xs text-muted-foreground">Дата отправки в банк</Label>
            <Input
              type="date"
              min={today}
              value={date}
              onChange={(e) => setDate(e.target.value || today)}
              className="w-44"
            />
            <p className="text-xs text-muted-foreground">
              {isNow
                ? "Сегодня — счёт уйдёт в банк сразу."
                : "Будущая дата — счёт уйдёт автоматически в этот день."}
            </p>
          </div>

          <div className="grid gap-1.5">
            <Label className="text-xs text-muted-foreground">Статья ДДС оплаты</Label>
            <ArticleCombobox
              articles={articlesQuery.data ?? []}
              value={ddsArticleId || "none"}
              onChange={(value) => setDdsArticleId(value === "none" ? "" : value)}
            />
            <label className="mt-0.5 flex items-center gap-2 text-sm">
              <input
                checked={rememberForCp}
                className="h-4 w-4"
                onChange={(e) => setRememberForCp(e.target.checked)}
                type="checkbox"
              />
              Закрепить статью за контрагентом
            </label>
            <p className="text-xs text-muted-foreground">
              Не складские поставщики (ПО, реклама, техподдержка) — оплата пойдёт по выбранной
              статье; без выбора — «Оплата поставщикам».
            </p>
          </div>
          </div>
        </div>

        <DialogFooter className="sm:justify-between">
          {intake.scheduled_send_date ? (
            <Button
              variant="ghost"
              className="text-red-600 hover:text-red-700"
              onClick={() => cancel.mutate()}
              disabled={busy}
            >
              Отменить план
            </Button>
          ) : (
            <span />
          )}
          <div className="flex gap-2">
            <Button variant="outline" onClick={onClose} disabled={busy}>
              Отмена
            </Button>
            <Button
              onClick={() => send.mutate()}
              disabled={!ready || !periodReady || !vatReady || busy}
            >
              {isNow ? "Отправить в банк" : "Запланировать"}
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
