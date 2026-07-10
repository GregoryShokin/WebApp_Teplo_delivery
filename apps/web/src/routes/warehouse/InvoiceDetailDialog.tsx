import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Banknote, LoaderCircle, Pencil, Send, Trash2 } from "lucide-react";
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
import { apiErrorMessage } from "@/lib/api";
import { usePermissions } from "@/lib/permissions";
import { formatRub } from "@/routes/counterparties/shared";

import { InvoiceEditDialog } from "./InvoiceEditDialog";
import { PayWarehouseInvoiceDialog } from "./PayWarehouseInvoiceDialog";
import {
  deleteWarehouseInvoice,
  getWarehouseInvoice,
  pushInvoiceToIiko,
  type WarehouseInvoiceDetail,
} from "./api";

/** Статус отправки накладной в iiko: подпись, цвет, нужна ли кнопка «Отправить». */
const IIKO_STATUS: Record<string, { label: string; cls: string; canSend: boolean }> = {
  pushed: { label: "Отправлена в iiko", cls: "border-emerald-200 bg-emerald-50 text-emerald-700", canSend: false },
  failed: { label: "Ошибка отправки в iiko", cls: "border-red-200 bg-red-50 text-red-700", canSend: true },
  skipped: { label: "Не отправляется в iiko", cls: "border-slate-200 bg-slate-50 text-slate-600", canSend: false },
  not_pushed: { label: "Не отправлена в iiko", cls: "border-amber-200 bg-amber-50 text-amber-700", canSend: true },
};

function formatDateTime(value: string | null): string {
  if (!value) return "—";
  return new Date(value).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" });
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
  const [editing, setEditing] = useState(false);
  const [adjustingPaid, setAdjustingPaid] = useState(false);
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

            {detail.payment_status === "unpaid" && !detail.barter_role && !detail.draft_id ? (
              <div className="flex flex-wrap gap-2">
                {canEdit ? (
                  <Button size="sm" variant="outline" onClick={() => setEditing(true)}>
                    <Pencil size={14} aria-hidden="true" /> Редактировать позиции
                  </Button>
                ) : null}
                {onKassaPay ? (
                  // Кассовый контекст: только наличная оплата с ТК Черникова (как строчная
                  // кнопка Кассы) — банк/сплит администратору Кассы недоступны.
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => {
                      onOpenChange(false);
                      onKassaPay(detail);
                    }}
                  >
                    <Banknote size={14} aria-hidden="true" /> Оплатить с ТК Черникова
                  </Button>
                ) : canPay ? (
                  <Button size="sm" variant="outline" onClick={() => setPaying(true)}>
                    <Banknote size={14} aria-hidden="true" /> Оплатить
                  </Button>
                ) : null}
                {canEdit ? (
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
                edit_paid (owner/admin). Излишек оплаты уходит в дебиторку. Не для бартера и
                не пока платёж в банке (draft_id). */}
            {(detail.payment_status === "paid" || detail.payment_status === "partially_paid") &&
            !detail.barter_role &&
            !detail.draft_id &&
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
                  onClick={() => setAdjustingPaid(true)}
                >
                  <Pencil size={14} aria-hidden="true" /> Исправить оплаченную
                </Button>
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
                      <td className="px-3 py-2 text-right tabular-nums">{formatRub(line.price)}</td>
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
