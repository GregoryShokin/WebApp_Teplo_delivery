import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowRight, Loader2, Pencil, X } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { api, apiErrorMessage } from "@/lib/api";
import { todayIso } from "@/lib/date";
import { usePermissions } from "@/lib/permissions";
import { navigateTo } from "@/router";

type View = "open" | "all" | "needs_review" | "recognized";
type Section = "balances" | "payments" | "documents" | "recognition";

type AccountingItem = {
  id: string;
  source_kind: "service_period" | "legacy_prepayment";
  counterparty_id: string;
  counterparty_name: string;
  article_id: string | null;
  article_name: string | null;
  invoice_id: string | null;
  invoice_number: string | null;
  amount: number;
  paid_amount: number;
  balance_amount: number;
  balance_type: "receivable" | "payable" | "scheduled" | "closed" | "needs_review";
  service_period_start: string | null;
  service_period_end: string | null;
  period_status: string;
  recognition_month: string | null;
  recognized: boolean;
};

type AccountingList = {
  items: AccountingItem[];
  receivable_total: number;
  payable_total: number;
  scheduled_total: number;
  needs_review_total: number;
};

type CounterpartyBalance = {
  counterparty_id: string;
  name: string;
  inn: string | null;
  receivable: number;
  payable: number;
  net: number;
  open_prepayments: number;
  unpaid_invoices: number;
  last_activity: string | null;
};

type BalanceList = {
  items: CounterpartyBalance[];
  receivable_total: number;
  payable_total: number;
};

type SettledInvoiceRef = {
  invoice_id: string;
  number: string | null;
  invoice_date: string | null;
  amount: number;
};

type PaymentPrepaymentInfo = {
  id: string;
  kind: string;
  status: string;
  amount: number;
  amount_settled: number;
  settled_invoices: SettledInvoiceRef[];
};

type PaymentRow = {
  id: string;
  row_kind: "transaction" | "opening_prepayment";
  operation_date: string;
  amount: number;
  counterparty_id: string;
  counterparty_name: string;
  wallet_name: string | null;
  article_name: string | null;
  purpose: string | null;
  settled_invoices: SettledInvoiceRef[];
  prepayment: PaymentPrepaymentInfo | null;
  unassigned_amount: number;
};

type PaymentList = { items: PaymentRow[]; total_amount: number };

type DocumentAllocationRef = {
  source_kind: string;
  amount: number;
  operation_date: string | null;
  prepayment_kind: string | null;
};

type DocumentRow = {
  invoice_id: string;
  number: string | null;
  invoice_date: string | null;
  source: string;
  doc_kind: string;
  // 'active' — документ в силе (в КЗ); 'pending' — будущий УПД, ждёт своей даты (правило 4).
  activation_status: string;
  counterparty_id: string;
  counterparty_name: string;
  amount: number;
  payment_status: string;
  remainder: number;
  service_period_start: string | null;
  service_period_end: string | null;
  allocations: DocumentAllocationRef[];
};

type DocumentList = { items: DocumentRow[]; total_amount: number; unpaid_total: number };

type RegisterFilters = {
  date_from: string;
  date_to: string;
  counterparty_id: string | null;
  counterparty_name: string | null;
};

const money = new Intl.NumberFormat("ru-RU", {
  style: "currency",
  currency: "RUB",
  maximumFractionDigits: 0,
});

