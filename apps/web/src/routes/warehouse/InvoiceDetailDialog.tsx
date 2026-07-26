import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Banknote,
  CalendarClock,
  CheckCircle2,
  LoaderCircle,
  Pencil,
  Send,
  ShieldAlert,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { apiErrorMessage } from "@/lib/api";
import { usePermissions } from "@/lib/permissions";
import { formatRub } from "@/routes/counterparties/shared";

import { InvoiceEditDialog } from "./InvoiceEditDialog";
import { PayWarehouseInvoiceDialog } from "./PayWarehouseInvoiceDialog";
import {
  confirmIikoPaymentManual,
  confirmInvoicePrices,
  deleteWarehouseInvoice,
  getWarehouseInvoice,
  pushInvoiceToIiko,
  retryIikoReturn,
  sendIikoPayment,
  setInvoiceServicePeriod,
  type WarehouseInvoiceDetail,
} from "./api";

/** Статус отправки накладной в iiko: подпись, цвет, нужна ли кнопка «Отправить». */
const IIKO_STATUS: Record<string, { label: string; cls: string; canSend: boolean }> = {
  pushed: { label: "Отправлена в iiko", cls: "border-emerald-200 bg-emerald-50 text-emerald-700", canSend: false },
  failed: { label: "Ошибка отправки в iiko", cls: "border-red-200 bg-red-50 text-red-700", canSend: true },
  skipped: { label: "Не отправляется в iiko", cls: "border-slate-200 bg-slate-50 text-slate-600", canSend: false },
  not_pushed: { label: "Не отправлена в iiko", cls: "border-amber-200 bg-amber-50 text-amber-700", canSend: true },
};

/** Статус отражения коррекции оплаченной накладной в iiko (возврат старого прихода + новая приходная). */
const RETURN_STATUS: Record<string, { label: string; cls: string }> = {
  booked: { label: "Коррекция отражена в iiko", cls: "border-emerald-200 bg-emerald-50 text-emerald-700" },
  pending: { label: "Коррекция в iiko ожидает", cls: "border-amber-200 bg-amber-50 text-amber-700" },
  failed: { label: "Ошибка коррекции в iiko", cls: "border-red-200 bg-red-50 text-red-700" },
  skipped: { label: "Коррекция в iiko не требуется", cls: "border-slate-200 bg-slate-50 text-slate-600" },
};

function formatDateTime(value: string | null): string {
  if (!value) return "—";
  return new Date(value).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" });
}

function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  return new Date(value).toLocaleDateString("ru-RU", { dateStyle: "short" });
}

/** Первое и последнее число текущего месяца в формате input[type=date] — разумный дефолт
 *  для платежа без счёта (период чаще всего = месяц оплаты). */
