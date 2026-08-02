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
import { ArticleCombobox } from "@/components/ui-app/ArticleCombobox";
import { CounterpartyCard } from "@/routes/counterparties/CounterpartyCard";
import { getExpenseArticles } from "@/routes/counterparties/api";

/** Состояние строки на языке владельца: «этот платёж уже стал расходом, а если нет — чего ждём». */
type Stage = "needs_period" | "waiting_document" | "period_running" | "in_expense";
// «Разрывы» отдельной вкладкой не живут: та же информация — срок и просрочка — теперь внутри
// состояния «ждём документ». Платежи и УПД схлопнуты в один реестр с переключателем.
type Section = "balances" | "recognition" | "expenses" | "register";
type RegisterView = "payments" | "documents";

type AccountingItem = {
  id: string;
  source_kind: "service_period" | "legacy_prepayment";
  stage: Stage;
  counterparty_id: string;
  counterparty_name: string;
  article_id: string | null;
  article_name: string | null;
  invoice_id: string | null;
  invoice_number: string | null;
  document_kind: string | null;
  amount: number;
  paid_amount: number;
  balance_amount: number;
  balance_type: "receivable" | "payable" | "scheduled" | "closed" | "needs_review";
  service_period_start: string | null;
  service_period_end: string | null;
  period_status: string;
  recognition_month: string | null;
  recognized: boolean;
  payment_date: string | null;
  expected_by: string | null;
  days_overdue: number;
  period_assumed: boolean;
  opening: boolean;
  settled: boolean;
  auto_recognition_on: string | null;
  document_amount: number | null;
  amount_mismatch: number;
  can_recognize: boolean;
  recognize_blocked_reason: string | null;
};

type StageTile = { count: number; amount: number };