const moneyExact = new Intl.NumberFormat("ru-RU", {
  style: "currency",
  currency: "RUB",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const date = new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" });

const INVOICE_SOURCE_LABEL: Record<string, string> = {
  iiko: "iiko",
  manual: "вручную",
  email: "почта",
  telegram: "Telegram",
  kassa_cheque: "чек Кассы",
  kassa_invoice: "Касса",
  sbis: "СБИС",
};

const ALLOCATION_LABEL: Record<string, string> = {
  bank: "банк",
  cash: "касса",
  card_pending: "карта (ждёт банк)",
  prepayment: "из предоплаты",
};

const PREPAYMENT_KIND_LABEL: Record<string, string> = {
  goods: "товарный аванс",
  subscription: "подписка/ЛК",
  ad: "рекламный кабинет",
  rent: "аренда",
  other: "прочее",
};

const PAYMENT_STATUS_LABEL: Record<string, { label: string; className: string }> = {
  unpaid: { label: "Не оплачен", className: "border-rose-200 bg-rose-50 text-rose-700" },
  partially_paid: { label: "Частично", className: "border-amber-200 bg-amber-50 text-amber-800" },
  paid: { label: "Оплачен", className: "border-emerald-200 bg-emerald-50 text-emerald-700" },
};

function fmtDate(value: string | null): string {
  if (!value) return "—";
  return date.format(new Date(`${value}T00:00:00`));
}

type StaffPayable = {
  as_of: string;
  total: number;
  receivable_total: number;
  salary_total: number;
  fund_total: number;
  fund_current_year_total: number;
  fund_prior_years_total: number;
  production_deposit_total: number;
  courier_deposit_total: number;
  deposit_total: number;
  items: {
    employee_id: string;
    full_name: string;
    position: string | null;
    staff_group: "staff" | "courier";
    basis: string;
    earned_to_date: number;
    on_demand_accrued: number;
    on_demand_paid: number;
    on_demand_debt: number;
    already_advanced: number;
    advances_outstanding: number;
    finalized_unpaid: number;
    loans_outstanding: number;
    salary_payouts_outstanding: number;
    salary_payable: number;
    fund_payable: number;
    fund_current_year_payable: number;
    fund_prior_years_payable: number;
    production_deposit_payable: number;
    courier_deposit_payable: number;
    deposit_payable: number;
    payable: number;
    receivable: number;
  }[];
};

const STAFF_BASIS_LABEL: Record<string, string> = {
  okladnik: "оклад",
  on_demand: "по востребованию",
  dishwasher: "смены",
  courier_deposit: "курьерский депозит",
  none: "прочие расчёты",
  production: "выработка",
};

// Расчёты с бюджетом: те же данные, что наполняют «Активные платежи» и кнопки на
// «Налогах» — одна цифра в трёх местах по построению. Черновик «в банке» долг НЕ гасит
// (гасит только факт списания из выписки), но его статус владельцу виден.
type TaxDebt = {
  as_of: string;
  payable_total: number;
  items: {
    kind: string;
    title: string;
    for_year: number | null;
    for_period: string | null;
    amount: number;
    due_date: string | null;
    draft_status: string | null;
  }[];
  // Расчётный ЕНС-кошелёк: переплата в бюджет считается из фактов и начислений,
  // а не вводится руками (решение владельца 26.07.2026) — поэтому не протухает.
  wallet: {
    as_of: string;
    inflow: number;
    recognized: number;
    balance: number;
    shortfall: number;
  };
};

const TAX_DRAFT_STATUS_LABEL: Record<string, string> = {
  ready_to_send: "подготовлен к отправке",
  in_bank: "отправлен в банк",
};

async function getAccounting(view: View): Promise<AccountingList> {
  const response = await api.get<AccountingList>("/accounting/suppliers", { params: { view } });
  return response.data;
}

async function getStaffPayable(): Promise<StaffPayable> {
  const response = await api.get<StaffPayable>("/accounting/suppliers/staff-payable");
  return response.data;
}

async function getTaxDebt(): Promise<TaxDebt> {
  const response = await api.get<TaxDebt>("/taxes/debt");
  return response.data;
}

async function getBalances(): Promise<BalanceList> {
  const response = await api.get<BalanceList>("/accounting/suppliers/balances");
  return response.data;
}

function registerParams(filters: RegisterFilters) {
  return {
    date_from: filters.date_from || undefined,
    date_to: filters.date_to || undefined,
    counterparty_id: filters.counterparty_id ?? undefined,
  };
}

async function getPayments(filters: RegisterFilters): Promise<PaymentList> {
  const response = await api.get<PaymentList>("/accounting/suppliers/payments", {
    params: registerParams(filters),
  });
  return response.data;
}

async function getDocuments(filters: RegisterFilters): Promise<DocumentList> {
  const response = await api.get<DocumentList>("/accounting/suppliers/documents", {
    params: registerParams(filters),
  });
  return response.data;
}

async function updatePeriod(
  id: string,
  payload: { service_period_start: string; service_period_end: string; reason?: string | null },
): Promise<AccountingItem> {
  const response = await api.patch<AccountingItem>(
    `/accounting/suppliers/service-periods/${id}`,
    payload,
  );
  return response.data;
}

const STATUS: Record<AccountingItem["balance_type"], { label: string; className: string }> = {
  receivable: { label: "Дебиторка", className: "border-sky-200 bg-sky-50 text-sky-700" },
  payable: { label: "Кредиторка", className: "border-rose-200 bg-rose-50 text-rose-700" },
  scheduled: { label: "Будущий расход", className: "border-violet-200 bg-violet-50 text-violet-700" },
  closed: { label: "Закрыто", className: "border-emerald-200 bg-emerald-50 text-emerald-700" },
  needs_review: { label: "Нужно распределить", className: "border-amber-200 bg-amber-50 text-amber-800" },
};

function formatPeriod(start: string | null, end: string | null) {
  if (!start || !end) return "Период не указан";
  return `${fmtDate(start)} — ${fmtDate(end)}`;
}

const DEFAULT_DATE_FROM = "2026-06-01";

export function DzKzRoute() {
  const permissions = usePermissions();
  const [section, setSection] = useState<Section>("balances");
  const [filters, setFilters] = useState<RegisterFilters>({
    date_from: DEFAULT_DATE_FROM,
    date_to: "",
    counterparty_id: null,
    counterparty_name: null,
  });

  const accounting = useQuery({
    queryKey: ["accounting", "suppliers", "open"],
    queryFn: () => getAccounting("open"),
  });
  const balances = useQuery({ queryKey: ["accounting", "balances"], queryFn: getBalances });
  // Долг сотрудникам — провизорный прогон калькулятора ЗП, тяжёлый: грузим отдельно и кэшируем.
  const staff = useQuery({
    queryKey: ["accounting", "staff-payable"],
    queryFn: getStaffPayable,
    staleTime: 5 * 60 * 1000,
  });
  // Задолженность по налогам видна только тем, кому виден модуль «Налоги»:
  // без права запрос не шлём вовсе, страница работает как раньше.
  const canSeeTaxes =
    permissions.hasPermission("accounting.taxes.read") ||
    permissions.hasPermission("accounting.taxes.manage");
  const taxes = useQuery({
    queryKey: ["accounting", "tax-debt"],
    queryFn: getTaxDebt,
    enabled: canSeeTaxes,
    staleTime: 5 * 60 * 1000,
  });

  const supplierPayable = balances.data?.payable_total;
  const staffPayable = staff.data
    ? staff.data.items
        .filter((item) => item.staff_group === "staff")
        .reduce((sum, item) => sum + item.payable, 0)
    : undefined;
  const courierPayable = staff.data
    ? staff.data.items
        .filter((item) => item.staff_group === "courier")
        .reduce((sum, item) => sum + item.payable, 0)
    : undefined;
  const peoplePayable =
    staffPayable == null && courierPayable == null
      ? undefined
      : (staffPayable ?? 0) + (courierPayable ?? 0);
  // Налоги — третий источник кредиторской: неоплаченные обязательства налогового контура.
  const taxPayable = canSeeTaxes ? taxes.data?.payable_total : null;
  const payableTotal =
    supplierPayable == null && peoplePayable == null && taxPayable == null
      ? undefined
      : (supplierPayable ?? 0) + (peoplePayable ?? 0) + (taxPayable ?? 0);
  const supplierReceivable = balances.data?.receivable_total;
  const staffReceivable = staff.data
    ? staff.data.items
        .filter((item) => item.staff_group === "staff")
        .reduce((sum, item) => sum + item.receivable, 0)
    : undefined;
  const courierReceivable = staff.data
    ? staff.data.items
        .filter((item) => item.staff_group === "courier")
        .reduce((sum, item) => sum + item.receivable, 0)
    : undefined;
  const peopleReceivable =
    staffReceivable == null && courierReceivable == null
      ? undefined
      : (staffReceivable ?? 0) + (courierReceivable ?? 0);
  // ДЗ бюджета — расчётная переплата на ЕНС (кошелёк налогового контура).
  const taxReceivable = canSeeTaxes ? taxes.data?.wallet.balance : null;
  const receivableTotal =
    supplierReceivable == null && peopleReceivable == null && taxReceivable == null
      ? undefined
      : (supplierReceivable ?? 0) + (peopleReceivable ?? 0) + (taxReceivable ?? 0);

  const openRegister = (target: "payments" | "documents", cp: CounterpartyBalance | null) => {
    setFilters((prev) => ({
      ...prev,
      counterparty_id: cp?.counterparty_id ?? null,
      counterparty_name: cp?.name ?? null,
    }));
    setSection(target);
  };

  return (
    <div className="mx-auto flex max-w-7xl flex-col gap-5 p-6">
      <div>
        <h1 className="text-2xl font-semibold">Учёт ДЗ/КЗ</h1>
        <p className="text-sm text-muted-foreground">
          Остатки взаиморасчётов с поставщиками, реестры платежей и УПД, признание расходов по
          периодам услуг.
        </p>
      </div>

      {/* Только чистые ДЗ/КЗ. Контур признания (будущие расходы, очередь распределения) —
          детали на вкладке «Признание расходов», не headline-числа: те же предоплаты уже
          посчитаны в дебиторке, отдельные плитки их дублировали (решение владельца 17.07). */}
      <div className="grid gap-3 sm:grid-cols-2">
        <Summary
          title="Дебиторская задолженность"
          value={receivableTotal}
          tone="sky"
          hint={
            supplierReceivable != null &&
            peopleReceivable != null &&
            (peopleReceivable > 0 || (taxReceivable ?? 0) > 0)
              ? [
                  `поставщики ${money.format(supplierReceivable)}`,
                  (staffReceivable ?? 0) > 0
                    ? `сотрудники ${money.format(staffReceivable ?? 0)}`
                    : null,
                  (courierReceivable ?? 0) > 0
                    ? `курьеры ${money.format(courierReceivable ?? 0)}`
                    : null,
                  (taxReceivable ?? 0) > 0 ? `бюджет ${money.format(taxReceivable ?? 0)}` : null,
                ]
                  .filter(Boolean)
                  .join(" · ")
              : "Открытые предоплаты: нам должны закрыть документами или вернуть"
          }
        />
        <Summary
          title="Кредиторская задолженность"
          value={payableTotal}
          tone="rose"
          hint={
            supplierPayable != null && peoplePayable != null
              ? [
                  `поставщики ${money.format(supplierPayable)}`,
                  `сотрудники ${money.format(staffPayable ?? 0)}`,
                  `курьеры ${money.format(courierPayable ?? 0)}`,
                  taxPayable != null ? `налоги ${money.format(taxPayable)}` : null,
                ]
                  .filter(Boolean)
                  .join(" · ")
              : "Накладные и акты к оплате + заработанное сотрудниками + налоги"
          }
        />
      </div>

      <Tabs value={section} onValueChange={(value) => setSection(value as Section)}>
        <TabsList>
          <TabsTrigger value="balances">Остатки</TabsTrigger>
          <TabsTrigger value="payments">Платежи</TabsTrigger>
          <TabsTrigger value="documents">УПД и накладные</TabsTrigger>
          <TabsTrigger value="recognition">
            Признание расходов
            {(accounting.data?.needs_review_total ?? 0) > 0 ? (
              <span className="ml-1.5 rounded-full bg-amber-100 px-1.5 text-xs text-amber-800">
                {money.format(accounting.data?.needs_review_total ?? 0)}
              </span>
            ) : null}
          </TabsTrigger>
        </TabsList>
      </Tabs>

      {section === "balances" ? (
        <BalancesSection
          query={balances}
          staffQuery={staff}
          taxQuery={canSeeTaxes ? taxes : null}
          onOpenRegister={openRegister}
        />
      ) : null}
      {section === "payments" || section === "documents" ? (
        <RegisterFiltersBar filters={filters} onChange={setFilters} />
      ) : null}
      {section === "payments" ? <PaymentsSection filters={filters} /> : null}
      {section === "documents" ? <DocumentsSection filters={filters} /> : null}
      {section === "recognition" ? (
        <RecognitionSection
          canEdit={permissions.hasPermission("accounting.suppliers.edit")}
          canCorrectRecognized={permissions.hasPermission(
            "accounting.service_periods.correct_recognized",
          )}
        />
      ) : null}
    </div>
  );
}

function Summary({
  title,
  value,
  tone,
  hint,
}: {
  title: string;
  value?: number;
  tone: "sky" | "rose" | "violet" | "amber";
  hint?: string;
}) {
  const tones = {
    sky: "text-sky-700",
    rose: "text-rose-700",
    violet: "text-violet-700",
    amber: "text-amber-700",
  };
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">{title}</CardTitle>
      </CardHeader>
      <CardContent>
        <div className={`text-2xl font-semibold tabular-nums ${tones[tone]}`}>
          {value == null ? "—" : money.format(value)}
        </div>
        {hint ? <p className="mt-1 text-xs text-muted-foreground">{hint}</p> : null}
      </CardContent>
    </Card>
  );
}

function TableStatus({ colSpan, state }: { colSpan: number; state: "loading" | "empty" | string }) {
  return (
    <TableRow>
      <TableCell
        colSpan={colSpan}
        className={`py-12 text-center ${state === "loading" || state === "empty" ? "text-muted-foreground" : "text-red-600"}`}
      >
        {state === "loading" ? (
          <>
            <Loader2 className="mr-2 inline animate-spin" size={16} /> Загрузка…
          </>
        ) : state === "empty" ? (
          "Записей нет."
        ) : (
          state
        )}
      </TableCell>
    </TableRow>
  );
}

function StaffPayableCard({
  query,
  group,
}: {
  query: ReturnType<typeof useQuery<StaffPayable>>;
  group: "staff" | "courier";
}) {
  const [open, setOpen] = useState(false);
  const isCourier = group === "courier";
  const items = query.data?.items.filter((row) => row.staff_group === group) ?? [];
  const totals = items.reduce(
    (sum, row) => ({
      payable: sum.payable + row.payable,
      receivable: sum.receivable + row.receivable,
      salary: sum.salary + row.salary_payable,
      fund: sum.fund + row.fund_payable,
      fundCurrentYear: sum.fundCurrentYear + row.fund_current_year_payable,
      fundPriorYears: sum.fundPriorYears + row.fund_prior_years_payable,
      productionDeposit: sum.productionDeposit + row.production_deposit_payable,
      courierDeposit: sum.courierDeposit + row.courier_deposit_payable,
    }),
    {
      payable: 0,
      receivable: 0,
      salary: 0,
      fund: 0,
      fundCurrentYear: 0,
      fundPriorYears: 0,
      productionDeposit: 0,
      courierDeposit: 0,
    },
  );
  return (
    <div className="rounded-lg border bg-background">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left"
      >
        <div>
          <div className="font-medium">
            {isCourier ? "Задолженность перед курьерами" : "Сотрудники — баланс расчётов"}
          </div>
          <div className="text-xs text-muted-foreground">
            {isCourier
              ? "Депозиты обычных курьеров. Старший курьер учитывается вместе с производственным и административным персоналом."
              : "Производственный и административный персонал, старший курьер, мойщицы и уборщицы. Зарплата, фонд и депозиты показаны отдельно."}
          </div>
          {query.data ? (
            <div className="mt-1 text-xs text-muted-foreground">
              {isCourier ? (
                <>депозиты курьеров {money.format(totals.courierDeposit)}</>
              ) : (
                <>
                  зарплата {money.format(totals.salary)} · фонд {money.format(totals.fund)} (
                  {new Date(query.data.as_of).getFullYear()}: {money.format(totals.fundCurrentYear)} ·{" "}
                  прошлые годы: {money.format(totals.fundPriorYears)}) · депозиты производства{" "}
                  {money.format(totals.productionDeposit)}
                  {totals.courierDeposit > 0
                    ? ` · депозит старшего курьера ${money.format(totals.courierDeposit)}`
                    : null}
                </>
              )}
            </div>
          ) : null}
        </div>
        <div className="flex items-center gap-3 text-lg font-semibold tabular-nums">
          {query.isLoading ? (
            <Loader2 className="animate-spin" size={17} />
          ) : query.isError ? (
            <span className="text-sm font-normal text-red-600">
              {apiErrorMessage(query.error, "не посчиталось")}
            </span>
          ) : (
            <>
              <span className="text-rose-700" title="Мы должны сотрудникам">
                {money.format(totals.payable)}
              </span>
              {totals.receivable > 0 ? (
                <span className="text-sky-700" title="Сотрудники должны нам (займы, переавансы)">
                  {money.format(totals.receivable)}
                </span>
              ) : null}
            </>
          )}
          <ArrowRight size={15} className={`transition-transform ${open ? "rotate-90" : ""}`} />
        </div>
      </button>
      {open && query.data ? (
        <div className="border-t">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Сотрудник</TableHead>
                <TableHead>Основа</TableHead>
                {!isCourier ? <TableHead className="text-right">Зарплата</TableHead> : null}
                {!isCourier ? (
                  <TableHead className="text-right">Накопительный фонд</TableHead>
                ) : null}
                {!isCourier ? (
                  <TableHead className="text-right">Депозит производства</TableHead>
                ) : null}
                <TableHead className="text-right">Депозит курьера</TableHead>
                <TableHead className="text-right">Мы должны</TableHead>
                <TableHead className="text-right">Нам должны</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((row) => (
                <TableRow key={row.employee_id}>
                  <TableCell>
                    <div className="font-medium">{row.full_name}</div>
                    <div className="text-xs text-muted-foreground">{row.position ?? "—"}</div>
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {STAFF_BASIS_LABEL[row.basis] ?? row.basis}
                  </TableCell>
                  {!isCourier ? (
                    <TableCell
                      className="text-right tabular-nums"
                      title={
                        row.salary_payable > 0
                          ? [
                              row.earned_to_date > 0
                                ? `текущая ЗП ${money.format(row.earned_to_date)}`
                                : null,
                              row.on_demand_debt > 0
                                ? `по востребованию ${money.format(row.on_demand_debt)}`
                                : null,
                              row.finalized_unpaid > 0
                                ? `хвост ведомостей ${money.format(row.finalized_unpaid)}`
                                : null,
                            ]
                              .filter(Boolean)
                              .join(" · ")
                          : undefined
                      }
                    >
                      {row.salary_payable > 0 ? money.format(row.salary_payable) : "—"}
                    </TableCell>
                  ) : null}
                  {!isCourier ? (
                    <TableCell
                      className="text-right tabular-nums"
                      title={
                        row.fund_payable > 0
                          ? `${new Date(query.data.as_of).getFullYear()}: ${money.format(
                              row.fund_current_year_payable,
                            )} · прошлые годы: ${money.format(row.fund_prior_years_payable)}`
                          : undefined
                      }
                    >
                      {row.fund_payable > 0 ? money.format(row.fund_payable) : "—"}
                    </TableCell>
                  ) : null}
                  {!isCourier ? (
                    <TableCell className="text-right tabular-nums">
                      {row.production_deposit_payable > 0
                        ? money.format(row.production_deposit_payable)
                        : "—"}
                    </TableCell>
                  ) : null}
                  <TableCell className="text-right tabular-nums">
                    {row.courier_deposit_payable > 0
                      ? money.format(row.courier_deposit_payable)
                      : "—"}
                  </TableCell>
                  <TableCell className="text-right font-semibold tabular-nums text-rose-700">
                    {row.payable > 0 ? money.format(row.payable) : "—"}
                  </TableCell>
                  <TableCell
                    className="text-right font-semibold tabular-nums text-sky-700"
                    title={
                      row.receivable > 0
                        ? [
                            row.advances_outstanding > 0
                              ? `авансы ${money.format(row.advances_outstanding)}`
                              : null,
                            row.loans_outstanding > 0
                              ? `займы ${money.format(row.loans_outstanding)}`
                              : null,
                            row.salary_payouts_outstanding > 0
                              ? `выплаты вне ведомости ${money.format(row.salary_payouts_outstanding)}`
                              : null,
                            row.on_demand_debt < 0
                              ? `переплата по востребованию ${money.format(-row.on_demand_debt)}`
                              : null,
                          ]
                            .filter(Boolean)
                            .join(" · ")
                        : undefined
                    }
                  >
                    {row.receivable > 0 ? money.format(row.receivable) : "—"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      ) : null}
    </div>
  );
}

function TaxDebtCard({ query }: { query: ReturnType<typeof useQuery<TaxDebt>> }) {
  const [open, setOpen] = useState(false);
  const wallet = query.data?.wallet;
  const today = todayIso();
  return (
    <div className="rounded-lg border bg-background">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left"
      >
        <div>
          <div className="font-medium">Задолженность по налогам</div>
          <div className="text-xs text-muted-foreground">
            Все известные обязательства перед бюджетом, не закрытые фактом уплаты из банка.
            «Отправлен в банк» — ещё долг: его гасит только списание из выписки.
          </div>
        </div>
        <div className="flex items-center gap-3 text-lg font-semibold tabular-nums">
          {query.isLoading ? (
            <Loader2 className="animate-spin" size={17} />
          ) : query.isError ? (
            <span className="text-sm font-normal text-red-600">
              {apiErrorMessage(query.error, "не посчиталось")}
            </span>
          ) : (
            <>
              <span className="text-rose-700" title="Мы должны бюджету">
                {money.format(query.data?.payable_total ?? 0)}
              </span>
              {(wallet?.balance ?? 0) > 0 ? (
                <span className="text-sky-700" title="Переплата в бюджет (расчётное сальдо ЕНС)">
                  {money.format(wallet?.balance ?? 0)}
                </span>
              ) : null}
            </>
          )}
          <ArrowRight size={15} className={`transition-transform ${open ? "rotate-90" : ""}`} />
        </div>
      </button>
      {open && query.data ? (
        <div className="border-t">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Обязательство</TableHead>
                <TableHead>Срок</TableHead>
                <TableHead>Статус</TableHead>
                <TableHead className="text-right">Сумма</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {query.data.items.length === 0 ? (
                <TableStatus colSpan={4} state="empty" />
              ) : (
                query.data.items.map((row) => {
                  const overdue = row.due_date != null && row.due_date < today && !row.draft_status;
                  return (
                    <TableRow key={`${row.kind}:${row.for_period ?? "year"}`}>
                      <TableCell className="font-medium">{row.title}</TableCell>
                      <TableCell
                        className={`tabular-nums ${overdue ? "font-medium text-red-600" : "text-muted-foreground"}`}
                      >
                        {row.due_date ? fmtDate(row.due_date) : "—"}
                        {overdue ? " · срок прошёл" : ""}
                      </TableCell>
                      <TableCell className="text-sm text-muted-foreground">
                        {row.draft_status
                          ? (TAX_DRAFT_STATUS_LABEL[row.draft_status] ?? row.draft_status)
                          : "к уплате"}
                      </TableCell>
                      <TableCell className="text-right font-semibold tabular-nums text-rose-700">
                        {money.format(row.amount)}
                      </TableCell>
                    </TableRow>
                  );
                })
              )}
            </TableBody>
          </Table>
          {wallet ? (
            <div className="border-t px-4 py-3 text-xs text-muted-foreground">
              <span className="font-medium text-foreground">Расчётный ЕНС-кошелёк:</span>{" "}
              уплачено через ЕНС {money.format(wallet.inflow)} − признано начислений{" "}
              {money.format(wallet.recognized)} ={" "}
              {wallet.balance > 0 ? (
                <span className="font-semibold text-sky-700">
                  переплата {money.format(wallet.balance)}
                </span>
              ) : (
                "переплаты нет"
              )}
              . Сальдо считается из выписки и наших начислений, а не вводится руками; с витриной
              личного кабинета ФНС может расходиться на резервы и пени.
              {wallet.shortfall > 0 ? (
                <>
                  {" "}
                  Фактов уплаты меньше признанных начислений на{" "}
                  {money.format(wallet.shortfall)} — обычно это платёж в пути или неполная
                  выписка; сами долги видны строками выше и в минус кошелёк не уходит.
                </>
              ) : null}{" "}
              <button
                type="button"
                className="text-primary hover:underline"
                onClick={() => navigateTo("/taxes")}
              >
                Открыть «Налоги»
              </button>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function BalancesSection({
  query,
  staffQuery,
  taxQuery,
  onOpenRegister,
}: {
  query: ReturnType<typeof useQuery<BalanceList>>;
  staffQuery: ReturnType<typeof useQuery<StaffPayable>>;
  taxQuery: ReturnType<typeof useQuery<TaxDebt>> | null;
  onOpenRegister: (target: "payments" | "documents", cp: CounterpartyBalance | null) => void;
}) {
  const [search, setSearch] = useState("");
  const items = useMemo(() => {
    const all = query.data?.items ?? [];
    const needle = search.trim().toLowerCase();
    if (!needle) return all;
    return all.filter(
      (item) => item.name.toLowerCase().includes(needle) || (item.inn ?? "").includes(needle),
    );
  }, [query.data, search]);

  return (
    <div className="flex flex-col gap-3">
      <StaffPayableCard query={staffQuery} group="staff" />
      <StaffPayableCard query={staffQuery} group="courier" />
      {taxQuery ? <TaxDebtCard query={taxQuery} /> : null}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Input
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Поиск по названию или ИНН…"
          className="max-w-xs"
        />
        <p className="text-xs text-muted-foreground">
          Дебиторка — открытые предоплаты; кредиторка — неоплаченные накладные и акты.
        </p>
      </div>
      <div className="overflow-hidden rounded-lg border bg-background">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Контрагент</TableHead>
              <TableHead className="text-right">Дебиторка</TableHead>
              <TableHead className="text-right">Кредиторка</TableHead>
              <TableHead className="text-right">Нетто</TableHead>
              <TableHead className="text-right">Последнее движение</TableHead>
              <TableHead className="w-44" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.isLoading ? (
              <TableStatus colSpan={6} state="loading" />
            ) : query.isError ? (
              <TableStatus
                colSpan={6}
                state={apiErrorMessage(query.error, "Не удалось загрузить остатки")}
              />
            ) : items.length === 0 ? (
              <TableStatus colSpan={6} state="empty" />
            ) : (
              items.map((item) => (
                <TableRow key={item.counterparty_id}>
                  <TableCell>
                    <div className="font-medium">{item.name}</div>
                    <div className="text-xs text-muted-foreground">
                      {item.inn ? `ИНН ${item.inn}` : "без ИНН"}
                      {item.open_prepayments > 0 ? ` · предоплат: ${item.open_prepayments}` : ""}
                      {item.unpaid_invoices > 0 ? ` · накладных: ${item.unpaid_invoices}` : ""}
                    </div>
                  </TableCell>
                  <TableCell className="text-right tabular-nums text-sky-700">
                    {item.receivable ? moneyExact.format(item.receivable) : "—"}
                  </TableCell>
                  <TableCell className="text-right tabular-nums text-rose-700">
                    {item.payable ? moneyExact.format(item.payable) : "—"}
                  </TableCell>
                  <TableCell
                    className={`text-right font-semibold tabular-nums ${item.net >= 0 ? "text-sky-700" : "text-rose-700"}`}
                  >
                    {moneyExact.format(item.net)}
                  </TableCell>
                  <TableCell className="text-right text-sm text-muted-foreground">
                    {fmtDate(item.last_activity)}
                  </TableCell>
                  <TableCell>
                    <div className="flex justify-end gap-1">
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => onOpenRegister("payments", item)}
                      >
                        Платежи <ArrowRight size={13} />
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => onOpenRegister("documents", item)}
                      >
                        УПД <ArrowRight size={13} />
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}

function RegisterFiltersBar({
  filters,
  onChange,
}: {
  filters: RegisterFilters;
  onChange: (filters: RegisterFilters) => void;
}) {
  return (
    <div className="flex flex-wrap items-end gap-3">
      <div className="grid gap-1">
        <Label className="text-xs text-muted-foreground">С даты</Label>
        <Input
          type="date"
          value={filters.date_from}
          onChange={(event) => onChange({ ...filters, date_from: event.target.value })}
          className="w-40"
        />
      </div>
      <div className="grid gap-1">
        <Label className="text-xs text-muted-foreground">По дату</Label>
        <Input
          type="date"
          value={filters.date_to}
          min={filters.date_from || undefined}
          onChange={(event) => onChange({ ...filters, date_to: event.target.value })}
          className="w-40"
        />
      </div>
      {filters.counterparty_id ? (
        <Badge variant="outline" className="mb-1 flex items-center gap-1 py-1.5">
          {filters.counterparty_name ?? "Контрагент"}
          <button
            type="button"
            className="ml-1 rounded-full p-0.5 hover:bg-muted"
            title="Сбросить фильтр по контрагенту"
            onClick={() =>
              onChange({ ...filters, counterparty_id: null, counterparty_name: null })
            }
          >
            <X size={12} />
          </button>
        </Badge>
      ) : null}
    </div>
  );
}

function SettledInvoicesChips({ refs }: { refs: SettledInvoiceRef[] }) {
  if (refs.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-1">
      {refs.map((ref) => (
        <Badge
          key={ref.invoice_id}
          variant="outline"
          className="border-emerald-200 bg-emerald-50 text-emerald-700"
        >
          {ref.number ? `№ ${ref.number}` : "УПД"} · {moneyExact.format(ref.amount)}
        </Badge>
      ))}
    </div>
  );
}

function PaymentsSection({ filters }: { filters: RegisterFilters }) {
  const query = useQuery({
    queryKey: ["accounting", "payments", filters.date_from, filters.date_to, filters.counterparty_id],
    queryFn: () => getPayments(filters),
  });

  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs text-muted-foreground">
        Исходящие платежи поставщикам и входящие остатки. В колонке «Гашение» — накладные,
        закрытые этим платежом, либо предоплата и УПД, которыми она погашена.
      </p>
      <div className="overflow-hidden rounded-lg border bg-background">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Дата</TableHead>
              <TableHead>Контрагент</TableHead>
              <TableHead>Канал / назначение</TableHead>
              <TableHead className="text-right">Сумма</TableHead>
              <TableHead>Гашение</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.isLoading ? (
              <TableStatus colSpan={5} state="loading" />
            ) : query.isError ? (
              <TableStatus
                colSpan={5}
                state={apiErrorMessage(query.error, "Не удалось загрузить реестр платежей")}
              />
            ) : (query.data?.items.length ?? 0) === 0 ? (
              <TableStatus colSpan={5} state="empty" />
            ) : (
              query.data?.items.map((row) => (
                <TableRow key={`${row.row_kind}:${row.id}`}>
                  <TableCell className="whitespace-nowrap">{fmtDate(row.operation_date)}</TableCell>
                  <TableCell>
                    <div className="font-medium">{row.counterparty_name}</div>
                    {row.row_kind === "opening_prepayment" ? (
                      <Badge
                        variant="outline"
                        className="mt-0.5 border-sky-200 bg-sky-50 text-sky-700"
                      >
                        Входящий остаток
                      </Badge>
                    ) : null}
                  </TableCell>
                  <TableCell className="max-w-72">
                    <div className="text-sm">
                      {row.wallet_name ?? "без движения денег"}
                      {row.article_name ? ` · ${row.article_name}` : ""}
                    </div>
                    {row.purpose ? (
                      <div className="truncate text-xs text-muted-foreground" title={row.purpose}>
                        {row.purpose}
                      </div>
                    ) : null}
                  </TableCell>
                  <TableCell className="text-right font-semibold tabular-nums">
                    {moneyExact.format(row.amount)}
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-col gap-1">
                      <SettledInvoicesChips refs={row.settled_invoices} />
                      {row.prepayment ? (
                        <div className="text-xs">
                          <Badge
                            variant="outline"
                            className="border-sky-200 bg-sky-50 text-sky-700"
                          >
                            {PREPAYMENT_KIND_LABEL[row.prepayment.kind] ?? row.prepayment.kind}:
                            погашено {moneyExact.format(row.prepayment.amount_settled)} из{" "}
                            {moneyExact.format(row.prepayment.amount)}
                          </Badge>
                          <div className="mt-1">
                            <SettledInvoicesChips refs={row.prepayment.settled_invoices} />
                          </div>
                        </div>
                      ) : null}
                      {row.unassigned_amount > 0 ? (
                        <Badge
                          variant="outline"
                          className="w-fit border-amber-200 bg-amber-50 text-amber-800"
                        >
                          Без документа: {moneyExact.format(row.unassigned_amount)}
                        </Badge>
                      ) : null}
                    </div>
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>
      {query.data ? (
        <p className="text-right text-xs text-muted-foreground">
          Всего за период: {moneyExact.format(query.data.total_amount)} ·{" "}
          {query.data.items.length} строк
        </p>
      ) : null}
    </div>
  );
}

function DocumentsSection({ filters }: { filters: RegisterFilters }) {
  const query = useQuery({
    queryKey: [
      "accounting",
      "documents",
      filters.date_from,
      filters.date_to,
      filters.counterparty_id,
    ],
    queryFn: () => getDocuments(filters),
  });

  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs text-muted-foreground">
        Входящие накладные, акты и УПД. В колонке «Оплата» — чем закрыт документ: банк, касса или
        зачёт из предоплаты.
      </p>
      <div className="overflow-hidden rounded-lg border bg-background">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Дата</TableHead>
              <TableHead>Документ</TableHead>
              <TableHead>Контрагент</TableHead>
              <TableHead>Период услуги</TableHead>
              <TableHead className="text-right">Сумма</TableHead>
              <TableHead>Статус</TableHead>
              <TableHead>Оплата</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.isLoading ? (
              <TableStatus colSpan={7} state="loading" />
            ) : query.isError ? (
              <TableStatus
                colSpan={7}
                state={apiErrorMessage(query.error, "Не удалось загрузить реестр УПД")}
              />
            ) : (query.data?.items.length ?? 0) === 0 ? (
              <TableStatus colSpan={7} state="empty" />
            ) : (
              query.data?.items.map((row) => {
                const status = PAYMENT_STATUS_LABEL[row.payment_status];
                return (
                  <TableRow key={row.invoice_id}>
                    <TableCell className="whitespace-nowrap">{fmtDate(row.invoice_date)}</TableCell>
                    <TableCell>
                      <div className="flex items-center gap-2">
                        <span className="font-medium">
                          {row.number ? `№ ${row.number}` : "Без номера"}
                        </span>
                        {row.activation_status === "pending" ? (
                          <span className="rounded border border-violet-200 bg-violet-50 px-1.5 py-0.5 text-[10px] font-medium text-violet-700">
                            будущий · ждёт {fmtDate(row.invoice_date)}
                          </span>
                        ) : null}
                      </div>
                      <div className="text-xs text-muted-foreground">
                        {INVOICE_SOURCE_LABEL[row.source] ?? row.source}
                      </div>
                    </TableCell>
                    <TableCell>{row.counterparty_name}</TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {row.service_period_start
                        ? formatPeriod(row.service_period_start, row.service_period_end)
                        : "—"}
                    </TableCell>
                    <TableCell className="text-right font-semibold tabular-nums">
                      {moneyExact.format(row.amount)}
                    </TableCell>
                    <TableCell>
                      {status ? (
                        <Badge variant="outline" className={status.className}>
                          {status.label}
                        </Badge>
                      ) : (
                        row.payment_status
                      )}
                      {row.remainder > 0 ? (
                        <div className="mt-1 text-xs tabular-nums text-rose-700">
                          остаток {moneyExact.format(row.remainder)}
                        </div>
                      ) : null}
                    </TableCell>
                    <TableCell>
                      <div className="flex flex-wrap gap-1">
                        {row.allocations.length === 0 ? (
                          <span className="text-xs text-muted-foreground">—</span>
                        ) : (
                          row.allocations.map((alloc, index) => (
                            <Badge
                              key={`${row.invoice_id}:${index}`}
                              variant="outline"
                              className={
                                alloc.source_kind === "prepayment"
                                  ? "border-sky-200 bg-sky-50 text-sky-700"
                                  : "border-emerald-200 bg-emerald-50 text-emerald-700"
                              }
                              title={alloc.operation_date ? fmtDate(alloc.operation_date) : undefined}
                            >
                              {ALLOCATION_LABEL[alloc.source_kind] ?? alloc.source_kind} ·{" "}
                              {moneyExact.format(alloc.amount)}
                            </Badge>
                          ))
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })
            )}
          </TableBody>
        </Table>
      </div>
      {query.data ? (
        <p className="text-right text-xs text-muted-foreground">
          Документов: {query.data.items.length} на {moneyExact.format(query.data.total_amount)} ·
          не оплачено {moneyExact.format(query.data.unpaid_total)}
        </p>
      ) : null}
    </div>
  );
}

function RecognitionSection({
  canEdit,
  canCorrectRecognized,
}: {
  canEdit: boolean;
  canCorrectRecognized: boolean;
}) {
  const [view, setView] = useState<View>("open");
  const [editing, setEditing] = useState<AccountingItem | null>(null);
  const query = useQuery({
    queryKey: ["accounting", "suppliers", view],
    queryFn: () => getAccounting(view),
  });

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Tabs value={view} onValueChange={(value) => setView(value as View)}>
          <TabsList>
            <TabsTrigger value="open">Актуальные</TabsTrigger>
            <TabsTrigger value="needs_review">На ручной разбор</TabsTrigger>
            <TabsTrigger value="recognized">Признанные</TabsTrigger>
            <TabsTrigger value="all">Все</TabsTrigger>
          </TabsList>
        </Tabs>
        <p className="text-xs text-muted-foreground">
          Расход признаётся после окончания последнего дня периода.
        </p>
      </div>

      <div className="overflow-hidden rounded-lg border bg-background">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Статус</TableHead>
              <TableHead>Контрагент / документ</TableHead>
              <TableHead>Период услуги</TableHead>
              <TableHead>Статья</TableHead>
              <TableHead className="text-right">Оплачено</TableHead>
              <TableHead className="text-right">Остаток</TableHead>
              <TableHead className="w-14" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.isLoading ? (
              <TableStatus colSpan={7} state="loading" />
            ) : query.isError ? (
              <TableStatus
                colSpan={7}
                state={apiErrorMessage(query.error, "Не удалось загрузить учёт ДЗ/КЗ")}
              />
            ) : (query.data?.items.length ?? 0) === 0 ? (
              <TableStatus colSpan={7} state="empty" />
            ) : (
              query.data?.items.map((item) => {
                const status = STATUS[item.balance_type];
                const correctionAllowed = !item.recognized || canCorrectRecognized;
                return (
                  <TableRow key={`${item.source_kind}:${item.id}`}>
                    <TableCell>
                      <Badge variant="outline" className={status.className}>
                        {status.label}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <div className="font-medium">{item.counterparty_name}</div>
                      <div className="text-xs text-muted-foreground">
                        {item.invoice_number ? `Счёт № ${item.invoice_number}` : "Предоплата"}
                      </div>
                    </TableCell>
                    <TableCell>
                      <div className={item.balance_type === "needs_review" ? "text-amber-700" : ""}>
                        {formatPeriod(item.service_period_start, item.service_period_end)}
                      </div>
                      {item.recognition_month ? (
                        <div className="text-xs text-muted-foreground">
                          P&L: {item.recognition_month.slice(0, 7)}
                        </div>
                      ) : null}
                    </TableCell>
                    <TableCell className="text-muted-foreground">{item.article_name ?? "—"}</TableCell>
                    <TableCell className="text-right tabular-nums">
                      {money.format(item.paid_amount)}
                    </TableCell>
                    <TableCell className="text-right font-semibold tabular-nums">
                      {money.format(item.balance_amount)}
                    </TableCell>
                    <TableCell>
                      {canEdit && item.source_kind === "service_period" ? (
                        <Button
                          size="icon"
                          variant="ghost"
                          title={
                            correctionAllowed
                              ? "Изменить период"
                              : "Нужно отдельное право на корректировку признанного расхода"
                          }
                          disabled={!correctionAllowed}
                          onClick={() => setEditing(item)}
                        >
                          <Pencil size={15} />
                        </Button>
                      ) : null}
                    </TableCell>
                  </TableRow>
                );
              })
            )}
          </TableBody>
        </Table>
      </div>

      {editing ? <PeriodDialog item={editing} onClose={() => setEditing(null)} /> : null}
    </div>
  );
}

function PeriodDialog({ item, onClose }: { item: AccountingItem; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [start, setStart] = useState(item.service_period_start ?? "");
  const [end, setEnd] = useState(item.service_period_end ?? "");
  const [reason, setReason] = useState("");
  const mutation = useMutation({
    mutationFn: () =>
      updatePeriod(item.id, {
        service_period_start: start,
        service_period_end: end,
        reason: reason.trim() || null,
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["accounting"] });
      toast.success("Период услуги изменён");
      onClose();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось изменить период")),
  });
  const ready = Boolean(start && end && end >= start && (!item.recognized || reason.trim()));

  return (
    <Dialog open onOpenChange={(open) => (!open ? onClose() : undefined)}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Период оказания услуги</DialogTitle>
          <DialogDescription>
            {item.counterparty_name}
            {item.invoice_number ? ` · счёт № ${item.invoice_number}` : ""}
          </DialogDescription>
        </DialogHeader>
        {item.recognized ? (
          <div className="flex gap-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
            <AlertTriangle className="mt-0.5 shrink-0" size={17} />
            Период уже попал в P&L. Изменение перенесёт расход в другой месяц и останется в журнале
            аудита.
          </div>
        ) : null}
        <div className="grid grid-cols-2 gap-3">
          <div className="grid gap-1.5">
            <Label>С</Label>
            <Input type="date" value={start} onChange={(event) => setStart(event.target.value)} />
          </div>
          <div className="grid gap-1.5">
            <Label>По</Label>
            <Input
              type="date"
              min={start || undefined}
              value={end}
              onChange={(event) => setEnd(event.target.value)}
            />
          </div>
        </div>
        <div className="grid gap-1.5">
          <Label>{item.recognized ? "Причина корректировки *" : "Комментарий"}</Label>
          <Textarea value={reason} onChange={(event) => setReason(event.target.value)} />
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Отмена
          </Button>
          <Button disabled={!ready || mutation.isPending} onClick={() => mutation.mutate()}>
            Сохранить
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
