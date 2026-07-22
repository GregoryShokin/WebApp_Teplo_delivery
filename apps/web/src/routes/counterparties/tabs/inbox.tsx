import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Banknote,
  Check,
  CheckCircle2,
  Circle,
  CircleMinus,
  CircleX,
  Plus,
  ShieldAlert,
  Store,
} from "lucide-react";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
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
import { DateRangePopover } from "@/components/ui/date-range-popover";
import { MultiSelect } from "@/components/ui/multi-select";
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";

import {
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
import { PayInvoiceDialog } from "../PayInvoiceDialog";
import { BulkPayDialog } from "../BulkPayDialog";
import { CreateInvoiceDialog } from "../../warehouse/CreateInvoiceDialog";
import { InvoiceDetailDialog } from "../../warehouse/InvoiceDetailDialog";

const ALL = "all";

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
  operationalScope,
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
  // Складской экран использует общий богатый реестр, но обязан запрашивать только
  // товарный контур; финансовые УПД остаются в ДЗ/КЗ и сюда не попадают.
  operationalScope?: "warehouse" | "finance" | "unknown";
}) {
  const queryClient = useQueryClient();
  const [categoryId, setCategoryId] = useState<string>(ALL);
  const [relationship, setRelationship] = useState<string>(ALL);
  const [statusFilter, setStatusFilter] = useState<string>("unpaid,partially_paid");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [isManualOpen, setIsManualOpen] = useState(false);
  const [payTarget, setPayTarget] = useState<CounterpartyInvoice | null>(null);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [bulkPayOpen, setBulkPayOpen] = useState(false);
  // Накладная, чью ошибку пуша в iiko показываем (клик по красному статусу в колонке).
  const [pushErrorTarget, setPushErrorTarget] = useState<CounterpartyInvoice | null>(null);
  // Быстрые фильтры: период по дате накладной, мультивыбор поставщиков, «только не в iiko».
  const [dateFrom, setDateFrom] = useState<string | null>(null);
  const [dateTo, setDateTo] = useState<string | null>(null);
  const [supplierIds, setSupplierIds] = useState<string[]>([]);
  const [onlyNotIiko, setOnlyNotIiko] = useState(false);

  const isBarter = kind === "barter";
  const forcedRelationship = kind === "barter" ? "barter" : kind === "normal" ? "non_barter" : null;
  const effectiveRelationship = forcedRelationship ?? (relationship === ALL ? undefined : relationship);

  const categoriesQuery = useQuery({ queryKey: ["cp", "categories"], queryFn: getLedgerCategories });
  const registryQuery = useQuery({ queryKey: ["cp", "registry", "filter"], queryFn: () => getRegistry() });
  const supplierOptions = useMemo(
    () =>
      (registryQuery.data ?? [])
        .map((item) => ({ value: item.counterparty_id, label: item.name, keywords: item.inn ?? "" }))
        .sort((a, b) => a.label.localeCompare(b.label, "ru")),
    [registryQuery.data],
  );
  const supplierCsv = supplierIds.join(",");
  const invoicesQuery = useQuery({
    queryKey: [
      "cp",
      "invoices",
      categoryId,
      effectiveRelationship ?? "all",
      statusFilter,
      dateFrom ?? "",
      dateTo ?? "",
      supplierCsv,
      onlyNotIiko,
      operationalScope ?? "all",
    ],
    queryFn: () =>
      getInvoices({
        status: statusFilter,
        category_id: categoryId === ALL ? undefined : categoryId,
        relationship: effectiveRelationship,
        operational_scope: operationalScope,
        // Бартер двусторонний (займы нам + наши возвраты) — показываем обе стороны.
        direction: isBarter || relationship === "barter" ? "" : undefined,
        date_from: dateFrom ?? undefined,
        date_to: dateTo ?? undefined,
        counterparty_ids: supplierCsv || undefined,
        not_in_iiko: onlyNotIiko || undefined,
      }),
  });
  const invoices = invoicesQuery.data ?? [];
  const hasActiveFilters = !!dateFrom || !!dateTo || supplierIds.length > 0 || onlyNotIiko;
  function resetFilters() {
    setDateFrom(null);
    setDateTo(null);
    setSupplierIds([]);
    setOnlyNotIiko(false);
    setSelected(new Set());
  }

  const selectedInvoices = invoices.filter((item) => selected.has(item.id));
  // К оплате пригодны выбранные без банковского черновика и не оплаченные; способ
  // (банк-черновик Т-Банк / наличные Сейф·ТК) выбирается в модалке «Оплатить».
  const selectedPayable = selectedInvoices.filter(
    (item) => !item.draft_id && item.payment_status !== "paid",
  );

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
        // Стоп всплытия: клик по чекбоксу выбирает накладную, а не открывает детали.
        <div onClick={(event) => event.stopPropagation()}>
          <Checkbox
            checked={selected.has(invoice.id)}
            onChange={() => toggle(invoice.id)}
            aria-label="Выбрать"
          />
        </div>
      ),
    },
    {
      key: "counterparty",
      header: "Контрагент",
      className: "min-w-[200px]",
      cell: (invoice) => (
        <button
          className="text-left font-medium hover:underline"
          onClick={(event) => {
            event.stopPropagation();
            onOpenCounterparty(invoice.counterparty_id);
          }}
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
      cell: (invoice) => {
        // Контроль цен приоритетнее статуса оплаты: пока цены подозрительны и не подтверждены,
        // оплата и отправка в банк заблокированы → в статусе показываем только «Проверить цены».
        // После подтверждения статус становится обычным («Не оплачено» и т. д.).
        if (invoice.price_control_status === "flagged") {
          return (
            <Badge className="gap-1 border-amber-300 bg-amber-100 text-amber-800">
              <ShieldAlert size={12} aria-hidden="true" /> Проверить цены
            </Badge>
          );
        }
        // Пока накладная не оплачена, статус ведёт привязанный черновик. Оплаченный статус
        // приоритетнее: после гашения draft_id остаётся, но показываем «Оплачено».
        if (invoice.draft_id && invoice.payment_status !== "paid") {
          // Неофициал: платёж исполнен, деньги дошли до Сейфа, но наличные поставщику ещё
          // не выданы — менеджеру видно, что деньги нужно отвезти контрагенту.
          if (invoice.draft_pays_via_safe && invoice.draft_status === "paid") {
            return (
              <Badge className="border-violet-200 bg-violet-50 text-violet-700">
                Деньги в Сейфе
              </Badge>
            );
          }
          return (
            <Badge className="border-sky-200 bg-sky-50 text-sky-700">Отправлено в банк</Badge>
          );
        }
        return (
          <InvoiceStatusBadge
            status={invoice.payment_status}
            direction={invoice.direction}
            barterSettled={!!invoice.barter_settlement_id}
            barterRole={invoice.barter_role}
          />
        );
      },
    },
    {
      key: "iiko",
      header: "Отправлено в iiko",
      className: "text-center",
      headerClassName: "text-center",
      cell: (invoice) => {
        if (invoice.iiko_push_status === "failed") {
          // create мог вернуть external_id, а следующий post — упасть. Такой черновик ещё не
          // считается отправленной накладной: ошибка важнее наличия технического documentId.
          return (
            <button
              type="button"
              className="inline-flex"
              onClick={(event) => {
                event.stopPropagation();
                setPushErrorTarget(invoice);
              }}
              title="Ошибка отправки в iiko — нажмите для деталей"
            >
              <CircleX size={18} className="text-red-600" aria-label="Ошибка отправки в iiko" />
            </button>
          );
        }
        // Зелёная галочка — только подтверждённо проведённая накладная: пришла из iiko либо
        // наш create+post завершился статусом pushed. Один external_id этого не доказывает.
        const inIiko = invoice.source === "iiko" || invoice.iiko_push_status === "pushed";
        if (inIiko) {
          return (
            <CheckCircle2
              size={18}
              className="inline-block text-emerald-600"
              aria-label="Отправлено в iiko"
            />
          );
        }
        if (invoice.iiko_push_status === "skipped") {
          return (
            <CircleMinus
              size={18}
              className="inline-block text-slate-400"
              aria-label="Не отправляется в iiko (нет товарных строк)"
            />
          );
        }
        return (
          <Circle
            size={18}
            className="inline-block text-slate-300"
            aria-label="Не отправлена в iiko"
          />
        );
      },
    },
  ];
  if (!splitPay) {
    // Бартер-инбокс: оплата не сплитовая, живёт строчной кнопкой (клик не всплывает на строку).
    columns.push({
      key: "actions",
      header: "",
      className: "text-right",
      cell: (invoice) =>
        canPay && !invoice.draft_id && invoice.payment_status !== "paid" ? (
          <Button
            size="sm"
            variant="outline"
            onClick={(event) => {
              event.stopPropagation();
              setPayTarget(invoice);
            }}
          >
            <Banknote size={15} aria-hidden="true" />
            Оплатить
          </Button>
        ) : null,
    });
  }

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

      <div className="flex flex-wrap items-center gap-2 rounded-md border bg-muted/30 p-2">
        <DateRangePopover
          from={dateFrom}
          to={dateTo}
          onChange={(nextFrom, nextTo) => {
            setDateFrom(nextFrom);
            setDateTo(nextTo);
            setSelected(new Set());
          }}
        />
        <MultiSelect
          options={supplierOptions}
          selected={supplierIds}
          onChange={(next) => {
            setSupplierIds(next);
            setSelected(new Set());
          }}
          placeholder="Все поставщики"
          searchPlaceholder="Поиск поставщика…"
          emptyMessage="Поставщики не найдены"
          icon={<Store className="h-4 w-4" aria-hidden="true" />}
        />
        {!isBarter ? (
          <Button
            type="button"
            variant={onlyNotIiko ? "secondary" : "outline"}
            className="h-9"
            aria-pressed={onlyNotIiko}
            onClick={() => {
              setOnlyNotIiko((value) => !value);
              setSelected(new Set());
            }}
          >
            <Check size={15} aria-hidden="true" />
            Только не в iiko
          </Button>
        ) : null}
        {hasActiveFilters ? (
          <Button type="button" variant="ghost" className="h-9" onClick={resetFilters}>
            Сбросить
          </Button>
        ) : null}
        <span className="ml-auto text-xs text-muted-foreground">
          {invoicesQuery.isLoading ? "Загрузка…" : `Показано: ${invoices.length}`}
        </span>
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
          {canPay && !isBarter ? (
            <Button
              disabled={selectedPayable.length === 0}
              onClick={() => setBulkPayOpen(true)}
              title={
                selectedInvoices.length === 0
                  ? "Выберите накладные"
                  : selectedPayable.length === 0
                    ? "Выбранные уже оплачены или отправлены в банк"
                    : undefined
              }
            >
              <Banknote size={16} aria-hidden="true" />
              Оплатить{selectedPayable.length ? ` (${selectedPayable.length})` : ""}
            </Button>
          ) : null}
        </div>
      </div>

      <DataTable
        columns={columns}
        rows={invoices}
        isLoading={invoicesQuery.isLoading}
        getRowKey={(invoice) => invoice.id}
        onRowClick={(invoice) => setDetailId(invoice.id)}
        // Контроль цен: подозрительную накладную (flagged) подсвечиваем жёлто-оранжевым прямо
        // в списке — видно без захода внутрь, что цены надо проверить и подтвердить.
        rowClassName={(invoice) =>
          invoice.price_control_status === "flagged"
            ? "bg-amber-100 hover:bg-amber-200"
            : undefined
        }
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
      <InvoiceDetailDialog
        invoiceId={detailId}
        onOpenChange={(open) => !open && setDetailId(null)}
      />
      {!splitPay ? (
        <PayInvoiceDialog
          invoice={payTarget}
          onOpenChange={(open) => !open && setPayTarget(null)}
        />
      ) : null}
      <BulkPayDialog
        invoices={selectedInvoices}
        open={bulkPayOpen}
        onOpenChange={setBulkPayOpen}
        onDone={() => setSelected(new Set())}
      />
      <AlertDialog
        open={Boolean(pushErrorTarget)}
        onOpenChange={(open) => !open && setPushErrorTarget(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              Ошибка отправки в iiko — №{pushErrorTarget?.number ?? "—"}
            </AlertDialogTitle>
            <AlertDialogDescription className="whitespace-pre-wrap">
              {pushErrorTarget?.iiko_push_error ??
                "iiko не принял документ. Откройте детали накладной и переотправьте."}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogAction onClick={() => setPushErrorTarget(null)}>Понятно</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