type AccountingList = {
  items: AccountingItem[];
  receivable_total: number;
  payable_total: number;
  scheduled_total: number;
  needs_review_total: number;
  in_expense: StageTile;
  period_running: StageTile;
  waiting_document: StageTile;
  needs_period: StageTile;
  in_expense_month: string | null;
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
  informational: boolean;
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
    vacation_payable: number;
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
    // Прогноз официального контура (документов ещё нет): в долге виден, платить рано.
    is_projection?: boolean;
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

async function getAccounting(
  stage: Stage | null,
  includeSettled = false,
): Promise<AccountingList> {
  const response = await api.get<AccountingList>("/accounting/suppliers", {
    params: {
      view: "all",
      stage: stage ?? undefined,
      include_settled: includeSettled ? true : undefined,
    },
  });
  return response.data;
}

async function reverseExpense(
  accrualId: string,
  payload: { amount: number; reason: string },
): Promise<{ reversed_amount: number; amount_left: number; fully_cancelled: boolean }> {
  const response = await api.post(`/accounting/suppliers/accruals/${accrualId}/reverse`, payload);
  return response.data;
}

async function recognizePrepayment(
  id: string,
  payload: {
    service_period_start: string;
    service_period_end: string;
    dds_article_id?: string | null;
  },
): Promise<{ months_recognized: number; amount_recognized: number; period_months: number }> {
  const response = await api.post(`/accounting/suppliers/prepayments/${id}/recognize`, payload);
  return response.data;
}

async function getStaffPayable(): Promise<StaffPayable> {
  const response = await api.get<StaffPayable>("/accounting/suppliers/staff-payable");
  return response.data;
}

async function getTaxDebt(): Promise<TaxDebt> {
  const response = await api.get<TaxDebt>("/taxes/debt");
  // Налоговый контур объявляет суммы как Decimal, а Pydantic сериализует Decimal в
  // СТРОКУ — тогда как остальные источники этой страницы отдают number. В сумме
  // «поставщики + сотрудники + налоги» строка превращала сложение в конкатенацию и
  // давала невалидное число с двумя точками → плитка КЗ печатала «не число ₽».
  // Приводим к числу ровно здесь, на границе с API, чтобы дальше по странице
  // арифметика работала с number, как заявляет тип.
  const data = response.data;
  return {
    ...data,
    payable_total: Number(data.payable_total),
    wallet: { ...data.wallet, balance: Number(data.wallet.balance) },
  };
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

type ExpenseMonthCell = {
  month: string;
  article_id: string | null;
  article_name: string;
  amount: number;
};

type ExpenseByMonth = {
  months: string[];
  items: ExpenseMonthCell[];
  total: number;
  unattributed: number;
  without_primary: number;
  without_location: number;
};

type BalanceAsOfRow = {
  counterparty_id: string;
  counterparty_name: string;
  receivable: number;
  payable: number;
};

type BalanceAsOf = {
  as_of: string;
  items: BalanceAsOfRow[];
  receivable_total: number;
  payable_total: number;
  approximate_settlements: number;
};

type OriginDocument = {
  kind: string;
  invoice_id: string | null;
  number: string | null;
  invoice_date: string | null;
  amount: number | null;
  intake_id: string | null;
  has_pdf: boolean;
};

type RecognitionOrigin = {
  counterparty_name: string;
  amount: number;
  basis: OriginDocument | null;
  basis_note: string;
  closing: OriginDocument | null;
  closing_note: string;
};

async function getRecognitionOrigin(prepaymentId: string): Promise<RecognitionOrigin> {
  const response = await api.get<RecognitionOrigin>(
    `/accounting/suppliers/prepayments/${prepaymentId}/origin`,
  );
  return response.data;
}

async function fetchOriginPdfUrl(intakeId: string): Promise<string> {
  const response = await api.get(`/accounting/suppliers/intakes/${intakeId}/pdf`, {
    responseType: "blob",
  });
  return URL.createObjectURL(response.data as Blob);
}

async function getBalancesAsOf(asOf: string): Promise<BalanceAsOf> {
  const response = await api.get<BalanceAsOf>("/accounting/suppliers/balances/as-of", {
    params: { as_of: asOf },
  });
  return response.data;
}

async function getExpensesByMonth(dateFrom: string, dateTo: string): Promise<ExpenseByMonth> {
  const response = await api.get<ExpenseByMonth>("/accounting/suppliers/expenses/by-month", {
    params: { date_from: dateFrom, date_to: dateTo },
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

/** Бухгалтерские «дебиторка / будущий расход» владельцу ничего не говорили: по ним нельзя было
 *  понять, ждут ли от него действия. Каждое состояние отвечает на это одной фразой. */
const STAGE: Record<
  Stage,
  { label: string; hint: string; className: string; tile: string }
> = {
  needs_period: {
    label: "Нужно ваше решение",
    hint: "Документа не будет — укажите, за какой период услуга, и расход разложится по месяцам",
    className: "border-amber-300 bg-amber-50 text-amber-900",
    tile: "border-amber-300 bg-amber-50",
  },
  waiting_document: {
    label: "Ждём документ",
    hint: "Сумму расхода принесёт УПД от контрагента. Красное — срок прошёл",
    className: "border-sky-200 bg-sky-50 text-sky-700",
    tile: "border-sky-200 bg-sky-50",
  },
  period_running: {
    label: "Период идёт",
    hint: "Расход признаётся сам, по окончании каждого месяца. Делать ничего не нужно",
    className: "border-violet-200 bg-violet-50 text-violet-700",
    tile: "border-violet-200 bg-violet-50",
  },
  in_expense: {
    label: "Уже в расходе",
    hint: "Попало в прибыль своего месяца",
    className: "border-emerald-200 bg-emerald-50 text-emerald-700",
    tile: "border-emerald-200 bg-emerald-50",
  },
};

const STAGE_ORDER: Stage[] = ["needs_period", "waiting_document", "period_running", "in_expense"];

function monthTitle(value: string | null): string {
  if (!value) return "";
  const [year, month] = value.split("-");
  const names = [
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
  ];
  return `${names[Number(month) - 1] ?? ""} ${year}`;
}

function formatPeriod(start: string | null, end: string | null) {
  if (!start || !end) return "Период не указан";
  return `${fmtDate(start)} — ${fmtDate(end)}`;
}

const DEFAULT_DATE_FROM = "2026-06-01";

export function DzKzRoute() {
  const permissions = usePermissions();
  const [section, setSection] = useState<Section>("balances");
  const [register, setRegister] = useState<RegisterView>("payments");
  const [filters, setFilters] = useState<RegisterFilters>({
    date_from: DEFAULT_DATE_FROM,
    date_to: "",
    counterparty_id: null,
    counterparty_name: null,
  });

  // Счётчик на вкладке: сколько строк ждут решения человека.
  const accounting = useQuery({
    queryKey: ["accounting", "suppliers", null],
    queryFn: () => getAccounting(null),
  });
  const balances = useQuery({ queryKey: ["accounting", "balances"], queryFn: getBalances });
  const [gapCardId, setGapCardId] = useState<string | null>(null);
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
  // Number() здесь не для красоты: /taxes/debt отдаёт Decimal, то есть в JSON это СТРОКА
  // ("57390.00"), а тип number во фронте — только обещание. Без приведения `+` склеивал
  // строки, и главная плитка «Кредиторская задолженность» показывала «не число ₽» всякий
  // раз, когда налоговый долг ненулевой.
  const taxPayable = canSeeTaxes ? Number(taxes.data?.payable_total ?? 0) : null;
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
  // Тот же Decimal-в-строке, что и в payable_total: без Number() дебиторка склеивалась бы
  // с остатком ЕНС-кошелька в текст.
  const taxReceivable = canSeeTaxes ? Number(taxes.data?.wallet.balance ?? 0) : null;
  const receivableTotal =
    supplierReceivable == null && peopleReceivable == null && taxReceivable == null
      ? undefined
      : (supplierReceivable ?? 0) + (peopleReceivable ?? 0) + (taxReceivable ?? 0);

  const openCounterpartyCard = (counterpartyId: string) => setGapCardId(counterpartyId);

  const openRegister = (target: RegisterView, cp: CounterpartyBalance | null) => {
    setFilters((prev) => ({
      ...prev,
      counterparty_id: cp?.counterparty_id ?? null,
      counterparty_name: cp?.name ?? null,
    }));
    setRegister(target);
    setSection("register");
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
          <TabsTrigger value="recognition">
            Признание расходов
            {(accounting.data?.needs_period.count ?? 0) > 0 ? (
              <span className="ml-1.5 rounded-full bg-amber-100 px-1.5 text-xs text-amber-800">
                {accounting.data?.needs_period.count}
              </span>
            ) : null}
          </TabsTrigger>
          <TabsTrigger value="expenses">Расход по месяцам</TabsTrigger>
          <TabsTrigger value="register">Реестр</TabsTrigger>
        </TabsList>
      </Tabs>

      {section === "balances" ? (
        <BalancesSection
          query={balances}
          staffQuery={staff}
          taxQuery={canSeeTaxes ? taxes : null}
          onOpenRegister={openRegister}
          onOpenCard={openCounterpartyCard}
        />
      ) : null}
      {section === "expenses" ? <ExpensesByMonthSection /> : null}
      {section === "register" ? (
        <div className="flex flex-col gap-3">
          {/* Платежи и документы — две стороны одного вопроса «что было с этим контрагентом»,
              и держать их разными вкладками значило заставлять переключаться туда-обратно. */}
          <Tabs value={register} onValueChange={(value) => setRegister(value as RegisterView)}>
            <TabsList>
              <TabsTrigger value="payments">Платежи</TabsTrigger>
              <TabsTrigger value="documents">УПД и накладные</TabsTrigger>
            </TabsList>
          </Tabs>
          <RegisterFiltersBar filters={filters} onChange={setFilters} />
          {register === "payments" ? (
            <PaymentsSection filters={filters} />
          ) : (
            <DocumentsSection filters={filters} />
          )}
        </div>
      ) : null}
      <CounterpartyCard
        counterpartyId={gapCardId}
        canOperate={permissions.hasPermission("counterparties.operate")}
        canAdmin={permissions.hasPermission("counterparties.admin")}
        onClose={() => setGapCardId(null)}
        defaultTab="settlement"
      />
      {section === "recognition" ? (
        <RecognitionSection
          canEdit={permissions.hasPermission("accounting.suppliers.edit")}
          canCorrectRecognized={permissions.hasPermission(
            "accounting.service_periods.correct_recognized",
          )}
          canReverse={permissions.hasPermission("accounting.expenses.reverse")}
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

/** ``tone`` нужен потому, что произвольный текст здесь означал только ошибку и красился
 *  красным. «Все документы получены» — хорошая новость, а выглядела как сбой. */
function TableStatus({
  colSpan,
  state,
  tone = "error",
}: {
  colSpan: number;
  state: "loading" | "empty" | string;
  tone?: "error" | "calm";
}) {
  const neutral = state === "loading" || state === "empty" || tone === "calm";
  return (
    <TableRow>
      <TableCell
        colSpan={colSpan}
        className={`py-12 text-center ${neutral ? "text-muted-foreground" : "text-red-600"}`}
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
      vacation: sum.vacation + row.vacation_payable,
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
      vacation: 0,
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
                  зарплата {money.format(totals.salary)}
                  {totals.vacation > 0 ? ` (в т.ч. отпускные ${money.format(totals.vacation)})` : null}
                  {" · "}фонд {money.format(totals.fund)} (
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
                              row.vacation_payable > 0
                                ? `отпускные ${money.format(row.vacation_payable)}`
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
                  const overdue =
                    row.due_date != null &&
                    row.due_date < today &&
                    !row.draft_status &&
                    !row.is_projection;
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
                        {row.is_projection
                          ? "прогноз — ждём документы"
                          : row.draft_status
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
  onOpenCard,
}: {
  query: ReturnType<typeof useQuery<BalanceList>>;
  staffQuery: ReturnType<typeof useQuery<StaffPayable>>;
  taxQuery: ReturnType<typeof useQuery<TaxDebt>> | null;
  onOpenRegister: (target: "payments" | "documents", cp: CounterpartyBalance | null) => void;
  onOpenCard: (counterpartyId: string) => void;
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
      <BalanceAsOfCard />
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
                    {/* Имя ведёт в сверку: остаток в этой строке и есть итог сверки,
                        и вопрос «откуда взялось это число» отвечается одним кликом. */}
                    <button
                      type="button"
                      className="font-medium hover:underline"
                      onClick={() => onOpenCard(item.counterparty_id)}
                    >
                      {item.name}
                    </button>
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
                        onClick={() => onOpenCard(item.counterparty_id)}
                      >
                        Сверка <ArrowRight size={13} />
                      </Button>
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

/** Реестр платежей: за что заплатили и чем это закрыто. Половина «Реестра».
 *
 *  Вопрос «у кого за прошлый месяц нет УПД» здесь больше не решается — на него отвечает
 *  состояние «ждём документ» на «Признании расходов», со сроком и просрочкой в строке.
 *  Реестр остаётся тем, чем и был: поиском по истории расчётов с контрагентом.
 */
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
                        {/* Документ по контрагенту с договором: виден, но в расчётах не
                            участвует — расход уже начислен договором. Прятать его нельзя,
                            расхождение с договором надо замечать. */}
                        {row.informational ? (
                          <span className="rounded border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-[10px] font-medium text-slate-600">
                            справочный · расход по договору
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
  canReverse,
}: {
  canEdit: boolean;
  canCorrectRecognized: boolean;
  canReverse: boolean;
}) {
  // Стартуем с очереди, которая ждёт человека. Если она пуста — экран сам покажет ожидание
  // документов: смотреть на пустой список «нужно ваше решение» бессмысленно.
  const [stage, setStage] = useState<Stage>("needs_period");
  const [editing, setEditing] = useState<AccountingItem | null>(null);
  const [recognizing, setRecognizing] = useState<AccountingItem | null>(null);
  const [origin, setOrigin] = useState<AccountingItem | null>(null);
  const [reversing, setReversing] = useState<AccountingItem | null>(null);
  // Закрытые платежи очередь по умолчанию не показывает — она про то, что требует шага. Но
  // вопрос «а где мой платёж от 7 июля» возникает именно здесь: человек ищет деньги там, где
  // смотрит на долги, а не в реестре.
  const [showSettled, setShowSettled] = useState(false);
  const query = useQuery({
    queryKey: ["accounting", "suppliers", stage, showSettled],
    queryFn: () => getAccounting(stage, showSettled),
    // Плитки живут в том же ответе, что и список. Без этого клик по состоянию обнулял ВСЕ
    // четыре плитки на время запроса — включая ту, цифру из которой человек только что читал.
    placeholderData: (previous) => previous,
  });
  const tiles = query.data;
  const tileOf = (name: Stage): StageTile =>
    (tiles?.[name] as StageTile | undefined) ?? { count: 0, amount: 0 };

  // Один раз, после первой загрузки: если делать нечего, открываем «ждём документ».
  const [autoSwitched, setAutoSwitched] = useState(false);
  if (tiles && !autoSwitched) {
    setAutoSwitched(true);
    if (stage === "needs_period" && tileOf("needs_period").count === 0) {
      setStage("waiting_document");
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {STAGE_ORDER.map((name) => {
          const tile = tileOf(name);
          const active = stage === name;
          return (
            <button
              key={name}
              type="button"
              onClick={() => setStage(name)}
              className={`rounded-lg border p-3 text-left transition ${
                active ? `${STAGE[name].tile} ring-2 ring-offset-1` : "bg-background hover:bg-muted/50"
              }`}
            >
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-sm font-medium">{STAGE[name].label}</span>
                <span className="text-xs text-muted-foreground">{tile.count}</span>
              </div>
              <div className="mt-1 text-lg font-semibold tabular-nums">
                {money.format(tile.amount)}
              </div>
              <div className="mt-0.5 text-[11px] leading-tight text-muted-foreground">
                {name === "in_expense" && tiles?.in_expense_month
                  ? `за ${monthTitle(tiles.in_expense_month)}`
                  : STAGE[name].hint}
              </div>
            </button>
          );
        })}
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-muted-foreground">{STAGE[stage].hint}.</p>
        <Button
          className="h-7 px-2 text-xs"
          onClick={() => setShowSettled((value) => !value)}
          size="sm"
          variant="ghost"
        >
          {showSettled ? "Скрыть закрытые" : "Показать закрытые"}
        </Button>
      </div>

      <div className="overflow-hidden rounded-lg border bg-background">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Контрагент / документ</TableHead>
              <TableHead>Период услуги</TableHead>
              <TableHead>Статья</TableHead>
              <TableHead>{stage === "waiting_document" ? "Ждём документ" : "Признание"}</TableHead>
              <TableHead className="text-right">Сумма</TableHead>
              <TableHead className="w-40" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.isLoading ? (
              <TableStatus colSpan={6} state="loading" />
            ) : query.isError ? (
              <TableStatus
                colSpan={6}
                state={apiErrorMessage(query.error, "Не удалось загрузить признание расходов")}
              />
            ) : (query.data?.items.length ?? 0) === 0 ? (
              <TableStatus colSpan={6} state={EMPTY_STAGE[stage]} tone="calm" />
            ) : (
              query.data?.items.map((item) => {
                const isPrepayment = item.source_kind === "legacy_prepayment";
                const correctionAllowed = !item.recognized || canCorrectRecognized;
                return (
                  <TableRow
                    key={`${item.source_kind}:${item.id}`}
                    /* Закрытая строка показана справочно — она не должна спорить за внимание
                       с живыми долгами в той же таблице. */
                    className={item.settled ? "opacity-60" : undefined}
                  >
                    <TableCell>
                      <div className="font-medium">{item.counterparty_name}</div>
                      <div className="text-xs text-muted-foreground">
                        {item.invoice_number
                          ? /* Счёт — основание платежа, УПД — то, что признало расход.
                               Одна подпись на оба вводила в заблуждение. */
                            `${item.document_kind === "closing" ? "УПД / акт" : "Счёт"} № ${item.invoice_number}`
                          : item.opening
                            ? /* Входящее сальдо, а не оплата: искать эти деньги в выписке
                                 бесполезно — их там нет по определению. Без даты намеренно:
                                 у сальдо её нет, а ``payment_date`` здесь — день, когда
                                 остаток занесли в систему, и он выдал бы себя за дату денег. */
                              "Входящий остаток"
                            : item.payment_date
                              ? `Платёж от ${fmtDate(item.payment_date)}`
                              : "Платёж"}
                      </div>
                      {/* Документ изменился уже после того, как расход признан. Сумму в
                          закрытом месяце система не переписывает молча — но и промолчать
                          нельзя: у СДЭК так потерялись 91,50 ₽ расхода. */}
                      {item.amount_mismatch !== 0 ? (
                        <div className="mt-1 text-xs text-amber-700">
                          Документ на {money.format(item.document_amount ?? 0)} — расход признан на{" "}
                          {money.format(item.amount)}, разница{" "}
                          {money.format(Math.abs(item.amount_mismatch))}
                        </div>
                      ) : null}
                    </TableCell>
                    <TableCell className="text-sm">
                      {item.period_assumed ? (
                        <span className="text-muted-foreground">Период не указан</span>
                      ) : (
                        formatPeriod(item.service_period_start, item.service_period_end)
                      )}
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {item.article_name ?? "—"}
                    </TableCell>
                    <TableCell className="text-sm">
                      {item.settled ? (
                        <span className="text-muted-foreground">закрыт документом</span>
                      ) : item.stage === "waiting_document" ? (
                        item.days_overdue > 0 ? (
                          <Badge
                            variant="outline"
                            className={
                              item.days_overdue > 30
                                ? "border-rose-200 bg-rose-50 text-rose-700"
                                : "border-amber-200 bg-amber-50 text-amber-800"
                            }
                          >
                            нет {item.days_overdue} дн
                          </Badge>
                        ) : (
                          <span className="text-muted-foreground">
                            ждём до {fmtDate(item.expected_by)}
                          </span>
                        )
                      ) : item.auto_recognition_on ? (
                        // Договор или аренда: расход придёт сам, и человеку важно знать —
                        // ждать до завтра или до сентября. «Период идёт» на это не отвечает.
                        <span className="text-muted-foreground">
                          начислится {fmtDate(item.auto_recognition_on)}
                        </span>
                      ) : item.recognition_month ? (
                        <span className="text-muted-foreground">
                          {monthTitle(item.recognition_month.slice(0, 7))}
                        </span>
                      ) : item.service_period_end ? (
                        <span className="text-muted-foreground">
                          после {fmtDate(item.service_period_end)}
                        </span>
                      ) : (
                        "—"
                      )}
                    </TableCell>
                    <TableCell className="text-right font-semibold tabular-nums">
                      {/* У признанного и оплаченного остаток равен нулю, и строка показывала
                          «0 ₽» под плиткой с суммой расхода. В этом состоянии величина строки —
                          сам расход. */}
                      {money.format(
                        item.stage === "in_expense" ? item.amount : item.balance_amount,
                      )}
                      {/* Частично закрытый платёж: в строке остаток, а подписана она платежом.
                          Манго платится по 5 000 ₽, УПД приходят на фактический объём — и
                          строка «Платёж от 26.06 — 3 891 ₽» читалась как платёж, которого в
                          выписке нет. Разницу называем вслух. */}
                      {item.stage !== "in_expense" &&
                      item.balance_amount > 0 &&
                      item.amount - item.balance_amount > 0.005 ? (
                        <div className="mt-0.5 text-xs font-normal text-muted-foreground">
                          из {money.format(item.amount)} закрыто{" "}
                          {money.format(item.amount - item.balance_amount)}
                        </div>
                      ) : null}
                    </TableCell>
                    <TableCell className="text-right">
                      {/* «За что заплатили» — тот же вопрос, что решает окно разбора на
                          «Странице на оплату». Здесь он даже острее: строка признания живёт
                          отдельно от документа, и проверить основание было негде. */}
                      {isPrepayment ? (
                        <Button
                          className="mr-1 h-8 px-2 text-xs"
                          onClick={() => setOrigin(item)}
                          size="sm"
                          variant="ghost"
                        >
                          Основание
                        </Button>
                      ) : null}
                      {!canEdit ? null : isPrepayment ? (
                        item.can_recognize ? (
                          <Button size="sm" variant="outline" onClick={() => setRecognizing(item)}>
                            Признать расход
                          </Button>
                        ) : (
                          <span className="text-xs text-muted-foreground">
                            {item.recognize_blocked_reason}
                          </span>
                        )
                      ) : (
                        <div className="flex items-center justify-end gap-1">
                          {canReverse && item.stage === "in_expense" ? (
                            <Button
                              size="sm"
                              variant="ghost"
                              className="text-xs"
                              title="Снять расход целиком или частью — сумма вернётся в дебиторку"
                              onClick={() => setReversing(item)}
                            >
                              Откатить
                            </Button>
                          ) : null}
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
                        </div>
                      )}
                    </TableCell>
                  </TableRow>
                );
              })
            )}
          </TableBody>
        </Table>
      </div>

      {editing ? <PeriodDialog item={editing} onClose={() => setEditing(null)} /> : null}
      {recognizing ? (
        <RecognizeDialog item={recognizing} onClose={() => setRecognizing(null)} />
      ) : null}
      {origin ? <OriginDialog item={origin} onClose={() => setOrigin(null)} /> : null}
      {reversing ? (
        <ReverseDialog item={reversing} onClose={() => setReversing(null)} />
      ) : null}
    </div>
  );
}

/** Пустая очередь — это хорошая новость, и сказать её надо словами, а не прочерком. */
const EMPTY_STAGE: Record<Stage, string> = {
  needs_period: "Решать нечего: по всем платежам понятно, за что они",
  waiting_document: "Все документы получены",
  period_running: "Нет услуг с идущим периодом",
  in_expense: "В этом месяце расходов по услугам ещё не признано",
};

function RecognizeDialog({ item, onClose }: { item: AccountingItem; onClose: () => void }) {
  const queryClient = useQueryClient();
  // Период не подставляем: угаданный по месяцу платежа, он выглядит как подтверждённый, и
  // человек подтвердит его не глядя. Пусть скажет сам — это единственное, что он тут решает.
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [articleId, setArticleId] = useState("");
  // Платежи из банковской выписки часто приходят без статьи ДДС, а без неё расход некуда
  // отнести. Спрашиваем здесь же, чтобы не отправлять человека размечать проводку в ДДС и
  // возвращаться обратно.
  const needsArticle = !item.article_id;
  const articlesQuery = useQuery({
    queryKey: ["cp", "expense-articles"],
    queryFn: getExpenseArticles,
    enabled: needsArticle,
  });
  const mutation = useMutation({
    mutationFn: () =>
      recognizePrepayment(item.id, {
        service_period_start: start,
        service_period_end: end,
        dds_article_id: needsArticle ? articleId : null,
      }),
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({ queryKey: ["accounting"] });
      toast.success(
        result.months_recognized === result.period_months
          ? `Расход признан за ${result.period_months} мес.`
          : `Признано ${result.months_recognized} из ${result.period_months} мес.: последний ещё не закончился`,
      );
      onClose();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось признать расход")),
  });
  const ready = Boolean(start && end && end >= start && (!needsArticle || articleId));

  return (
    <Dialog open onOpenChange={(open) => (!open ? onClose() : undefined)}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>За какой период эта услуга?</DialogTitle>
          <DialogDescription>
            {item.counterparty_name} · {moneyExact.format(item.balance_amount)}
            {item.payment_date ? ` · платёж от ${fmtDate(item.payment_date)}` : ""}
          </DialogDescription>
        </DialogHeader>
        <p className="rounded-md border bg-muted/40 p-3 text-sm text-muted-foreground">
          Сумма разложится по месяцам периода и попадёт в прибыль каждого из них. Месяц, который
          ещё не закончился, признаётся сам в первую ночь следующего.
        </p>
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
        {needsArticle ? (
          <div className="grid gap-1.5">
            <Label>Статья расхода *</Label>
            <ArticleCombobox
              articles={articlesQuery.data ?? []}
              value={articleId}
              onChange={setArticleId}
            />
            <p className="text-xs text-muted-foreground">
              У этого платежа статья не указана — без неё расход не разнести по отчёту.
            </p>
          </div>
        ) : null}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Отмена
          </Button>
          <Button disabled={!ready || mutation.isPending} onClick={() => mutation.mutate()}>
            Признать расход
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ReverseDialog({ item, onClose }: { item: AccountingItem; onClose: () => void }) {
  const queryClient = useQueryClient();
  // Сумму не подставляем целиком: откат чаще частичный, а предзаполненная полная сумма
  // подталкивает снять весь расход одним нажатием.
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const value = Number(amount.replace(",", "."));
  const mutation = useMutation({
    mutationFn: () => reverseExpense(item.id, { amount: value, reason: reason.trim() }),
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({ queryKey: ["accounting"] });
      toast.success(
        result.fully_cancelled
          ? "Расход снят целиком, деньги вернулись в дебиторку"
          : `Снято ${money.format(result.reversed_amount)}, в расходе осталось ${money.format(result.amount_left)}`,
      );
      onClose();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось откатить расход")),
  });
  const ready = value > 0 && value <= item.amount && Boolean(reason.trim());

  return (
    <Dialog open onOpenChange={(open) => (!open ? onClose() : undefined)}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Откатить расход</DialogTitle>
          <DialogDescription>
            {item.counterparty_name} · в расходе {moneyExact.format(item.amount)}
            {item.recognition_month ? ` · ${monthTitle(item.recognition_month.slice(0, 7))}` : ""}
          </DialogDescription>
        </DialogHeader>
        <div className="flex gap-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
          <AlertTriangle className="mt-0.5 shrink-0" size={17} />
          Сумма уйдёт из прибыли этого месяца и вернётся в дебиторку — контрагент снова будет
          должен закрыть её документами или вернуть деньги.
        </div>
        <div className="grid gap-1.5">
          <Label>Сколько снять, ₽</Label>
          <Input
            inputMode="decimal"
            value={amount}
            placeholder={String(item.amount)}
            onChange={(event) => setAmount(event.target.value)}
          />
          <button
            type="button"
            className="justify-self-start text-xs text-muted-foreground underline"
            onClick={() => setAmount(String(item.amount))}
          >
            снять весь расход
          </button>
        </div>
        <div className="grid gap-1.5">
          <Label>Причина *</Label>
          <Textarea
            value={reason}
            placeholder="Например: услуга оказана половину месяца"
            onChange={(event) => setReason(event.target.value)}
          />
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Отмена
          </Button>
          <Button disabled={!ready || mutation.isPending} onClick={() => mutation.mutate()}>
            Откатить
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
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

function ExpensesByMonthSection() {
  // Полугодовое окно по умолчанию: меньше — не видно сезонности, больше — таблица перестаёт
  // помещаться на экран, а листать её горизонтально ради цифры за прошлый год незачем.
  const today = new Date();
  const to = new Date(today.getFullYear(), today.getMonth() + 1, 0);
  const from = new Date(today.getFullYear(), today.getMonth() - 5, 1);
  const iso = (value: Date) =>
    `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(
      value.getDate(),
    ).padStart(2, "0")}`;
  const dateFrom = iso(from);
  const dateTo = iso(to);

  const query = useQuery({
    queryKey: ["accounting", "expenses-by-month", dateFrom, dateTo],
    queryFn: () => getExpensesByMonth(dateFrom, dateTo),
  });

  const months = query.data?.months ?? [];
  // Строка = статья, колонка = месяц. Именно так расход и читают: «сколько мы тратим на
  // лицензии» — вопрос про строку, «что было в июле» — вопрос про колонку.
  const byArticle = new Map<string, { name: string; cells: Map<string, number>; total: number }>();
  for (const cell of query.data?.items ?? []) {
    const key = cell.article_id ?? "none";
    const row = byArticle.get(key) ?? { name: cell.article_name, cells: new Map(), total: 0 };
    row.cells.set(cell.month, (row.cells.get(cell.month) ?? 0) + cell.amount);
    row.total += cell.amount;
    byArticle.set(key, row);
  }
  const rows = [...byArticle.values()].sort((a, b) => b.total - a.total);

  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs text-muted-foreground">
        Признанный расход по месяцам оказания услуги, а не по дате документа: акт за квартал
        раскладывается на три месяца. В расход идёт только признанное — запланированное и
        отменённое сюда не попадают.
      </p>
      {query.data && query.data.without_primary > 0 ? (
        <p className="text-xs text-amber-700">
          Из них {money.format(query.data.without_primary)} признано без первичного документа
          (самоакты и платежи без УПД) — в налоговую базу УСН такой расход не идёт.
        </p>
      ) : null}
      {query.data && query.data.unattributed > 0 ? (
        <p className="text-xs text-rose-700">
          {money.format(query.data.unattributed)} без статьи ДДС — отнести в отчёт о прибыли
          некуда, пока статья не проставлена.
        </p>
      ) : null}
      {query.data && query.data.without_location > 0 ? (
        <p className="text-xs text-muted-foreground">
          {money.format(query.data.without_location)} без помещения — в разрез по точкам такой
          расход не попадёт. У документа поставщика помещения нет, и подставлять его наугад
          нельзя: выдуманная цифра выглядит достоверно и расходится с реальностью молча.
        </p>
      ) : null}
      <div className="overflow-x-auto rounded-lg border bg-background">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Статья</TableHead>
              {months.map((month) => (
                <TableHead key={month} className="text-right whitespace-nowrap">
                  {monthTitle(month.slice(0, 7))}
                </TableHead>
              ))}
              <TableHead className="text-right">Итого</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.isLoading ? (
              <TableStatus colSpan={months.length + 2} state="loading" />
            ) : query.isError ? (
              <TableStatus
                colSpan={months.length + 2}
                state={apiErrorMessage(query.error, "Не удалось загрузить расход по месяцам")}
              />
            ) : rows.length === 0 ? (
              <TableStatus colSpan={months.length + 2} state="empty" tone="calm" />
            ) : (
              <>
                {rows.map((row) => (
                  <TableRow key={row.name}>
                    <TableCell className="font-medium">{row.name}</TableCell>
                    {months.map((month) => (
                      <TableCell key={month} className="text-right tabular-nums">
                        {row.cells.has(month) ? money.format(row.cells.get(month) ?? 0) : "—"}
                      </TableCell>
                    ))}
                    <TableCell className="text-right font-semibold tabular-nums">
                      {money.format(row.total)}
                    </TableCell>
                  </TableRow>
                ))}
                <TableRow>
                  <TableCell className="font-semibold">Всего</TableCell>
                  {months.map((month) => (
                    <TableCell key={month} className="text-right font-semibold tabular-nums">
                      {money.format(
                        rows.reduce((sum, row) => sum + (row.cells.get(month) ?? 0), 0),
                      )}
                    </TableCell>
                  ))}
                  <TableCell className="text-right font-semibold tabular-nums">
                    {money.format(query.data?.total ?? 0)}
                  </TableCell>
                </TableRow>
              </>
            )}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}

function BalanceAsOfCard() {
  // Свёрнута по умолчанию: обычный вопрос — «сколько должны сейчас», и таблица выше на него
  // уже отвечает. «На дату» нужна в конце месяца, когда собирают баланс, — тогда её и открывают.
  const [open, setOpen] = useState(false);
  const [asOf, setAsOf] = useState(() => {
    const today = new Date();
    const lastMonthEnd = new Date(today.getFullYear(), today.getMonth(), 0);
    return `${lastMonthEnd.getFullYear()}-${String(lastMonthEnd.getMonth() + 1).padStart(2, "0")}-${String(
      lastMonthEnd.getDate(),
    ).padStart(2, "0")}`;
  });

  const query = useQuery({
    queryKey: ["accounting", "balances-as-of", asOf],
    queryFn: () => getBalancesAsOf(asOf),
    enabled: open,
  });

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="self-start text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground"
      >
        Показать остатки на дату (для баланса на конец месяца)
      </button>
    );
  }

  return (
    <div className="flex flex-col gap-2 rounded-lg border bg-background p-3">
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-sm font-medium">Остатки на дату</span>
        <Input
          type="date"
          value={asOf}
          onChange={(event) => setAsOf(event.target.value)}
          className="max-w-[11rem]"
        />
        <button
          type="button"
          onClick={() => setOpen(false)}
          className="text-xs text-muted-foreground underline underline-offset-2"
        >
          свернуть
        </button>
      </div>
      <p className="text-xs text-muted-foreground">
        Кто сколько был должен на конец выбранного дня. Отличается от таблицы выше: документ,
        оплаченный позже, здесь ещё остаётся долгом — именно так собирается баланс на конец
        месяца.
      </p>
      {query.data && query.data.approximate_settlements > 0 ? (
        <p className="text-xs text-amber-700">
          {money.format(query.data.approximate_settlements)} гашений учтены по дате записи, а не
          по дате события: у бартерных зачётов денежного факта нет. На эту величину остаток
          прошедшей даты может отличаться от истинного.
        </p>
      ) : null}
      {query.isLoading ? (
        <p className="text-xs text-muted-foreground">Считаем…</p>
      ) : query.isError ? (
        <p className="text-xs text-rose-700">
          {apiErrorMessage(query.error, "Не удалось посчитать остатки на дату")}
        </p>
      ) : (
        <div className="overflow-hidden rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Контрагент</TableHead>
                <TableHead className="text-right">Дебиторка</TableHead>
                <TableHead className="text-right">Кредиторка</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {(query.data?.items.length ?? 0) === 0 ? (
                <TableStatus colSpan={3} state="empty" tone="calm" />
              ) : (
                <>
                  {query.data?.items.map((row) => (
                    <TableRow key={row.counterparty_id}>
                      <TableCell>{row.counterparty_name}</TableCell>
                      <TableCell className="text-right tabular-nums">
                        {row.receivable ? money.format(row.receivable) : "—"}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {row.payable ? money.format(row.payable) : "—"}
                      </TableCell>
                    </TableRow>
                  ))}
                  <TableRow>
                    <TableCell className="font-semibold">Итого</TableCell>
                    <TableCell className="text-right font-semibold tabular-nums">
                      {money.format(query.data?.receivable_total ?? 0)}
                    </TableCell>
                    <TableCell className="text-right font-semibold tabular-nums">
                      {money.format(query.data?.payable_total ?? 0)}
                    </TableCell>
                  </TableRow>
                </>
              )}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}

function OriginDialog({ item, onClose }: { item: AccountingItem; onClose: () => void }) {
  const query = useQuery({
    queryKey: ["accounting", "origin", item.id],
    queryFn: () => getRecognitionOrigin(item.id),
  });
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const [pdfFor, setPdfFor] = useState<string | null>(null);

  // PDF грузим по требованию: у одной строки бывает два документа (счёт и УПД), и тянуть оба
  // сразу — лишний трафик ради того, на что человек, возможно, и не посмотрит.
  const openPdf = (intakeId: string) => {
    if (pdfFor === intakeId) return;
    setPdfFor(intakeId);
    setPdfUrl(null);
    fetchOriginPdfUrl(intakeId)
      .then(setPdfUrl)
      .catch(() => setPdfUrl(null));
  };

  const card = (doc: OriginDocument | null, note: string, title: string) => (
    <div className="rounded-lg border bg-background p-3">
      <div className="text-xs font-medium uppercase text-muted-foreground">{title}</div>
      <div className="mt-1 text-sm">{note}</div>
      {doc ? (
        <div className="mt-2 flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
          {doc.invoice_date ? <span>от {fmtDate(doc.invoice_date)}</span> : null}
          {doc.amount != null ? <span>{money.format(doc.amount)}</span> : null}
          {doc.has_pdf && doc.intake_id ? (
            <button
              type="button"
              className="underline underline-offset-2 hover:text-foreground"
              onClick={() => openPdf(doc.intake_id as string)}
            >
              {pdfFor === doc.intake_id ? "показан ниже" : "открыть PDF"}
            </button>
          ) : (
            <span>файла нет — документ заведён вручную</span>
          )}
        </div>
      ) : null}
    </div>
  );

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>Основание платежа</DialogTitle>
          <DialogDescription>
            {query.data
              ? `${query.data.counterparty_name} · ${money.format(query.data.amount)}`
              : item.counterparty_name}
          </DialogDescription>
        </DialogHeader>

        {query.isLoading ? (
          <p className="text-sm text-muted-foreground">Загружаем…</p>
        ) : query.isError ? (
          <p className="text-sm text-rose-700">
            {apiErrorMessage(query.error, "Не удалось загрузить основание")}
          </p>
        ) : query.data ? (
          <div className="flex flex-col gap-3">
            {card(query.data.basis, query.data.basis_note, "За что заплатили")}
            {card(query.data.closing, query.data.closing_note, "Чем закрыто")}
            {pdfFor ? (
              pdfUrl ? (
                <iframe title="Документ" src={pdfUrl} className="h-[55vh] w-full rounded-md border" />
              ) : (
                <p className="text-sm text-muted-foreground">Загружаем PDF…</p>
              )
            ) : null}
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
