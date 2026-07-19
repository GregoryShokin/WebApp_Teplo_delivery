import { useMemo, useState } from "react";
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
  scheduleSend,
  sendToBank,
  type PaymentIntake,
} from "./api";

function ReqField({
  label,
  value,
  onChange,
  missing,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  missing?: boolean;
}) {
  return (
    <div className="grid gap-1">
      <Label className="text-xs text-muted-foreground">{label}</Label>
      <Input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={missing ? "border-amber-400" : undefined}
      />
    </div>
  );
}

// Окно отправки счёта в банк: сверка реквизитов получателя + выбор даты (дефолт — сегодня).
// Сегодня → уходит сразу; будущая дата → запланированная авто-отправка. Реквизиты подтверждаются
// здесь же (apply_requisites), чтобы не было отдельного скрытого барьера.
export function SendDialog({
  intake,
  onClose,
}: {
  intake: PaymentIntake;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const req = useMemo(() => intake.requisites ?? {}, [intake.requisites]);
  const today = todayIso();

  const [date, setDate] = useState(intake.scheduled_send_date ?? today);
  const [periodStart, setPeriodStart] = useState(intake.service_period_start ?? "");
  const [periodEnd, setPeriodEnd] = useState(intake.service_period_end ?? "");
  // Статья ДДС оплаты: уже выбранная на счёте → закреплённая за контрагентом → пусто (на бэке
  // дефолт «Оплата поставщикам»). Чекбокс закрепляет выбранную статью за контрагентом на будущее.
  const [ddsArticleId, setDdsArticleId] = useState(
    intake.invoice_dds_article_id ?? intake.default_dds_article_id ?? "",
  );
  const [rememberForCp, setRememberForCp] = useState(false);
  const articlesQuery = useQuery({ queryKey: ["dds", "articles"], queryFn: getDdsArticles });
  const [r, setR] = useState({
    recipientName: req.recipientName ?? intake.recipient_name ?? "",
    inn: req.inn ?? intake.inn ?? "",
    kpp: req.kpp ?? "",
    bankAcnt: req.bankAcnt ?? "",
    bankBik: req.bankBik ?? "",
    recipientCorrAccountNumber: req.recipientCorrAccountNumber ?? "",
  });
  const setField = (k: keyof typeof r, v: string) => setR((p) => ({ ...p, [k]: v }));

  const invalidate = () =>
    void queryClient.invalidateQueries({ queryKey: ["payment-page", "intakes"] });

  const ready =
    r.recipientName.trim() !== "" &&
    r.bankAcnt.trim() !== "" &&
    r.bankBik.trim() !== "" &&
    r.inn.trim() !== "" &&
    r.recipientCorrAccountNumber.trim() !== "";
  const periodReady = !intake.service_period_required || Boolean(periodStart && periodEnd);
  const isNow = date <= today;

  const send = useMutation({
    mutationFn: async () => {
      // Подтверждаем реквизиты (заносим в карточку + verified), затем отправляем или планируем.
      await confirmIntake(intake.id, {
        requisites: r,
        apply_requisites: true,
        service_period_start: periodStart || null,
        service_period_end: periodEnd || null,
      });
      const choice = {
        dds_article_id: ddsArticleId || null,
        remember_for_counterparty: rememberForCp,
      };
      if (isNow) await sendToBank(intake.id, choice);
      else await scheduleSend(intake.id, date, choice);
    },
    onSuccess: () => {
      invalidate();
      toast.success(
        isNow
          ? "Отправлено в банк — ожидает подтверждения"
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

  return (
    <Dialog open onOpenChange={(open) => (!open ? onClose() : undefined)}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Отправка счёта в банк</DialogTitle>
          <DialogDescription>
            Сверьте реквизиты получателя и выберите дату. Уйдёт банковский черновик — деньги не
            списываются, окончательное подтверждение в банке.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-3">
          <div className="flex items-baseline justify-between rounded-md border bg-muted/30 px-3 py-2">
            <div className="font-medium">
              {r.recipientName || intake.counterparty_name || "—"}
              {intake.invoice_number ? (
                <span className="ml-2 text-sm text-muted-foreground">№ {intake.invoice_number}</span>
              ) : null}
            </div>
            <div className="text-lg font-semibold tabular-nums">
              {intake.amount ? formatRub(intake.amount) : "—"}
            </div>
          </div>

          <div className="grid gap-2 rounded-md border p-3">
            <div className="text-xs font-medium uppercase text-muted-foreground">
              Реквизиты получателя
            </div>
            <ReqField
              label="Официальное название"
              value={r.recipientName}
              onChange={(v) => setField("recipientName", v)}
              missing={r.recipientName.trim() === ""}
            />
            <ReqField
              label="Расчётный счёт"
              value={r.bankAcnt}
              onChange={(v) => setField("bankAcnt", v)}
              missing={r.bankAcnt.trim() === ""}
            />
            <div className="grid gap-2 sm:grid-cols-2">
              <ReqField
                label="БИК"
                value={r.bankBik}
                onChange={(v) => setField("bankBik", v)}
                missing={r.bankBik.trim() === ""}
              />
              <ReqField
                label="ИНН"
                value={r.inn}
                onChange={(v) => setField("inn", v)}
                missing={r.inn.trim() === ""}
              />
            </div>
            <div className="grid gap-2 sm:grid-cols-2">
              <ReqField label="КПП" value={r.kpp} onChange={(v) => setField("kpp", v)} />
              <ReqField
                label="Корр. счёт"
                value={r.recipientCorrAccountNumber}
                onChange={(v) => setField("recipientCorrAccountNumber", v)}
                missing={r.recipientCorrAccountNumber.trim() === ""}
              />
            </div>
            {!ready ? (
              <p className="text-xs text-amber-600">
                Заполните название, ИНН, БИК, расчётный и корреспондентский счета.
              </p>
            ) : null}
          </div>

          {intake.service_period_required || periodStart || periodEnd ? (
            <div className="grid gap-2 rounded-md border p-3">
              <div className="flex items-center justify-between gap-2">
                <div className="text-xs font-medium uppercase text-muted-foreground">
                  Период оказания услуги
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
                По окончании этого периода расход автоматически попадёт в P&L, а предоплата
                закроется в учёте ДЗ/КЗ.
              </p>
              {!periodReady ? (
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
            <Button onClick={() => send.mutate()} disabled={!ready || !periodReady || busy}>
              {isNow ? "Отправить в банк" : "Запланировать"}
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
