import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Banknote, LoaderCircle, Plus, Send } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
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
  getInvoices,
  getLedgerCategories,
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
import { PayInvoiceDialog } from "../PayInvoiceDialog";
import { CreateInvoiceDialog } from "../../warehouse/CreateInvoiceDialog";
import { PayWarehouseInvoiceDialog } from "../../warehouse/PayWarehouseInvoiceDialog";
import { pushInvoiceToIiko } from "../../warehouse/api";

const ALL = "all";

const PUSH_BADGE: Record<string, { label: string; cls: string }> = {
  pushed: { label: "в iiko", cls: "border-emerald-200 bg-emerald-50 text-emerald-700" },
  failed: { label: "ошибка iiko", cls: "border-red-200 bg-red-50 text-red-700" },
  skipped: { label: "без iiko", cls: "border-amber-200 bg-amber-50 text-amber-700" },
};

const REL_SEGMENTS: Array<{ value: string; label: string }> = [
  { value: "all", label: "Все" },
  { value: "official", label: "Поставщики" },
  { value: "informal", label: "Неофициальные" },
  { value: "barter", label: "Бартер" },
];

const STATUS_SEGMENTS: Array<{ value: string; label: string }> = [
  { value: "unpaid,partially_paid", label: "К оплате" },
  { value: "paid", label: "Оплаченные" },
  { value: "", label: "Все статусы" },
];

