import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
import { Label } from "@/components/ui/label";
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import { apiErrorMessage } from "@/lib/api";
import { getInvoices, type CounterpartyInvoice } from "@/routes/counterparties/api";
import { formatRub } from "@/routes/counterparties/shared";
import { InvoiceDetailDialog } from "@/routes/warehouse/InvoiceDetailDialog";
import { payInvoiceFromKassa } from "@/routes/warehouse/api";

const STATUS_LABEL: Record<string, string> = {
  unpaid: "Не оплачена",
  partially_paid: "Частично",
};

function formatDate(value: string | null): string {
  return value ? new Date(value).toLocaleDateString("ru-RU") : "—";
}

/** Вкладка «Накладные» Кассы: неоплаченные накладные с доплатой наличными с ТК Черникова
 * (чтобы дооплатить уже созданную накладную, а не пересоздавать её ради тоггла «Оплачено»). */
export function KassaInvoicesTab({ canPay }: { canPay: boolean }) {
  const queryClient = useQueryClient();
  const [payTarget, setPayTarget] = useState<CounterpartyInvoice | null>(null);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [amount, setAmount] = useState("");

  const invoicesQuery = useQuery({
    queryKey: ["kassa", "invoices", "unpaid"],
    queryFn: () =>
      getInvoices({
        status: "unpaid,partially_paid",
        direction: "payable",
        source: "kassa_invoice",
      }),
  });

  const payMutation = useMutation({
    mutationFn: ({ id, amt }: { id: string; amt: number | null }) => payInvoiceFromKassa(id, amt),
    onSuccess: () => {
      toast.success("Накладная оплачена с ТК Черникова");
      void queryClient.invalidateQueries({ queryKey: ["kassa"] });
      void queryClient.invalidateQueries({ queryKey: ["cp"] });
      void queryClient.invalidateQueries({ queryKey: ["dds"] });
      setPayTarget(null);
      setAmount("");
    },
    onError: (e) => toast.error(apiErrorMessage(e, "Не удалось оплатить накладную")),
  });

  const openPay = (invoice: CounterpartyInvoice) => {
    setPayTarget(invoice);
    setAmount(String(invoice.remaining || invoice.amount));
  };

  const columns: Array<DataTableColumn<CounterpartyInvoice>> = [
    {
      key: "cp",
      header: "Поставщик",
      className: "min-w-[180px] font-medium",
      cell: (i) => i.counterparty_name,
    },
    { key: "number", header: "Счёт", cell: (i) => i.number ?? "—" },
    { key: "date", header: "Дата", cell: (i) => formatDate(i.invoice_date) },
    {
      key: "amount",
      header: "Сумма",
      className: "text-right tabular-nums",
      headerClassName: "text-right",
      cell: (i) => formatRub(i.amount),
    },
    {
      key: "remaining",
      header: "Остаток",
      className: "text-right tabular-nums",
      headerClassName: "text-right",
      cell: (i) => formatRub(i.remaining || i.amount),
    },
    {
      key: "status",
      header: "Статус",
      cell: (i) => STATUS_LABEL[i.payment_status] ?? i.payment_status,
    },
    {
      key: "actions",
      header: "",
      className: "text-right",
      cell: (i) => (
        <div className="flex justify-end gap-2">
          <Button size="sm" variant="ghost" onClick={() => setDetailId(i.id)}>
            Детали
          </Button>
          {canPay ? (
            <Button size="sm" variant="outline" onClick={() => openPay(i)}>
              Оплатить с ТК Черникова
            </Button>
          ) : null}
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <DataTable
        columns={columns}
        rows={invoicesQuery.data ?? []}
        isLoading={invoicesQuery.isLoading}
        getRowKey={(i) => i.id}
        emptyMessage="Неоплаченных накладных нет"
      />

      <InvoiceDetailDialog
        invoiceId={detailId}
        onOpenChange={(open) => !open && setDetailId(null)}
      />

      <Dialog open={!!payTarget} onOpenChange={(open) => !open && setPayTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Оплата с ТК Черникова</DialogTitle>
          </DialogHeader>
          {payTarget ? (
            <div className="space-y-3">
              <p className="text-sm text-muted-foreground">
                {payTarget.counterparty_name} · №{payTarget.number ?? "—"} · остаток{" "}
                <span className="font-medium text-foreground">
                  {formatRub(payTarget.remaining || payTarget.amount)}
                </span>
              </p>
              <div className="grid gap-2">
                <Label>Сумма оплаты</Label>
                <Input
                  type="number"
                  value={amount}
                  onChange={(event) => setAmount(event.target.value)}
                />
                <p className="text-xs text-muted-foreground">
                  Спишется наличными с ТК Черникова и проведётся в iiko по поставщику.
                </p>
              </div>
            </div>
          ) : null}
          <DialogFooter>
            <Button variant="outline" onClick={() => setPayTarget(null)}>
              Отмена
            </Button>
            <Button
              disabled={payMutation.isPending || !amount}
              onClick={() =>
                payTarget &&
                payMutation.mutate({ id: payTarget.id, amt: amount ? Number(amount) : null })
              }
            >
              Оплатить
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