function currentMonthRange(): { start: string; end: string } {
  const now = new Date();
  const iso = (d: Date) =>
    `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  return {
    start: iso(new Date(now.getFullYear(), now.getMonth(), 1)),
    end: iso(new Date(now.getFullYear(), now.getMonth() + 1, 0)),
  };
}

/** Блок «Период оказания услуги» для накладной контрагента, которому период обязателен.
 *  Пока период не заполнен — накладную нельзя отправить в банк, а заполнить его больше негде
 *  (у накладной не из почты период при создании не ставится). Здесь оператор его вводит. */
function ServicePeriodBlock({
  invoiceId,
  detail,
  canEdit,
}: {
  invoiceId: string;
  detail: WarehouseInvoiceDetail;
  canEdit: boolean;
}) {
  const queryClient = useQueryClient();
  const ready = detail.service_period_status === "ready";
  const [editing, setEditing] = useState(false);
  const fallback = useMemo(currentMonthRange, []);
  const [start, setStart] = useState(detail.service_period_start ?? fallback.start);
  const [end, setEnd] = useState(detail.service_period_end ?? fallback.end);

  const mutation = useMutation({
    mutationFn: () =>
      setInvoiceServicePeriod(invoiceId, {
        service_period_start: start,
        service_period_end: end,
      }),
    onSuccess: () => {
      queryClient.setQueryData<WarehouseInvoiceDetail | undefined>(
        ["wh", "invoice", invoiceId],
        (prev) =>
          prev
            ? {
                ...prev,
                service_period_status: "ready",
                service_period_start: start,
                service_period_end: end,
              }
            : prev,
      );
      void queryClient.invalidateQueries({ queryKey: ["wh"] });
      void queryClient.invalidateQueries({ queryKey: ["cp"] });
      toast.success("Период оказания услуги сохранён");
      setEditing(false);
    },
    onError: (e) => toast.error(apiErrorMessage(e, "Не удалось сохранить период")),
  });

  const canSubmit = Boolean(start && end) && start <= end && !mutation.isPending;

  // Заполнен и не редактируем — компактная строка со сводкой и кнопкой «Изменить».
  if (ready && !editing) {
    return (
      <div className="flex flex-wrap items-center gap-2 rounded-md border p-3 text-sm">
        <CalendarClock size={16} className="text-muted-foreground" aria-hidden="true" />
        <span>
          Период оказания услуги:{" "}
          <span className="font-medium">
            {formatDate(detail.service_period_start)} — {formatDate(detail.service_period_end)}
          </span>
        </span>
        {canEdit ? (
          <Button
            size="sm"
            variant="outline"
            className="ml-auto"
            onClick={() => setEditing(true)}
          >
            <Pencil size={14} aria-hidden="true" /> Изменить
          </Button>
        ) : null}
      </div>
    );
  }

  const border = ready ? "border" : "border-amber-200 bg-amber-50/40";
  return (
    <div className={`grid gap-3 rounded-md p-3 ${border}`}>
      <div className="flex items-start gap-2 text-sm">
        <CalendarClock
          size={16}
          className={ready ? "mt-0.5 text-muted-foreground" : "mt-0.5 text-amber-700"}
          aria-hidden="true"
        />
        <div>
          <span className="block font-medium">Период оказания услуги</span>
          {!ready ? (
            <span className="block text-xs text-amber-700">
              Не заполнен — накладную нельзя отправить в банк, пока не укажете период.
            </span>
          ) : null}
        </div>
      </div>
      {canEdit ? (
        <>
          <div className="grid grid-cols-2 gap-3">
            <div className="flex flex-col gap-1.5">
              <Label className="text-xs">С</Label>
              <Input
                type="date"
                value={start}
                max={end || undefined}
                onChange={(e) => setStart(e.target.value)}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label className="text-xs">По</Label>
              <Input
                type="date"
                value={end}
                min={start || undefined}
                onChange={(e) => setEnd(e.target.value)}
              />
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Button size="sm" disabled={!canSubmit} onClick={() => mutation.mutate()}>
              {mutation.isPending ? (
                <LoaderCircle size={14} className="animate-spin" aria-hidden="true" />
              ) : (
                <CheckCircle2 size={14} aria-hidden="true" />
              )}
              Сохранить период
            </Button>
            {ready ? (
              <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
                Отмена
              </Button>
            ) : null}
          </div>
        </>
      ) : (
        <span className="text-xs text-muted-foreground">
          Заполнить период может тот, кто редактирует накладные.
        </span>
      )}
    </div>
  );
}

/** Детализация накладной: позиции (номенклатура + суммы), оплата и статус отправки в iiko
 * с возможностью переотправить. Переиспользуется из Кассы и карточки контрагента.
 *
 * ``onKassaPay`` — кассовый контекст: администратор на Кассе платит ТОЛЬКО наличными с
 * ТК Черникова, поэтому вместо универсальной «Оплатить» (банк/сплит) показывается
 * «Оплатить с ТК Черникова», открывающая кассовый диалог оплаты родителя. */
export function InvoiceDetailDialog({
  invoiceId,
  onOpenChange,
  onKassaPay,
}: {
  invoiceId: string | null;
  onOpenChange: (open: boolean) => void;
  onKassaPay?: (detail: WarehouseInvoiceDetail) => void;
}) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  // Отправку в iiko может делать управляющий/менеджер (counterparties.operate) и
  // кассир — через узкое право kassa.invoices.push (накладные из Кассы).
  const canPush =
    permissions.canPerformAction("counterparties.operate") ||
    permissions.canPerformAction("kassa.invoices.push");
  const canEdit =
    permissions.canPerformAction("invoices.normal.edit") ||
    permissions.canPerformAction("kassa.invoices.create");
  const canPay = permissions.canPerformAction("invoices.normal.pay");
  // Точечное право «правка ОПЛАЧЕННОЙ» (owner/admin): исправить не ту накладную, излишек → дебиторка.
  const canEditPaid = permissions.canPerformAction("invoices.normal.edit_paid");
  // Точечное право «подтвердить подозрительные цены» (owner/admin/менеджер).
  const canConfirmPrice = permissions.hasPermission("invoices.confirm_price");
  const [editing, setEditing] = useState(false);
  const [adjustingPaid, setAdjustingPaid] = useState(false);
  const [iikoGateOpen, setIikoGateOpen] = useState(false);
  const [paying, setPaying] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const open = Boolean(invoiceId);

  const detailQuery = useQuery({
    queryKey: ["wh", "invoice", invoiceId],
    queryFn: () => getWarehouseInvoice(invoiceId!),
    enabled: open,
  });
  const detail = detailQuery.data;

  const pushMutation = useMutation({
    mutationFn: () => pushInvoiceToIiko(invoiceId!),
    onSuccess: (updated) => {
      queryClient.setQueryData(["wh", "invoice", invoiceId], updated);
      void queryClient.invalidateQueries({ queryKey: ["wh"] });
      void queryClient.invalidateQueries({ queryKey: ["cp"] });
      if (updated.iiko_push_status === "pushed") toast.success("Накладная отправлена в iiko");
      else toast.error(updated.iiko_push_error ?? "iiko не принял документ");
    },
    onError: (e) => toast.error(apiErrorMessage(e, "Не удалось отправить в iiko")),
  });

  const returnRetryMutation = useMutation({
    mutationFn: () => retryIikoReturn(invoiceId!),
    onSuccess: (updated) => {
      queryClient.setQueryData(["wh", "invoice", invoiceId], updated);
      void queryClient.invalidateQueries({ queryKey: ["wh"] });
      void queryClient.invalidateQueries({ queryKey: ["cp"] });
      if (updated.iiko_return_status === "booked") toast.success("Коррекция отражена в iiko");
      else toast.error(updated.iiko_return_error ?? "iiko не принял коррекцию");
    },
    onError: (e) => toast.error(apiErrorMessage(e, "Не удалось отразить коррекцию в iiko")),
  });

  const confirmPricesMutation = useMutation({
    mutationFn: () => confirmInvoicePrices(invoiceId!),
    onSuccess: (updated) => {
      queryClient.setQueryData(["wh", "invoice", invoiceId], updated);
      void queryClient.invalidateQueries({ queryKey: ["wh"] });
      void queryClient.invalidateQueries({ queryKey: ["cp"] });
      toast.success("Цены подтверждены — оплата разблокирована");
    },
    onError: (e) => toast.error(apiErrorMessage(e, "Не удалось подтвердить цены")),
  });

  // Гард правки оплаченной: провести оплату оригинала в iiko сейчас (add_payment). Успех
  // (в т.ч. «already paid») → снять гейт и открыть форму правки; иначе — предложить ручное.
  const sendIikoPaymentMutation = useMutation({
    mutationFn: () => sendIikoPayment(invoiceId!),
    onSuccess: (updated) => {
      queryClient.setQueryData(["wh", "invoice", invoiceId], updated);
      void queryClient.invalidateQueries({ queryKey: ["wh"] });
      void queryClient.invalidateQueries({ queryKey: ["cp"] });
      if (updated.iiko_payment_settled) {
        toast.success("Оплата отправлена в iiko");
        setIikoGateOpen(false);
        setAdjustingPaid(true);
      } else {
        toast.error(
          updated.iiko_payment_push_error ?? "iiko не принял оплату — подтвердите вручную",
        );
      }
    },
    onError: (e) => toast.error(apiErrorMessage(e, "Не удалось отправить оплату в iiko")),
  });

  // Гард правки оплаченной: пометить, что оплата уже проведена в iiko вручную (снять гейт).
  const confirmIikoManualMutation = useMutation({
    mutationFn: () => confirmIikoPaymentManual(invoiceId!),
    onSuccess: (updated) => {
      queryClient.setQueryData(["wh", "invoice", invoiceId], updated);
      void queryClient.invalidateQueries({ queryKey: ["wh"] });
      void queryClient.invalidateQueries({ queryKey: ["cp"] });
      toast.success("Отмечено: оплата подтверждена в iiko");
      setIikoGateOpen(false);
      setAdjustingPaid(true);
    },
    onError: (e) => toast.error(apiErrorMessage(e, "Не удалось отметить оплату")),
  });

  const deleteMutation = useMutation({
    mutationFn: () => deleteWarehouseInvoice(invoiceId!),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["wh"] });
      void queryClient.invalidateQueries({ queryKey: ["cp"] });
      toast.success("Накладная удалена");
      setConfirmingDelete(false);
      onOpenChange(false);
    },
    onError: (e) => toast.error(apiErrorMessage(e, "Не удалось удалить накладную")),
  });

  const iiko = detail ? IIKO_STATUS[detail.iiko_push_status] ?? null : null;
  const hasStaff = (detail?.staff_amount ?? 0) > 0;
  // Отправка в iiko уместна только для документов, созданных у нас (вручную / Касса:
  // накладная и чек). source=iiko пришли ИЗ iiko — повторный пуш создал бы дубль.
  const canPushSource =
    detail?.source === "manual" ||
    detail?.source === "kassa_invoice" ||
    detail?.source === "kassa_cheque";
  // Есть ли что отправлять в iiko: только товарные строки (не расходные статьи). Если
  // товара нет (всё «персонал»/расходы), iiko-приход не создаётся — кнопку не показываем.
  const hasGoods = (detail?.lines ?? []).some((line) => !line.is_expense);
  // Исправлять оплаченную можно только если есть сохранённые позиции — правка идёт через них.
  // Старые iiko-накладные без строк («Позиции не сохранены») так править нельзя: нечего менять.
  const hasLines = (detail?.lines ?? []).length > 0;
  // Контроль цен: flagged — подозрительно (оплата/банк заблокированы до подтверждения);
  // confirmed — цены сверены человеком. Показываем баннер и блокируем «Оплатить».
  const priceFlagged = detail?.price_control_status === "flagged";
  const priceConfirmed = detail?.price_control_status === "confirmed";
  const priceAnomalies = detail?.price_anomalies ?? [];
  // Есть что доплачивать. Раньше кнопка «Оплатить» жила под общим условием payment_status ===
  // 'unpaid' вместе с «Редактировать»/«Удалить», поэтому ПОСЛЕ первой же частичной оплаты она
  // исчезала, и хвост нельзя было закрыть ничем — ни авансом, ни банком, ни наличными (кейс
  // Лигая 26.07: зачёт аванса покрыл 153 918 из 153 978, оставшиеся 60 ₽ добить было нечем).
  // Правка/удаление по-прежнему только для полностью неоплаченной — бэкенд требует именно
  // unpaid (update_warehouse_invoice, ensure_invoice_deletable), иначе вернёт 409.
  const hasUnpaidRemainder = (detail?.remaining ?? 0) > 0 && detail?.payment_status !== "void";
  const isUnpaid = detail?.payment_status === "unpaid";

  return (
    <>
      <InvoiceEditDialog
        invoiceId={editing ? invoiceId : null}
        onOpenChange={(o) => !o && setEditing(false)}
      />
      <InvoiceEditDialog
        invoiceId={adjustingPaid ? invoiceId : null}
        onOpenChange={(o) => !o && setAdjustingPaid(false)}
        paid
      />
      <PayWarehouseInvoiceDialog
        invoiceId={paying ? invoiceId : null}
        onOpenChange={(o) => !o && setPaying(false)}
      />
      <AlertDialog open={confirmingDelete} onOpenChange={setConfirmingDelete}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              Удалить накладную {detail?.number ? `№${detail.number}` : ""}?
            </AlertDialogTitle>
            <AlertDialogDescription>
              {detail?.external_id
                ? "Накладная будет удалена и здесь, и в iiko (документ распроведётся и попадёт в удалённые). Действие необратимо."
                : "Накладная будет удалена из приложения. Действие необратимо."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Отмена</AlertDialogCancel>
            <AlertDialogAction
              disabled={deleteMutation.isPending}
              onClick={() => deleteMutation.mutate()}
            >
              {deleteMutation.isPending ? "Удаляем…" : "Удалить"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
      {/* Гард правки оплаченной: пока оплата оригинала не отражена в iiko — правку не запускаем,
          предлагаем провести оплату сейчас или подтвердить её вручную. */}
      <AlertDialog open={iikoGateOpen} onOpenChange={setIikoGateOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Сначала проведите оплату в iiko</AlertDialogTitle>
            <AlertDialogDescription>
              Оплата этой накладной ещё не отражена в iiko. Если исправить её сейчас, возврат в
              iiko уйдёт не на ту сумму и баланс поставщика перекосится.{" "}
              {detail?.iiko_payment_auto_sendable
                ? "Отправьте оплату в iiko, затем исправляйте."
                : "Дождитесь автосинхронизации (несколько минут) или подтвердите, если оплата уже проведена вручную в бэк-офисе iiko."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          {/* Одна безопасная кнопка: если оплату можно отправить автоматически — «Отправить сейчас»
              (реальный add_payment, «already paid» = успех). Иначе (мультиплатёж/аванс/наличные/
              Касса) — только ручное подтверждение. Вслепую глушить авто-зеркало НЕ даём. */}
          <AlertDialogFooter className="flex-col gap-2 sm:flex-row">
            <AlertDialogCancel>Отмена</AlertDialogCancel>
            {detail?.iiko_payment_auto_sendable ? (
              <Button
                disabled={sendIikoPaymentMutation.isPending}
                onClick={() => sendIikoPaymentMutation.mutate()}
              >
                {sendIikoPaymentMutation.isPending ? (
                  <LoaderCircle size={14} className="animate-spin" aria-hidden="true" />
                ) : (
                  <Send size={14} aria-hidden="true" />
                )}
                Отправить оплату в iiko сейчас
              </Button>
            ) : (
              <Button
                disabled={confirmIikoManualMutation.isPending}
                onClick={() => confirmIikoManualMutation.mutate()}
              >
                {confirmIikoManualMutation.isPending ? (
                  <LoaderCircle size={14} className="animate-spin" aria-hidden="true" />
                ) : null}
                Оплата подтверждена вручную
              </Button>
            )}
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>Накладная {detail?.number ? `№${detail.number}` : ""}</DialogTitle>
          </DialogHeader>

        {detailQuery.isLoading || !detail ? (
          <div className="flex justify-center py-8 text-muted-foreground">
            <LoaderCircle className="animate-spin" aria-hidden="true" />
          </div>
        ) : (
          <div className="grid gap-4">
            <div className="text-sm text-muted-foreground">
              {detail.counterparty_name} · {formatDateTime(detail.issued_at)} · всего{" "}
              <span className="font-medium tabular-nums text-foreground">
                {formatRub(detail.amount)}
              </span>
              {hasStaff ? (
                <span className="ml-1">
                  (товар {formatRub(detail.production_amount)} · персонал{" "}
                  {formatRub(detail.staff_amount)})
                </span>
              ) : null}
            </div>

            {/* Контроль ошибочных цен: подозрительную накладную нельзя оплатить/отправить в банк,
                пока цены не подтверждены. Баннер показывает аномальные позиции (цена vs среднее). */}
            {priceFlagged ? (
              <div className="rounded-md border border-red-200 bg-red-50 p-3">
                <div className="flex items-center gap-2 text-sm font-medium text-red-800">
                  <ShieldAlert size={16} aria-hidden="true" />
                  Подозрительные цены — оплата и отправка в банк заблокированы
                </div>
                <ul className="mt-2 space-y-1 text-xs text-red-700">
                  {priceAnomalies.map((a, i) => (
                    <li key={`${a.product_guid ?? a.name}-${i}`} className="tabular-nums">
                      <span className="font-medium">{a.name}</span>: {formatRub(a.price)} —{" "}
                      {a.direction === "high" ? "дороже" : "дешевле"} среднего{" "}
                      {formatRub(a.avg_price)} на{" "}
                      <span className="font-medium">
                        {a.deviation_pct > 0 ? "+" : ""}
                        {a.deviation_pct}%
                      </span>{" "}
                      <span className="text-red-500">(по {a.sample_count} закупкам)</span>
                    </li>
                  ))}
                </ul>
                {canConfirmPrice ? (
                  <div className="mt-3 flex items-center gap-2">
                    <Button
                      size="sm"
                      className="bg-red-600 text-white hover:bg-red-700"
                      disabled={confirmPricesMutation.isPending}
                      onClick={() => confirmPricesMutation.mutate()}
                    >
                      {confirmPricesMutation.isPending ? (
                        <LoaderCircle size={14} className="animate-spin" aria-hidden="true" />
                      ) : (
                        <CheckCircle2 size={14} aria-hidden="true" />
                      )}
                      Цены верны — подтвердить
                    </Button>
                    <span className="text-xs text-red-600">
                      После проверки нажмите, чтобы разблокировать оплату.
                    </span>
                  </div>
                ) : (
                  <p className="mt-2 text-xs text-red-600">
                    Подтвердить цены может владелец, администратор или менеджер.
                  </p>
                )}
              </div>
            ) : priceConfirmed ? (
              <div className="flex flex-wrap items-center gap-2 rounded-md border border-emerald-200 bg-emerald-50 p-3 text-xs text-emerald-800">
                <CheckCircle2 size={14} aria-hidden="true" />
                Цены подтверждены
                {detail.price_confirmed_by ? ` — ${detail.price_confirmed_by}` : ""}
                {detail.price_confirmed_at
                  ? ` · ${formatDateTime(detail.price_confirmed_at)}`
                  : ""}
              </div>
            ) : null}

            {/* Период оказания услуги — только для контрагентов, которым он обязателен. Пока
                не заполнен, накладную не отправить в банк; здесь его и вводят (для накладной
                не из почты это единственная точка). */}
            {invoiceId && detail.service_period_required ? (
              <ServicePeriodBlock invoiceId={invoiceId} detail={detail} canEdit={canEdit} />
            ) : null}

            {(isUnpaid || hasUnpaidRemainder) && !detail.barter_role && !detail.draft_id ? (
              <div className="flex flex-wrap gap-2">
                {canEdit && isUnpaid ? (
                  <Button size="sm" variant="outline" onClick={() => setEditing(true)}>
                    <Pencil size={14} aria-hidden="true" /> Редактировать позиции
                  </Button>
                ) : null}
                {hasUnpaidRemainder && onKassaPay ? (
                  // Кассовый контекст: только наличная оплата с ТК Черникова (как строчная
                  // кнопка Кассы) — банк/сплит администратору Кассы недоступны.
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={priceFlagged}
                    title={priceFlagged ? "Сначала подтвердите цены" : undefined}
                    onClick={() => {
                      onOpenChange(false);
                      onKassaPay(detail);
                    }}
                  >
                    <Banknote size={14} aria-hidden="true" /> Оплатить с ТК Черникова
                  </Button>
                ) : hasUnpaidRemainder && canPay ? (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={priceFlagged}
                    title={priceFlagged ? "Сначала подтвердите цены" : undefined}
                    onClick={() => setPaying(true)}
                  >
                    <Banknote size={14} aria-hidden="true" /> Оплатить
                  </Button>
                ) : null}
                {canEdit && isUnpaid ? (
                  <Button
                    size="sm"
                    variant="outline"
                    className="border-red-200 text-red-700 hover:bg-red-50"
                    disabled={deleteMutation.isPending}
                    onClick={() => setConfirmingDelete(true)}
                  >
                    <Trash2 size={14} aria-hidden="true" /> Удалить
                  </Button>
                ) : null}
              </div>
            ) : null}

            {/* Правка УЖЕ ОПЛАЧЕННОЙ (поставщик прислал не ту накладную) — точечное право
                edit_paid (owner/admin). Излишек оплаты уходит в дебиторку. Не для бартера.
                Черновик блокирует, только пока платёж НЕ финализирован (висит в банке / резерв
                на Сейфе); банк-оплаченную (draft.status='paid', не через Сейф) править можно. */}
            {(detail.payment_status === "paid" || detail.payment_status === "partially_paid") &&
            !detail.barter_role &&
            (!detail.draft_id ||
              (detail.draft_status === "paid" && !detail.draft_pays_via_safe)) &&
            hasLines &&
            canEditPaid ? (
              <div className="flex flex-wrap items-center gap-2 rounded-md border border-amber-200 bg-amber-50/40 p-3">
                <span className="text-xs text-amber-800">
                  Поставщик прислал не ту накладную? Исправьте позиции/сумму — излишек оплаты уйдёт
                  в дебиторку поставщику.
                </span>
                <Button
                  size="sm"
                  variant="outline"
                  className="ml-auto border-amber-300 text-amber-800 hover:bg-amber-100"
                  onClick={() => {
                    // Гард: накладную нельзя корректировать, пока её оплата не отражена в iiko
                    // (иначе возврат уйдёт не на ту сумму). Бэкенд считает iiko_payment_settled по
                    // контуру источника (iiko/Касса); для незащищённых источников оно = true.
                    if (detail.external_id && detail.iiko_payment_settled === false) {
                      setIikoGateOpen(true);
                    } else {
                      setAdjustingPaid(true);
                    }
                  }}
                >
                  <Pencil size={14} aria-hidden="true" /> Исправить оплаченную
                </Button>
              </div>
            ) : null}

            {/* Статус отражения коррекции в iiko (возврат старого прихода + новая приходная) + ретрай при сбое. */}
            {detail.iiko_return_status && detail.iiko_return_status !== "none" ? (
              <div className="flex flex-wrap items-center gap-2 rounded-md border p-3">
                <Badge
                  variant="outline"
                  className={RETURN_STATUS[detail.iiko_return_status]?.cls}
                >
                  {RETURN_STATUS[detail.iiko_return_status]?.label ?? detail.iiko_return_status}
                </Badge>
                {detail.iiko_return_error ? (
                  <span className="text-xs text-muted-foreground">{detail.iiko_return_error}</span>
                ) : null}
                {canEditPaid &&
                (detail.iiko_return_status === "failed" ||
                  detail.iiko_return_status === "pending") ? (
                  <Button
                    size="sm"
                    variant="outline"
                    className="ml-auto"
                    disabled={returnRetryMutation.isPending}
                    onClick={() => returnRetryMutation.mutate()}
                  >
                    {returnRetryMutation.isPending ? (
                      <LoaderCircle size={14} className="animate-spin" aria-hidden="true" />
                    ) : (
                      <Send size={14} aria-hidden="true" />
                    )}
                    Повторить коррекцию в iiko
                  </Button>
                ) : null}
              </div>
            ) : null}

            {/* Платёж уехал: правка/оплата/удаление недоступны до отзыва платежа. Для
                неофициала после исполнения перевода деньги ждут на Сейфе — наличные
                нужно выдать поставщику (гашение произойдёт при выплате резерва). */}
            {detail.payment_status !== "paid" && detail.draft_id ? (
              <div className="rounded-md border p-3 text-sm">
                {detail.draft_pays_via_safe && detail.draft_status === "paid" ? (
                  <>
                    <Badge className="border-violet-200 bg-violet-50 text-violet-700">
                      Деньги в Сейфе
                    </Badge>
                    <span className="ml-2 text-muted-foreground">
                      Перевод исполнен, деньги на Сейфе — выдайте наличные поставщику
                      (накладная станет «Оплачено» после выплаты резерва).
                    </span>
                  </>
                ) : (
                  <>
                    <Badge className="border-sky-200 bg-sky-50 text-sky-700">
                      Отправлено в банк
                    </Badge>
                    <span className="ml-2 text-muted-foreground">
                      Черновик платежа в банке — правка и повторная оплата недоступны.
                    </span>
                  </>
                )}
              </div>
            ) : null}

            {/* Статус в iiko + переотправка — только для созданных у нас документов.
                Если товарных строк нет (всё расходы/персонал) — отправлять нечего, даже
                если статус ещё «не отправлена»: показываем «не отправляется», без кнопки. */}
            {canPushSource ? (
              !hasGoods && detail.iiko_push_status !== "pushed" ? (
                <div className="flex flex-wrap items-center gap-2 rounded-md border p-3">
                  <Badge variant="outline" className={IIKO_STATUS.skipped.cls}>
                    {IIKO_STATUS.skipped.label}
                  </Badge>
                  <span className="text-xs text-muted-foreground">
                    Нет товарных строк для iiko (расходные статьи / персонал)
                  </span>
                </div>
              ) : (
              <div className="flex flex-wrap items-center gap-2 rounded-md border p-3">
                {iiko ? (
                  <Badge variant="outline" className={iiko.cls}>
                    {iiko.label}
                  </Badge>
                ) : (
                  <Badge variant="outline">{detail.iiko_push_status}</Badge>
                )}
                {detail.iiko_push_error ? (
                  <span className="text-xs text-muted-foreground">{detail.iiko_push_error}</span>
                ) : null}
                {canPush && iiko?.canSend && hasGoods ? (
                  <Button
                    size="sm"
                    variant="outline"
                    className="ml-auto"
                    disabled={pushMutation.isPending}
                    onClick={() => pushMutation.mutate()}
                  >
                    {pushMutation.isPending ? (
                      <LoaderCircle size={14} className="animate-spin" aria-hidden="true" />
                    ) : (
                      <Send size={14} aria-hidden="true" />
                    )}
                    {detail.iiko_push_status === "failed"
                      ? "Переотправить в iiko"
                      : "Отправить в iiko"}
                  </Button>
                ) : null}
              </div>
              )
            ) : null}

            {/* Позиции */}
            <div className="overflow-hidden rounded-md border">
              <table className="w-full text-sm">
                <thead className="bg-muted/50 text-xs text-muted-foreground">
                  <tr>
                    <th className="px-3 py-2 text-left font-medium">Позиция</th>
                    <th className="px-3 py-2 text-right font-medium">Кол-во</th>
                    <th className="px-3 py-2 text-right font-medium">Цена</th>
                    <th className="px-3 py-2 text-right font-medium">Сумма</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.lines.map((line) => (
                    <tr key={line.id} className={line.is_return ? "border-t bg-red-50" : "border-t"}>
                      <td className={line.is_return ? "px-3 py-2 text-red-700" : "px-3 py-2"}>
                        <span className={line.is_return ? "line-through decoration-red-400" : undefined}>
                          {line.name}
                        </span>
                        {line.unit ? (
                          <span className="ml-1 text-xs text-muted-foreground">{line.unit}</span>
                        ) : null}
                        {line.is_return ? (
                          <Badge
                            variant="outline"
                            className="ml-2 border-red-200 bg-red-50 text-[10px] text-red-700"
                          >
                            возврат
                          </Badge>
                        ) : null}
                        {line.is_expense ? (
                          <Badge
                            variant="outline"
                            className="ml-2 border-amber-200 bg-amber-50 text-[10px] text-amber-700"
                          >
                            {line.is_staff ? "персонал" : line.dds_article_name ?? "расход"}
                          </Badge>
                        ) : null}
                      </td>
                      <td className="px-3 py-2 text-right tabular-nums">{line.quantity}</td>
                      <td
                        className={
                          line.price_flag
                            ? "px-3 py-2 text-right font-medium tabular-nums text-red-700"
                            : "px-3 py-2 text-right tabular-nums"
                        }
                        title={
                          line.price_flag && line.price_avg != null
                            ? `Среднее ${formatRub(line.price_avg)} · отклонение ${
                                (line.price_deviation_pct ?? 0) > 0 ? "+" : ""
                              }${line.price_deviation_pct}%`
                            : undefined
                        }
                      >
                        {formatRub(line.price)}
                        {line.price_flag ? (
                          <ShieldAlert
                            size={12}
                            className="ml-1 inline align-[-1px] text-red-500"
                            aria-label="Цена отклоняется от среднего"
                          />
                        ) : null}
                      </td>
                      <td
                        className={
                          line.is_return
                            ? "px-3 py-2 text-right tabular-nums text-red-700 line-through decoration-red-400"
                            : "px-3 py-2 text-right tabular-nums"
                        }
                      >
                        {formatRub(line.sum)}
                      </td>
                    </tr>
                  ))}
                  {detail.lines.length === 0 ? (
                    <tr>
                      <td colSpan={4} className="px-3 py-4 text-center text-muted-foreground">
                        Позиции не сохранены (накладная из старой синхронизации)
                      </td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            </div>

            {/* Оплата */}
            <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
              <span className="text-muted-foreground">
                Оплачено:{" "}
                <span className="font-medium tabular-nums text-foreground">
                  {formatRub(detail.allocated)}
                </span>
              </span>
              <span className="text-muted-foreground">
                Остаток:{" "}
                <span className="font-medium tabular-nums text-foreground">
                  {formatRub(detail.remaining)}
                </span>
              </span>
              {detail.returned_total ? (
                <span className="text-muted-foreground">
                  Возврат в магазин:{" "}
                  <span className="font-medium tabular-nums text-red-700">
                    {formatRub(detail.returned_total)}
                  </span>
                  <span className="ml-1 text-xs">(не проведён, ждём деньги от банка)</span>
                </span>
              ) : null}
            </div>
          </div>
        )}
        </DialogContent>
      </Dialog>
    </>
  );
}
