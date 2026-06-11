import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Plus, Send } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import { apiErrorMessage } from "@/lib/api";

import {
  createDraft,
  createManualInvoice,
  getInvoices,
  getLedgerCategories,
  getRegistry,
  type CounterpartyInvoice,
} from "../api";
import {
  InvoiceStatusBadge,
  SOURCE_LABELS,
  formatDate,
  formatRub,
  formatVat,
  isOverdue,
} from "../shared";

const ALL = "all";

export function InboxTab({
  canOperate,
  onOpenCounterparty,
}: {
  canOperate: boolean;
  onOpenCounterparty: (id: string) => void;
}) {
  const queryClient = useQueryClient();
  const [categoryId, setCategoryId] = useState<string>(ALL);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [isManualOpen, setIsManualOpen] = useState(false);

  const categoriesQuery = useQuery({ queryKey: ["cp", "categories"], queryFn: getLedgerCategories });
  const invoicesQuery = useQuery({
    queryKey: ["cp", "invoices", categoryId],
    queryFn: () =>
      getInvoices({
        status: "unpaid,partially_paid",
        category_id: categoryId === ALL ? undefined : categoryId,
      }),
  });
  const invoices = invoicesQuery.data ?? [];

  const selectedInvoices = invoices.filter((item) => selected.has(item.id));
  const distinctCounterparties = new Set(selectedInvoices.map((item) => item.counterparty_id));
  const selectableForBank = selectedInvoices.filter((item) => !item.draft_id);
  const canSendToBank =
    canOperate &&
    selectableForBank.length > 0 &&
    selectableForBank.length === selectedInvoices.length &&
    distinctCounterparties.size === 1;

  const draftMutation = useMutation({
    mutationFn: () => createDraft(selectedInvoices.map((item) => item.id)),
    onSuccess: async (draft) => {
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      setSelected(new Set());
      toast.success(`Черновик на ${formatRub(draft.amount)} отправлен в банк`);
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось отправить в банк")),
  });

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }

  const columns: Array<DataTableColumn<CounterpartyInvoice>> = [
    {
      key: "select",
      header: "",
      className: "w-10",
      cell: (invoice) => (
        <Checkbox
          checked={selected.has(invoice.id)}
          onChange={() => toggle(invoice.id)}
          aria-label="Выбрать"
        />
      ),
    },
    {
      key: "counterparty",
      header: "Контрагент",
      className: "min-w-[200px]",
      cell: (invoice) => (
        <button
          className="text-left font-medium hover:underline"
          onClick={() => onOpenCounterparty(invoice.counterparty_id)}
          type="button"
        >
          {invoice.counterparty_name}
        </button>
      ),
    },
    { key: "number", header: "Счёт", cell: (invoice) => invoice.number ?? "—" },
    {
      key: "invoice_date",
      header: "Дата",
      cell: (invoice) => formatDate(invoice.invoice_date),
    },
    {
      key: "due_date",
      header: "Оплатить до",
      cell: (invoice) => {
        const overdue = isOverdue(invoice.due_date, invoice.payment_status);
        return (
          <span className={overdue ? "font-medium text-red-600" : undefined}>
            {formatDate(invoice.due_date)}
            {overdue ? " · просрочено" : ""}
          </span>
        );
      },
    },
    {
      key: "amount",
      header: "Сумма",
      className: "text-right tabular-nums",
      headerClassName: "text-right",
      cell: (invoice) => formatRub(invoice.remaining),
    },
    {
      key: "vat",
      header: "НДС",
      className: "text-xs text-muted-foreground",
      cell: (invoice) => formatVat(invoice.vat_breakdown),
    },
    {
      key: "source",
      header: "Источник",
      cell: (invoice) => (
        <Badge variant="outline">{SOURCE_LABELS[invoice.source] ?? invoice.source}</Badge>
      ),
    },
    {
      key: "status",
      header: "Статус",
      cell: (invoice) =>
        invoice.draft_id ? (
          <Badge className="border-sky-200 bg-sky-50 text-sky-700">В банке</Badge>
        ) : (
          <InvoiceStatusBadge status={invoice.payment_status} />
        ),
    },
  ];

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div className="grid w-full max-w-xs gap-2">
          <Label>Категория</Label>
          <Select value={categoryId} onValueChange={setCategoryId}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>Все категории</SelectItem>
              {(categoriesQuery.data ?? []).map((category) => (
                <SelectItem key={category.id} value={category.id}>
                  {category.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {canOperate ? (
            <Button onClick={() => setIsManualOpen(true)} variant="outline">
              <Plus size={16} aria-hidden="true" />
              Добавить вручную
            </Button>
          ) : null}
          {canOperate ? (
            <Button
              disabled={!canSendToBank || draftMutation.isPending}
              onClick={() => draftMutation.mutate()}
              title={
                selectedInvoices.length === 0
                  ? "Выберите накладные"
                  : distinctCounterparties.size > 1
                    ? "Можно отправить только накладные одного контрагента"
                    : undefined
              }
            >
              {draftMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <Send size={16} aria-hidden="true" />
              )}
              Отправить в банк{selectedInvoices.length ? ` (${selectedInvoices.length})` : ""}
            </Button>
          ) : null}
        </div>
      </div>

      {selected.size > 0 && distinctCounterparties.size > 1 ? (
        <p className="text-sm text-amber-600">
          В один черновик можно собрать только накладные одного контрагента.
        </p>
      ) : null}

      <DataTable
        columns={columns}
        rows={invoices}
        isLoading={invoicesQuery.isLoading}
        getRowKey={(invoice) => invoice.id}
        emptyMessage="Нет неоплаченных накладных"
      />

      <ManualInvoiceDialog open={isManualOpen} onOpenChange={setIsManualOpen} />
    </div>
  );
}

function ManualInvoiceDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [counterpartyId, setCounterpartyId] = useState("");
  const [amount, setAmount] = useState("");
  const [number, setNumber] = useState("");
  const [invoiceDate, setInvoiceDate] = useState("");
  const [vat10, setVat10] = useState("");
  const [vat22, setVat22] = useState("");

  const registryQuery = useQuery({
    queryKey: ["cp", "registry", "all"],
    queryFn: () => getRegistry(),
    enabled: open,
  });

  const createMutation = useMutation({
    mutationFn: () => {
      const vat: Record<string, number> = {};
      if (Number(vat10) > 0) vat["10"] = Number(vat10);
      if (Number(vat22) > 0) vat["22"] = Number(vat22);
      return createManualInvoice({
        counterparty_id: counterpartyId,
        amount: Number(amount),
        number: number || null,
        invoice_date: invoiceDate || null,
        vat_breakdown: Object.keys(vat).length ? vat : null,
      });
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      setAmount("");
      setNumber("");
      setInvoiceDate("");
      setVat10("");
      setVat22("");
      onOpenChange(false);
      toast.success("Накладная добавлена");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось добавить накладную")),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Накладная вручную</DialogTitle>
        </DialogHeader>
        <div className="grid gap-4">
          <div className="grid gap-2">
            <Label>Контрагент</Label>
            <Select value={counterpartyId} onValueChange={setCounterpartyId}>
              <SelectTrigger>
                <SelectValue placeholder="Выберите контрагента" />
              </SelectTrigger>
              <SelectContent>
                {(registryQuery.data ?? []).map((item) => (
                  <SelectItem key={item.counterparty_id} value={item.counterparty_id}>
                    {item.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div className="grid gap-2">
              <Label>Сумма, ₽</Label>
              <Input
                type="number"
                value={amount}
                onChange={(event) => setAmount(event.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <Label>Номер счёта</Label>
              <Input value={number} onChange={(event) => setNumber(event.target.value)} />
            </div>
          </div>
          <div className="grid grid-cols-3 gap-4">
            <div className="grid gap-2">
              <Label>Дата</Label>
              <Input
                type="date"
                value={invoiceDate}
                onChange={(event) => setInvoiceDate(event.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <Label>НДС 10%, ₽</Label>
              <Input type="number" value={vat10} onChange={(event) => setVat10(event.target.value)} />
            </div>
            <div className="grid gap-2">
              <Label>НДС 22%, ₽</Label>
              <Input type="number" value={vat22} onChange={(event) => setVat22(event.target.value)} />
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button
            disabled={!counterpartyId || !(Number(amount) > 0) || createMutation.isPending}
            onClick={() => createMutation.mutate()}
          >
            {createMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : null}
            Сохранить
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