export function InboxTab({
  canOperate,
  canCreate,
  canPay,
  onOpenCounterparty,
  kind,
  splitPay,
}: {
  // canOperate — вспомогательные операции (пуш в iiko). Создание и оплата разведены
  // на отдельные права (canCreate/canPay), чтобы доступ к обычным и бартерным
  // накладным настраивался независимо — права пробрасывает страница по типу инбокса.
  canOperate: boolean;
  canCreate: boolean;
  canPay: boolean;
  onOpenCounterparty: (id: string) => void;
  // Два инбокса: "barter" (только бартер, обе стороны, создание займа) /
  // "normal" (всё кроме бартера). Без него — старое поведение с сегментами.
  kind?: "normal" | "barter";
  // splitPay: «Оплатить» открывает диалог сплит-оплаты с банк-подсказками + персонал-разнесением
  // (Фаза 4). Без него — простой диалог оплаты со счёта.
  splitPay?: boolean;
}) {
  const queryClient = useQueryClient();
  const [categoryId, setCategoryId] = useState<string>(ALL);
  const [relationship, setRelationship] = useState<string>(ALL);
  const [statusFilter, setStatusFilter] = useState<string>("unpaid,partially_paid");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [isManualOpen, setIsManualOpen] = useState(false);
  const [payTarget, setPayTarget] = useState<CounterpartyInvoice | null>(null);

  const isBarter = kind === "barter";
  const forcedRelationship = kind === "barter" ? "barter" : kind === "normal" ? "non_barter" : null;
  const effectiveRelationship = forcedRelationship ?? (relationship === ALL ? undefined : relationship);

  const categoriesQuery = useQuery({ queryKey: ["cp", "categories"], queryFn: getLedgerCategories });
  const invoicesQuery = useQuery({
    queryKey: ["cp", "invoices", categoryId, effectiveRelationship ?? "all", statusFilter],
    queryFn: () =>
      getInvoices({
        status: statusFilter,
        category_id: categoryId === ALL ? undefined : categoryId,
        relationship: effectiveRelationship,
        // Бартер двусторонний (займы нам + наши возвраты) — показываем обе стороны.
        direction: isBarter || relationship === "barter" ? "" : undefined,
      }),
  });
  const invoices = invoicesQuery.data ?? [];

  const selectedInvoices = invoices.filter((item) => selected.has(item.id));
  const distinctCounterparties = new Set(selectedInvoices.map((item) => item.counterparty_id));
  const selectableForBank = selectedInvoices.filter((item) => !item.draft_id);
  const canSendToBank =
    canPay &&
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

  const pushMutation = useMutation({
    mutationFn: (id: string) => pushInvoiceToIiko(id),
    onSuccess: async (invoice) => {
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      if (invoice.iiko_push_status === "pushed") {
        toast.success("Отправлено в iiko");
      } else if (invoice.iiko_push_status === "skipped") {
        toast.info(`Не отправлено: ${invoice.iiko_push_error ?? "нет товарных строк"}`);
      } else {
        toast.error(`Ошибка iiko: ${invoice.iiko_push_error ?? ""}`);
      }
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось отправить в iiko")),
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
      // Остаток к оплате; для оплаченных он 0 — показываем полную сумму накладной.
      cell: (invoice) => formatRub(invoice.remaining || invoice.amount),
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
          <Badge className="border-sky-200 bg-sky-50 text-sky-700">Отправлено в банк</Badge>
        ) : (
          <InvoiceStatusBadge
            status={invoice.payment_status}
            direction={invoice.direction}
            barterSettled={!!invoice.barter_settlement_id}
            barterRole={invoice.barter_role}
          />
        ),
    },
    {
      key: "actions",
      header: "",
      className: "text-right",
      cell: (invoice) => (
        <div className="flex items-center justify-end gap-2">
          {invoice.source === "manual" && canOperate ? (
            invoice.iiko_push_status === "pushed" || invoice.iiko_push_status === "skipped" ? (
              <Badge className={PUSH_BADGE[invoice.iiko_push_status].cls}>
                {PUSH_BADGE[invoice.iiko_push_status].label}
              </Badge>
            ) : (
              <Button
                size="sm"
                variant="ghost"
                disabled={pushMutation.isPending}
                title="Создать документ в iiko"
                onClick={() => {
                  if (
                    window.confirm(
                      "Отправить накладную в iiko? Будет создан реальный документ.",
                    )
                  ) {
                    pushMutation.mutate(invoice.id);
                  }
                }}
              >
                <Send size={15} aria-hidden="true" />
                {invoice.iiko_push_status === "failed" ? "Повторить" : "В iiko"}
              </Button>
            )
          ) : null}
          {canPay && !invoice.draft_id && invoice.payment_status !== "paid" ? (
            <Button size="sm" variant="outline" onClick={() => setPayTarget(invoice)}>
              <Banknote size={15} aria-hidden="true" />
              Оплатить
            </Button>
          ) : null}
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      {!kind ? (
        <div className="flex flex-wrap gap-1">
          {REL_SEGMENTS.map((segment) => (
            <Button
              key={segment.value}
              size="sm"
              variant={relationship === segment.value ? "default" : "outline"}
              onClick={() => setRelationship(segment.value)}
            >
              {segment.label}
            </Button>
          ))}
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-1">
        <span className="mr-1 text-xs text-muted-foreground">Статус:</span>
        {STATUS_SEGMENTS.map((segment) => (
          <Button
            key={segment.value || "all"}
            size="sm"
            variant={statusFilter === segment.value ? "secondary" : "ghost"}
            onClick={() => {
              setStatusFilter(segment.value);
              setSelected(new Set());
            }}
          >
            {segment.label}
          </Button>
        ))}
      </div>

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
          {canCreate ? (
            <Button onClick={() => setIsManualOpen(true)} variant="outline">
              <Plus size={16} aria-hidden="true" />
              Добавить вручную
            </Button>
          ) : null}
          {canPay ? (
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
        emptyMessage={
          statusFilter === "paid"
            ? "Нет оплаченных накладных"
            : statusFilter === ""
              ? "Накладных нет"
              : "Нет неоплаченных накладных"
        }
      />

      <CreateInvoiceDialog
        open={isManualOpen}
        onOpenChange={setIsManualOpen}
        barter={isBarter}
        onCreated={() => queryClient.invalidateQueries({ queryKey: ["cp"] })}
      />
      {splitPay ? (
        <PayWarehouseInvoiceDialog
          invoiceId={payTarget?.id ?? null}
          onOpenChange={(open) => !open && setPayTarget(null)}
        />
      ) : (
        <PayInvoiceDialog
          invoice={payTarget}
          onOpenChange={(open) => !open && setPayTarget(null)}
        />
      )}
    </div>
  );
}
