/** Страница «Отчёты → ОПиУ».
 *
 * ГЛАВНОЕ ПРАВИЛО ЭКРАНА: пустая ячейка НИКОГДА не рисуется нулём. Ноль печатается только
 * когда источник ответил и движения не было; во всех остальных случаях стоит бейдж —
 * «нет данных», «ждём документ», «не настроено». Различие приходит из контракта API, а не
 * решается здесь: если бы страница выбирала сама, она рано или поздно выбрала бы ноль.
 *
 * Подытог, у которого хоть одно слагаемое неизвестно, показывается со знаком «≈» и подписью,
 * каких источников не хватает. В июле 2026 без выручки EBITDA обязана читаться как «неполно»,
 * а не как убыток в четыре миллиона.
 *
 * Контрол периода — стрелки и выпадающий список, БЕЗ `<input type="month">`: Safari его не
 * поддерживает, и на этом уже терялся период платежа в другом окне.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { apiErrorMessage } from "@/lib/api";

import {
  fetchGoodsClassifications,
  fetchGoodsLedger,
  fetchPartnerCommissionLedger,
  fetchPayrollLedger,
  fetchPnlLineDrill,
  fetchPnlReport,
  fetchRecognitionLedger,
  updateGoodsClassification,
  type GoodsClassificationDecision,
  type GoodsClassificationLedger,
  type GoodsClassificationRow,
  type GoodsLedger,
  type GoodsSourceKind,
  type LineStatus,
  type PartnerCommissionLedger,
  type PayrollLedger,
  type PnlDrill,
  type PnlLine,
  type PnlReport,
  type RecognitionLedger,
} from "./api";

const MONTH_NAMES = [
  "Январь",
  "Февраль",
  "Март",
  "Апрель",
  "Май",
  "Июнь",
  "Июль",
  "Август",
  "Сентябрь",
  "Октябрь",
  "Ноябрь",
  "Декабрь",
];

/** Первый месяц управленческого учёта. Раньше данные в приложении неполны. */
const FIRST_MONTH = "2026-07";

function monthLabel(month: string): string {
  const [year, monthNumber] = month.split("-").map(Number);
  return `${MONTH_NAMES[monthNumber - 1]} ${year}`;
}

function shiftMonth(month: string, direction: -1 | 1): string {
  const [year, monthNumber] = month.split("-").map(Number);
  const next = new Date(Date.UTC(year, monthNumber - 1 + direction, 1));
  return `${next.getUTCFullYear()}-${String(next.getUTCMonth() + 1).padStart(2, "0")}`;
}

function availableMonths(): string[] {
  const months: string[] = [];
  let cursor = FIRST_MONTH;
  const now = new Date();
  const limit = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
  while (cursor <= limit) {
    months.push(cursor);
    cursor = shiftMonth(cursor, 1);
  }
  return months.reverse();
}

/** Деньги с разделителем разрядов. Строку не парсим в число ради арифметики — только для показа. */
function formatMoney(value: string): string {
  const amount = Number(value);
  if (!Number.isFinite(amount)) return value;
  return amount.toLocaleString("ru-RU", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatPercent(value: string | null): string {
  if (value === null) return "";
  const ratio = Number(value);
  if (!Number.isFinite(ratio)) return "";
  return `${(ratio * 100).toLocaleString("ru-RU", { maximumFractionDigits: 1 })}%`;
}

const STATUS_LABEL: Partial<Record<LineStatus, string>> = {
  no_data: "нет данных",
  not_configured: "не настроено",
  waiting_document: "ждём документ",
  overdue_document: "документ просрочен",
  needs_review: "требует проверки",
  not_used: "не используется",
  before_accounting_start: "учёт не вёлся",
  manual: "вручную",
  incomplete: "неполно",
  ok: "",
  zero_confirmed: "",
};

const STATUS_VARIANT: Partial<Record<LineStatus, "secondary" | "outline" | "destructive">> = {
  incomplete: "outline",
  overdue_document: "destructive",
  needs_review: "destructive",
  waiting_document: "outline",
  manual: "outline",
};

function LineAmount({ line }: { line: PnlLine }) {
  if (line.kind === "ratio") {
    // «≈» переносится и на рентабельность: она посчитана от неполного числителя, и знак
    // оговорки должен стоять там же, где стоит у самой суммы.
    return (
      <span className="tabular-nums">
        {line.status === "incomplete" && line.amount !== null ? "≈ " : ""}
        {formatPercent(line.amount)}
      </span>
    );
  }
  if (line.amount === null) {
    const label = STATUS_LABEL[line.status] || line.status;
    return (
      <Badge variant={STATUS_VARIANT[line.status] ?? "secondary"} className="font-normal">
        {label}
      </Badge>
    );
  }
  const incomplete = line.status === "incomplete";
  return (
    <span className="tabular-nums" title={incomplete ? "Сумма известных слагаемых" : undefined}>
      {incomplete ? "≈ " : ""}
      {formatMoney(line.amount)}
    </span>
  );
}

function ReportTable({
  report,
  onOpenLine,
}: {
  report: PnlReport;
  onOpenLine: (code: string) => void;
}) {
  const visible = report.lines.filter((line) => line.kind !== "service");
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="sticky top-0 bg-background">
          <tr className="border-b text-muted-foreground">
            <th className="py-2 text-left font-medium">Строка</th>
            <th className="py-2 text-right font-medium w-40">Сумма, ₽</th>
            <th className="py-2 text-right font-medium w-24">% выручки</th>
            <th className="py-2 text-left font-medium w-56 pl-4">Источник</th>
          </tr>
        </thead>
        <tbody>
          {visible.map((line) => {
            const isTotal = line.kind === "subtotal";
            const isRatio = line.kind === "ratio";
            // Расшифровка есть у строк с источниками. Подытог раскрывать нечем — он и так
            // виден слагаемыми, стоящими под ним в таблице.
            const openable = line.drill_available && !isTotal && !isRatio;
            return (
              <tr
                key={line.code}
                className={[
                  "border-b border-muted/40",
                  isTotal ? "bg-muted/40 font-semibold" : "",
                  isRatio ? "text-muted-foreground" : "",
                  openable ? "cursor-pointer hover:bg-muted/50" : "",
                ].join(" ")}
                onClick={openable ? () => onOpenLine(line.code) : undefined}
                tabIndex={openable ? 0 : undefined}
                onKeyDown={
                  openable
                    ? (event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          onOpenLine(line.code);
                        }
                      }
                    : undefined
                }
              >
                <td className="py-1.5" style={{ paddingLeft: `${line.level * 16}px` }}>
                  {line.title}
                  {openable && (
                    <span className="ml-1.5 text-xs text-muted-foreground/60" aria-hidden>
                      ›
                    </span>
                  )}
                  {line.status === "incomplete" && line.missing_lines.length > 0 && (
                    <span className="ml-2 text-xs font-normal text-amber-600">
                      неполно: нет {line.missing_lines.length} источников
                    </span>
                  )}
                </td>
                <td className="py-1.5 text-right">
                  <LineAmount line={line} />
                </td>
                <td className="py-1.5 text-right tabular-nums text-muted-foreground">
                  {!isRatio && formatPercent(line.pct_of_revenue)}
                </td>
                <td className="py-1.5 pl-4 text-xs text-muted-foreground">
                  {line.components
                    .filter((component) => component.amount !== null)
                    .map((component) => component.stream)
                    .join(" + ")}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

const STREAM_TITLE: Record<string, string> = {
  recognition: "признание",
  cashflow: "касса",
  cashflow_excluded: "касса",
  waiting: "ДЗ/КЗ",
  unperiodled: "ДЗ/КЗ",
  acquiring: "выписка",
  payroll: "зарплата",
  iiko: "iiko",
  inventory: "ревизии",
  manual: "вручную",
};

/** «1 операция», «2 операции», «5 операций» — иначе подпись читается как машинный вывод.
 *  Слово нейтральное: за пометкой стоят и платежи, и списания фонда. */
function operationsWord(count: number): string {
  const tens = count % 100;
  if (tens >= 11 && tens <= 14) return "операций";
  switch (count % 10) {
    case 1:
      return "операция";
    case 2:
    case 3:
    case 4:
      return "операции";
    default:
      return "операций";
  }
}

function formatRowDate(value: string | null): string {
  if (!value) return "";
  const [year, month, day] = value.split("-");
  return `${day}.${month}.${year.slice(2)}`;
}

/** Расшифровка строки: кто и на сколько её сформировал.
 *
 * Группы «не в итоге» показываются наравне с остальными и НЕ приглушаются: именно они
 * отвечают на вопрос, с которым сюда чаще всего приходят, — «я заплатил, где эти деньги».
 */
function DrillPanel({
  drill,
  loading,
  error,
}: {
  drill: PnlDrill | null;
  loading: boolean;
  error: string | null;
}) {
  if (loading) {
    return (
      <div className="mt-6 space-y-2">
        <Skeleton className="h-5 w-2/3" />
        <Skeleton className="h-5 w-full" />
        <Skeleton className="h-5 w-1/2" />
      </div>
    );
  }
  if (error) {
    return <p className="mt-6 text-sm text-destructive">{error}</p>;
  }
  if (!drill) return null;
  if (drill.groups.length === 0) {
    return (
      <p className="mt-6 text-sm text-muted-foreground">
        {drill.undecomposed[0] ?? "Расшифровки у этой строки нет."}
      </p>
    );
  }

  return (
    <div className="mt-6 space-y-6">
      {drill.groups.map((group) => (
        <div key={`${group.stream}-${group.title}`}>
          <div className="flex items-baseline justify-between gap-3 border-b pb-1">
            <div className="text-sm font-medium">
              {group.title}
              <span className="ml-2 text-xs font-normal text-muted-foreground">
                {STREAM_TITLE[group.stream] ?? group.stream}
              </span>
            </div>
            <div className="tabular-nums text-sm font-medium">{formatMoney(group.amount)} ₽</div>
          </div>
          {group.note && <p className="mt-1 text-xs text-muted-foreground">{group.note}</p>}
          <table className="mt-2 w-full text-sm">
            <tbody>
              {group.rows.map((row, index) => (
                <tr key={`${row.title}-${index}`} className="border-b border-muted/30">
                  <td className="w-16 py-1 text-xs tabular-nums text-muted-foreground">
                    {formatRowDate(row.row_date)}
                  </td>
                  <td className="py-1">
                    {row.title}
                    {row.subtitle && (
                      <div className="text-xs text-muted-foreground">{row.subtitle}</div>
                    )}
                  </td>
                  <td className="w-32 py-1 text-right tabular-nums">{formatMoney(row.amount)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
      <div className="flex justify-between border-t pt-2 text-sm font-semibold">
        <span>Итого в строке</span>
        <span className="tabular-nums">{formatMoney(drill.total)} ₽</span>
      </div>
      {drill.asides.map((aside, index) => (
        <p key={index} className="text-xs text-muted-foreground">
          Ещё {formatMoney(aside.amount)} ₽ ({aside.count} {operationsWord(aside.count)}) —{" "}
          {aside.reason}.
        </p>
      ))}
    </div>
  );
}

function Reconciliation({ report }: { report: PnlReport }) {
  const { reconciliation } = report;
  const ok = reconciliation.balanced;
  return (
    <Card className={`p-4 ${ok ? "border-emerald-300" : "border-amber-300"}`}>
      <div className="text-sm font-medium">
        Сходимость денежного слоя: отток {formatMoney(reconciliation.cash_out_total)} ₽
      </div>
      <div className="mt-2 grid gap-1 text-xs text-muted-foreground sm:grid-cols-2">
        {Object.entries(reconciliation.by_verdict)
          .sort((a, b) => Number(b[1]) - Number(a[1]))
          .map(([verdict, amount]) => (
            <div key={verdict} className="flex justify-between gap-4">
              <span>{VERDICT_LABEL[verdict] ?? verdict}</span>
              <span className="tabular-nums">{formatMoney(amount)}</span>
            </div>
          ))}
      </div>
      <div className={`mt-2 text-xs ${ok ? "text-emerald-700" : "text-amber-700"}`}>
        {ok
          ? "Каждый рубль разложен по строкам и исключениям."
          : `Не разнесено: ${formatMoney(reconciliation.unmapped)} ₽ (${reconciliation.unmapped_count} проводок) — отчёт неполон ровно на эту сумму.`}
      </div>
    </Card>
  );
}

const VERDICT_LABEL: Record<string, string> = {
  included: "в отчёте",
  excluded_out_of_pnl: "вне ОПиУ (переводы, займы, чужой слой)",
  excluded_accrual_counterparty: "заменено начислением",
  excluded_accrual_settlement: "оплата признанного расхода",
  excluded_owned_by_layer: "посчитано другим модулем",
  excluded_quality: "исключено при разборе выписки",
  excluded_transfer: "перевод между счетами",
  unmapped: "не разнесено",
};

type PnlTab = "summary" | "payroll" | "partners" | "goods" | "recognition";

function formatDate(value: string | null): string {
  if (!value) return "—";
  const [year, month, day] = value.slice(0, 10).split("-");
  return `${day}.${month}.${year}`;
}

function formatPeriod(start: string | null, end: string | null): string {
  if (!start || !end) return "—";
  if (start === end) return formatDate(start);
  return `${formatDate(start)} — ${formatDate(end)}`;
}

function MetricCard({
  title,
  amount,
  note,
}: {
  title: string;
  amount: string | null;
  note?: string;
}) {
  return (
    <Card className="p-4">
      <div className="text-xs text-muted-foreground">{title}</div>
      <div className="mt-1 text-lg font-semibold tabular-nums">
        {amount === null ? "нет данных" : `${formatMoney(amount)} ₽`}
      </div>
      {note ? <div className="mt-1 text-xs text-muted-foreground">{note}</div> : null}
    </Card>
  );
}

function LedgerMessage({ children }: { children: string }) {
  return <Card className="p-4 text-sm text-muted-foreground">{children}</Card>;
}

function useLedger<T>(
  enabled: boolean,
  month: string,
  loader: (target: string) => Promise<T>,
): {
  data: T | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
  replace: (value: T) => void;
} {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const reload = useCallback(() => setRevision((value) => value + 1), []);
  const replace = useCallback((value: T) => setData(value), []);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    loader(month)
      .then((value) => {
        if (!cancelled) setData(value);
      })
      .catch((cause: unknown) => {
        if (cancelled) return;
        setData(null);
        setError(cause instanceof Error ? cause.message : "Не удалось загрузить леджер");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, loader, month, revision]);

  return { data, loading, error, reload, replace };
}

function PayrollLedgerView({ ledger }: { ledger: PayrollLedger }) {
  const totals = ledger.totals;
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <MetricCard title="Смены и оклады" amount={totals.base_pay} />
        <MetricCard title="Процент с выручки" amount={totals.percent_pay} />
        <MetricCard title="Премии" amount={totals.bonuses} />
        <MetricCard title="Отпускные" amount={totals.vacation_pay} />
        <MetricCard
          title="Накопительный фонд"
          amount={totals.fund_expense}
          note="начислено минус списано"
        />
        <MetricCard
          title="Прочие штрафы"
          amount={totals.other_penalties}
          note="уменьшают зарплатный расход"
        />
        <MetricCard
          title="Штрафы по ревизиям"
          amount={totals.audit_penalties}
          note="уменьшают результат ревизий"
        />
        <MetricCard
          title="Списанные депозиты"
          amount={totals.deposit_written_off}
          note="доход ниже EBITDA"
        />
      </div>
      {ledger.employees.length === 0 ? (
        <LedgerMessage>За выбранный месяц нет финализированных начислений.</LedgerMessage>
      ) : (
        <Card className="overflow-x-auto p-4">
          <table className="w-full min-w-[1180px] text-sm">
            <thead>
              <tr className="border-b text-left text-xs text-muted-foreground">
                <th className="pb-2 font-medium">Сотрудник</th>
                <th className="pb-2 font-medium">Роли</th>
                <th className="pb-2 text-right font-medium">Оклад / смены</th>
                <th className="pb-2 text-right font-medium">Процент</th>
                <th className="pb-2 text-right font-medium">Премии</th>
                <th className="pb-2 text-right font-medium">Отпускные</th>
                <th className="pb-2 text-right font-medium">Фонд</th>
                <th className="pb-2 text-right font-medium">Штрафы</th>
                <th className="pb-2 text-right font-medium">Ревизии</th>
                <th className="pb-2 text-right font-medium">Зарплатный расход</th>
              </tr>
            </thead>
            <tbody>
              {ledger.employees.map((row) => (
                <tr key={row.employee_id ?? row.employee_name} className="border-b border-muted/40">
                  <td className="py-2 font-medium">{row.employee_name}</td>
                  <td className="py-2 text-xs text-muted-foreground">
                    {row.roles.join(", ") || "—"}
                  </td>
                  <td className="py-2 text-right tabular-nums">{formatMoney(row.base_pay)}</td>
                  <td className="py-2 text-right tabular-nums">{formatMoney(row.percent_pay)}</td>
                  <td className="py-2 text-right tabular-nums">{formatMoney(row.bonuses)}</td>
                  <td className="py-2 text-right tabular-nums">{formatMoney(row.vacation_pay)}</td>
                  <td className="py-2 text-right tabular-nums">{formatMoney(row.fund_expense)}</td>
                  <td className="py-2 text-right tabular-nums">
                    {formatMoney(row.other_penalties)}
                  </td>
                  <td className="py-2 text-right tabular-nums">
                    {formatMoney(row.audit_penalties)}
                  </td>
                  <td className="py-2 text-right font-medium tabular-nums">
                    {formatMoney(row.salary_expense)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}

function PartnerCommissionLedgerView({ ledger }: { ledger: PartnerCommissionLedger }) {
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <MetricCard
          title="Выручка партнёров"
          amount={ledger.revenue_amount}
          note="Сумма со скидкой из OLAP iiko"
        />
        <MetricCard
          title="Комиссия партнёрам"
          amount={ledger.commission_amount}
          note="Выручка × ставка партнёра"
        />
      </div>
      {ledger.rows.length === 0 ? (
        <LedgerMessage>За выбранный месяц OLAP-расчёт партнёров ещё не загружен.</LedgerMessage>
      ) : (
        <Card className="overflow-x-auto p-4">
          <table className="w-full min-w-[760px] text-sm">
            <thead>
              <tr className="border-b text-left text-xs text-muted-foreground">
                <th className="pb-2 font-medium">Партнёр</th>
                <th className="pb-2 text-right font-medium">Выручка со скидкой</th>
                <th className="pb-2 text-right font-medium">Ставка</th>
                <th className="pb-2 text-right font-medium">Комиссия</th>
                <th className="pb-2 text-right font-medium">Строк OLAP</th>
              </tr>
            </thead>
            <tbody>
              {ledger.rows.map((row) => (
                <tr
                  key={`${row.partner_name}-${row.source_ref}`}
                  className="border-b border-muted/40"
                >
                  <td className="py-3 font-medium">{row.partner_name}</td>
                  <td className="py-3 text-right tabular-nums">
                    {formatMoney(row.revenue_amount)} ₽
                  </td>
                  <td className="py-3 text-right tabular-nums">
                    {formatPercent(row.commission_rate)}
                  </td>
                  <td className="py-3 text-right font-medium tabular-nums">
                    {formatMoney(row.commission_amount)} ₽
                  </td>
                  <td className="py-3 text-right tabular-nums">{row.rows_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}

const GOODS_SOURCE_LABEL: Record<GoodsSourceKind | "temporary", string> = {
  inventory: "Складской учёт iiko",
  incoming_invoice: "Приходные накладные",
  temporary: "Временная разметка месяца",
};

function GoodsClassificationView({
  ledger,
  savingKey,
  onChange,
}: {
  ledger: GoodsClassificationLedger;
  savingKey: string | null;
  onChange: (row: GoodsClassificationRow, decision: GoodsClassificationDecision) => void;
}) {
  const [query, setQuery] = useState("");
  const normalizedQuery = query.trim().toLocaleLowerCase("ru-RU");
  const matchedRows = ledger.rows.filter(
    (row) =>
      !normalizedQuery ||
      row.product_name.toLocaleLowerCase("ru-RU").includes(normalizedQuery) ||
      row.product_code?.toLocaleLowerCase("ru-RU").includes(normalizedQuery),
  );
  const attentionRows = matchedRows.filter(
    (row) => row.status === "unclassified" || row.status === "requires_owner_review",
  );
  const attentionRowsCount = ledger.rows.filter(
    (row) => row.status === "unclassified" || row.status === "requires_owner_review",
  ).length;
  const classifiedRows = matchedRows.filter(
    (row) => row.status !== "unclassified" && row.status !== "requires_owner_review",
  );

  const renderTable = (rows: GoodsClassificationRow[], emptyText: string) => {
    if (rows.length === 0) {
      return (
        <div className="mt-4 rounded-md bg-muted/40 p-3 text-sm text-muted-foreground">
          {emptyText}
        </div>
      );
    }
    return (
      <div className="mt-4 overflow-x-auto">
        <table className="w-full min-w-[900px] text-sm">
          <thead>
            <tr className="border-b text-left text-xs text-muted-foreground">
              <th className="pb-2 font-medium">Номенклатура</th>
              <th className="pb-2 pl-4 font-medium">Источник для ОПиУ</th>
              <th className="pb-2 pl-4 font-medium">Статья для ОПиУ</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const key = row.product_guid;
              const selectedSource = row.selected_source_kind;
              const lineOptions = ledger.options.filter(
                (option) =>
                  option.source_kind === selectedSource &&
                  (!option.temporary ||
                    row.status === "unclassified" ||
                    row.status === "requires_owner_review" ||
                    row.status === "workup"),
              );
              // Складской товар может нести строку барной ревизии — тогда показываем именно
              // её, а не общий «Складской учёт». Иначе привязка не видна на экране, и человек
              // не понимает, что она вообще есть.
              const lineValue =
                (row.status === "include" ||
                  row.status === "workup" ||
                  row.status === "stocked") &&
                row.line_code
                  ? row.line_code
                  : row.status === "stocked"
                    ? "stocked"
                    : row.status === "exclude"
                      ? "exclude"
                      : "review";
              return (
                <tr key={key} className="border-b border-muted/40 align-middle">
                  <td className="py-3 pr-4">
                    <div className="flex flex-wrap items-center gap-2 font-medium">
                      <span>{row.product_name}</span>
                      {row.revision_product ? (
                        <Badge className="bg-emerald-100 text-emerald-800 hover:bg-emerald-100">
                          Складской учёт
                        </Badge>
                      ) : null}
                    </div>
                    <div className="text-xs text-muted-foreground">
                      {row.product_code ? `Код ${row.product_code} · ` : ""}
                      {row.product_guid}
                    </div>
                    {row.note ? (
                      <div className="mt-1 max-w-xl text-xs text-muted-foreground">{row.note}</div>
                    ) : null}
                  </td>
                  <td className="py-3 pl-4">
                    <Select
                      value={selectedSource ?? undefined}
                      disabled={row.revision_product || savingKey === key}
                      onValueChange={(value) => {
                        const sourceKind = value as GoodsSourceKind;
                        const canKeepLine =
                          (row.status === "include" || row.status === "workup") &&
                          row.line_code !== null &&
                          ledger.options.some(
                            (option) =>
                              option.source_kind === sourceKind &&
                              option.line_code === row.line_code,
                          );
                        const canKeepStocked =
                          row.status === "stocked" && sourceKind === "inventory";
                        onChange(row, {
                          source_kind: sourceKind,
                          status:
                            row.status === "exclude"
                              ? "exclude"
                              : canKeepStocked
                                ? "stocked"
                                : canKeepLine
                                  ? row.status === "workup"
                                    ? "workup"
                                    : "include"
                                  : "requires_owner_review",
                          line_code: canKeepLine ? row.line_code : null,
                        });
                      }}
                    >
                      <SelectTrigger className="w-72" aria-label={`Источник: ${row.product_name}`}>
                        <SelectValue placeholder="Выберите источник" />
                      </SelectTrigger>
                      <SelectContent>
                        {row.sources.map((source) => (
                          <SelectItem key={source.source_kind} value={source.source_kind}>
                            {GOODS_SOURCE_LABEL[source.source_kind]} ·{" "}
                            {source.amount === null
                              ? source.source_kind === "inventory"
                                ? "нет движения в месяце"
                                : "нет закупок в месяце"
                              : `${formatMoney(source.amount)} ₽`}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </td>
                  <td className="py-3 pl-4">
                    <Select
                      value={lineValue}
                      disabled={
                        row.revision_product || selectedSource === null || savingKey === key
                      }
                      onValueChange={(value) => {
                        if (selectedSource === null) return;
                        const selectedOption = lineOptions.find(
                          (option) => option.line_code === value,
                        );
                        onChange(row, {
                          source_kind: selectedSource,
                          status:
                            value === "exclude"
                              ? "exclude"
                              : value === "stocked" || selectedSource === "inventory"
                                ? "stocked"
                                : selectedOption?.temporary
                                  ? "workup"
                                  : "include",
                          line_code: value === "exclude" || value === "stocked" ? null : value,
                        });
                      }}
                    >
                      <SelectTrigger
                        className="w-72"
                        aria-label={`Статья ОПиУ: ${row.product_name}`}
                      >
                        <SelectValue
                          placeholder={
                            selectedSource === null
                              ? "Сначала выберите источник"
                              : "Выберите статью"
                          }
                        />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="review" disabled>
                          Выберите статью
                        </SelectItem>
                        {lineOptions.map((option) => (
                          <SelectItem key={option.line_code} value={option.line_code}>
                            {option.temporary
                              ? `${option.line_title} — только этот месяц`
                              : option.line_title}
                          </SelectItem>
                        ))}
                        {selectedSource === "inventory" ? (
                          <SelectItem value="stocked">Складской учёт</SelectItem>
                        ) : null}
                        <SelectItem value="exclude">Не учитывать в товарных расходах</SelectItem>
                      </SelectContent>
                    </Select>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    );
  };

  return (
    <div className="space-y-4">
      <div className="flex justify-end">
        <Input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Найти товар"
          aria-label="Поиск по номенклатуре"
          className="w-64"
        />
      </div>

      <Card className={`p-4 ${attentionRowsCount > 0 ? "border-amber-300" : "border-emerald-300"}`}>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="text-sm font-semibold">Нужно разметить</div>
            <div className="mt-1 text-xs text-muted-foreground">
              Здесь только новые товары, для которых система ещё не знает источник и статью ОПиУ.
              Пока решение не принято, сумма в отчёт не попадает.
            </div>
          </div>
          <Badge variant={attentionRowsCount > 0 ? "outline" : "secondary"}>
            {attentionRowsCount} товаров
          </Badge>
        </div>
        {renderTable(
          attentionRows,
          normalizedQuery ? "По запросу новых товаров не найдено." : "Все новые товары размечены.",
        )}
      </Card>

      <Card className="p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="text-sm font-semibold">Размеченные товары</div>
            <div className="mt-1 text-xs text-muted-foreground">
              Одна строка на номенклатуру. «Проработка» учитывает расход сейчас, но в следующем
              месяце снова вернёт товар на разметку.
            </div>
          </div>
          <Badge variant="secondary">{ledger.rules_count} товаров</Badge>
        </div>
        {renderTable(
          classifiedRows,
          normalizedQuery
            ? "По запросу размеченных товаров не найдено."
            : "Размеченных товаров пока нет.",
        )}
      </Card>
    </div>
  );
}

function GoodsLedgerView({
  ledger,
  classifications,
  savingKey,
  onClassificationChange,
}: {
  ledger: GoodsLedger;
  classifications: GoodsClassificationLedger;
  savingKey: string | null;
  onClassificationChange: (
    row: GoodsClassificationRow,
    decision: GoodsClassificationDecision,
  ) => void;
}) {
  const revisionSummaries = ledger.summaries.filter((item) => item.source_kind === "inventory");
  const revisionRows = ledger.rows.filter((item) => item.source_kind === "inventory");
  const suppliesSummaries = ledger.summaries.filter((item) => item.source_kind !== "inventory");
  const suppliesRows = ledger.rows.filter((item) => item.source_kind !== "inventory");

  const renderLedger = (
    summaries: GoodsLedger["summaries"],
    rows: GoodsLedger["rows"],
    kind: "revision" | "supplies",
  ) => {
    // Обещать номенклатуру можно только там, где она вообще бывает. У результата
    // инвентаризации её нет по построению, и прежняя плашка звала ждать синхронизацию,
    // которая ничего не принесёт.
    const incomplete = summaries.filter(
      (item) => item.amount !== null && item.details_expected && !item.details_complete,
    );
    const auditSummary = summaries.find((item) => item.line_code === "audit_results");
    return (
      <div className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {kind === "revision" ? (
            <>
              {/* Строку берём по коду, а не по порядку: рядом с продуктовыми ревизиями в
                  этом же блоке стоят результаты инвентаризации упаковки, и summaries[0]
                  однажды окажется не тем, чем кажется. */}
              <MetricCard
                title="Недостача по ревизиям"
                amount={auditSummary?.amount ?? null}
                note="Входит в расход ОПиУ"
              />
              <MetricCard
                title="Излишки по ревизиям"
                amount={auditSummary?.surplus_amount ?? null}
                note="Показаны справочно, недостачу не уменьшают"
              />
              {summaries
                .filter((item) => item.line_code !== "audit_results")
                .map((item) => (
                  <MetricCard
                    key={item.line_code}
                    title={item.line_title}
                    amount={item.amount}
                    note="Расхождение книги с фактом, не складской оборот"
                  />
                ))}
            </>
          ) : (
            summaries.map((item) => (
              <MetricCard
                key={item.line_code}
                title={item.line_title}
                amount={item.amount}
                note={GOODS_SOURCE_LABEL[item.source_kind]}
              />
            ))
          )}
        </div>
        {incomplete.length > 0 ? (
          <Card className="border-amber-300 p-4 text-sm">
            Для {incomplete.map((item) => item.line_title).join(", ")} итог уже сохранён, но
            поимённая номенклатура появится после следующей синхронизации iiko.
          </Card>
        ) : null}
        {rows.length === 0 ? (
          <LedgerMessage>
            {kind === "revision"
              ? "По выбранному месяцу нет проведённых ревизий. Складской оборот здесь намеренно не показывается."
              : "По выбранному месяцу нет прямых товарных расходов."}
          </LedgerMessage>
        ) : (
          <Card className="overflow-x-auto p-4">
            <table className="w-full min-w-[860px] text-sm">
              <thead>
                <tr className="border-b text-left text-xs text-muted-foreground">
                  {kind === "supplies" ? (
                    <th className="pb-2 font-medium">Статья ОПиУ</th>
                  ) : (
                    <th className="pb-2 font-medium">Дата ревизии</th>
                  )}
                  <th className="pb-2 font-medium">Номенклатура</th>
                  {kind === "revision" ? (
                    <>
                      <th className="pb-2 text-right font-medium">Недостача</th>
                      <th className="pb-2 text-right font-medium">Излишек</th>
                    </>
                  ) : (
                    <th className="pb-2 text-right font-medium">Закупка</th>
                  )}
                  {kind === "supplies" ? (
                    <th className="pb-2 text-right font-medium">Расход</th>
                  ) : null}
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr
                    key={`${row.source_kind}-${row.audit_date ?? "direct"}-${row.product_guid}`}
                    className="border-b border-muted/40"
                  >
                    {kind === "supplies" ? (
                      <td className="py-2">{row.line_title}</td>
                    ) : (
                      <td className="py-2 tabular-nums">{formatDate(row.audit_date)}</td>
                    )}
                    <td className="py-2 font-medium">{row.product_name}</td>
                    {kind === "revision" ? (
                      <>
                        <td className="py-2 text-right font-medium tabular-nums">
                          {formatMoney(row.amount)}
                        </td>
                        <td className="py-2 text-right tabular-nums">
                          {formatMoney(row.surplus_amount)}
                        </td>
                      </>
                    ) : (
                      <td className="py-2 text-right tabular-nums">
                        {formatMoney(row.source_amount)}
                      </td>
                    )}
                    {kind === "supplies" ? (
                      <td className="py-2 text-right font-medium tabular-nums">
                        {formatMoney(row.amount)}
                      </td>
                    ) : null}
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        )}
      </div>
    );
  };

  return (
    <div className="space-y-4">
      <Tabs defaultValue="revision" className="space-y-4">
        <TabsList className="grid w-full max-w-md grid-cols-2">
          <TabsTrigger value="revision">Ревизии</TabsTrigger>
          <TabsTrigger value="supplies">Расходники</TabsTrigger>
        </TabsList>
        <TabsContent value="revision" className="mt-0 space-y-4">
          <div className="text-sm text-muted-foreground">
            Только фактические результаты проведённых ревизий iiko. Недостачи и излишки показаны
            отдельно; складской оборот «начало + приход − конец» относится к балансу.
          </div>
          {renderLedger(revisionSummaries, revisionRows, "revision")}
        </TabsContent>
        <TabsContent value="supplies" className="mt-0 space-y-4">
          <div className="text-sm text-muted-foreground">
            Содержание торговых точек, вспомогательные товары и временная «Проработка» по приходным
            накладным.
          </div>
          {renderLedger(suppliesSummaries, suppliesRows, "supplies")}
        </TabsContent>
      </Tabs>
      <GoodsClassificationView
        ledger={classifications}
        savingKey={savingKey}
        onChange={onClassificationChange}
      />
    </div>
  );
}

const RECOGNITION_STATUS = {
  recognized: { label: "признан", variant: "secondary" as const },
  waiting_document: { label: "ждём документ", variant: "outline" as const },
  missing_period: { label: "нет периода", variant: "destructive" as const },
};

function RecognitionLedgerView({ ledger }: { ledger: RecognitionLedger }) {
  const totals = ledger.totals;
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        <MetricCard title="Признано в месяце" amount={totals.recognized} />
        <MetricCard title="Ещё не признано" amount={totals.unrecognized} />
        <MetricCard title="Ждём документ" amount={totals.waiting_document} />
        <MetricCard title="Не заполнен период" amount={totals.missing_period} />
        <MetricCard title="Без статьи ОПиУ" amount={totals.unattributed} />
      </div>
      {ledger.rows.length === 0 ? (
        <LedgerMessage>За выбранный месяц нет начислений и ожидающих расходов.</LedgerMessage>
      ) : (
        <Card className="overflow-x-auto p-4">
          <table className="w-full min-w-[860px] text-sm">
            <thead>
              <tr className="border-b text-left text-xs text-muted-foreground">
                <th className="pb-2 font-medium">Статус</th>
                <th className="pb-2 font-medium">Контрагент</th>
                <th className="pb-2 font-medium">Статья / причина</th>
                <th className="pb-2 font-medium">Период услуги</th>
                <th className="pb-2 text-right font-medium">Сумма</th>
              </tr>
            </thead>
            <tbody>
              {ledger.rows.map((row) => {
                const status = RECOGNITION_STATUS[row.status];
                return (
                  <tr
                    key={`${row.source_kind}-${row.source_id}`}
                    className="border-b border-muted/40 align-top"
                  >
                    <td className="py-2">
                      <Badge variant={status.variant}>{status.label}</Badge>
                    </td>
                    <td className="py-2 font-medium">{row.counterparty_name}</td>
                    <td className="py-2">
                      <div>{row.article_name}</div>
                      {row.line_title ? (
                        <div className="text-xs text-muted-foreground">{row.line_title}</div>
                      ) : null}
                      <div className="mt-1 max-w-lg text-xs text-muted-foreground">
                        {row.reason}
                      </div>
                    </td>
                    <td className="py-2 whitespace-nowrap">
                      {formatPeriod(row.service_period_start, row.service_period_end)}
                    </td>
                    <td className="py-2 text-right font-medium whitespace-nowrap tabular-nums">
                      {formatMoney(row.amount)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}

export function PnlRoute() {
  const months = useMemo(() => availableMonths(), []);
  const [month, setMonth] = useState(months[0] ?? FIRST_MONTH);
  const [activeTab, setActiveTab] = useState<PnlTab>("summary");
  const [report, setReport] = useState<PnlReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [openLine, setOpenLine] = useState<string | null>(null);
  const [drill, setDrill] = useState<PnlDrill | null>(null);
  const [drillLoading, setDrillLoading] = useState(false);
  const [drillError, setDrillError] = useState<string | null>(null);
  const [goodsSavingKey, setGoodsSavingKey] = useState<string | null>(null);
  const payrollLedger = useLedger(activeTab === "payroll", month, fetchPayrollLedger);
  const partnerLedger = useLedger(activeTab === "partners", month, fetchPartnerCommissionLedger);
  const goodsLedger = useLedger(activeTab === "goods", month, fetchGoodsLedger);
  const goodsClassifications = useLedger(activeTab === "goods", month, fetchGoodsClassifications);
  const recognitionLedger = useLedger(activeTab === "recognition", month, fetchRecognitionLedger);

  const load = useCallback((target: string) => {
    setLoading(true);
    setError(null);
    fetchPnlReport(target)
      .then(setReport)
      .catch((cause: unknown) => {
        setError(cause instanceof Error ? cause.message : "Не удалось загрузить отчёт");
        setReport(null);
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load(month);
  }, [month, load]);

  const saveGoodsClassification = useCallback(
    async (row: GoodsClassificationRow, decision: GoodsClassificationDecision) => {
      const key = row.product_guid;
      setGoodsSavingKey(key);
      try {
        const updatedClassifications = await updateGoodsClassification(month, row, decision);
        goodsClassifications.replace(updatedClassifications);
        toast.success(
          decision.status === "requires_owner_review"
            ? "Источник выбран. Теперь выберите статью ОПиУ"
            : decision.status === "workup"
              ? "Товар учтён в Проработке только за выбранный месяц"
              : decision.status === "stocked"
                ? "Товар включён в складской учёт"
                : "Разметка сохранена, ОПиУ пересчитан",
        );
        try {
          const [updatedGoodsLedger, updatedReport] = await Promise.all([
            fetchGoodsLedger(month),
            fetchPnlReport(month),
          ]);
          goodsLedger.replace(updatedGoodsLedger);
          setReport(updatedReport);
        } catch (cause: unknown) {
          toast.warning(
            apiErrorMessage(cause, "Разметка сохранена, но итоговые суммы не обновились"),
          );
        }
      } catch (cause: unknown) {
        toast.error(apiErrorMessage(cause, "Не удалось сохранить разметку"));
      } finally {
        setGoodsSavingKey(null);
      }
    },
    [goodsClassifications, goodsLedger, month],
  );

  // Расшифровка запрашивается по клику, а не вместе с отчётом: восемьдесят разложений в
  // каждом ответе утяжелили бы главный экран ради данных, которые чаще всего не откроют.
  useEffect(() => {
    if (openLine === null) return;
    let cancelled = false;
    setDrillLoading(true);
    setDrillError(null);
    setDrill(null);
    fetchPnlLineDrill(openLine, month)
      .then((value) => {
        if (!cancelled) setDrill(value);
      })
      .catch((cause: unknown) => {
        if (cancelled) return;
        setDrillError(cause instanceof Error ? cause.message : "Не удалось загрузить расшифровку");
      })
      .finally(() => {
        if (!cancelled) setDrillLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [openLine, month]);

  const openedLine = report?.lines.find((line) => line.code === openLine) ?? null;
  const canGoBack = month > FIRST_MONTH;
  const canGoForward = month < months[0];

  return (
    <div className="space-y-4 p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Отчёт о прибылях и убытках</h1>
          <p className="text-sm text-muted-foreground">
            Управленческий ОПиУ. Пустые строки означают отсутствие данных, а не ноль.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={!canGoBack}
            onClick={() => setMonth(shiftMonth(month, -1))}
          >
            ‹
          </Button>
          <Select value={month} onValueChange={setMonth}>
            <SelectTrigger className="w-44">
              <SelectValue>{monthLabel(month)}</SelectValue>
            </SelectTrigger>
            <SelectContent>
              {months.map((item) => (
                <SelectItem key={item} value={item}>
                  {monthLabel(item)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button
            variant="outline"
            size="sm"
            disabled={!canGoForward}
            onClick={() => setMonth(shiftMonth(month, 1))}
          >
            ›
          </Button>
        </div>
      </div>

      <Tabs
        value={activeTab}
        onValueChange={(value) => {
          setActiveTab(value as PnlTab);
          setOpenLine(null);
        }}
        className="space-y-4"
      >
        <div className="overflow-x-auto">
          <TabsList className="h-auto min-w-max justify-start">
            <TabsTrigger value="summary">Свод ОПиУ</TabsTrigger>
            <TabsTrigger value="payroll">Зарплата</TabsTrigger>
            <TabsTrigger value="partners">Расчёты с партнёрами</TabsTrigger>
            <TabsTrigger value="goods">Товары iiko</TabsTrigger>
            <TabsTrigger value="recognition">Признание ДЗ/КЗ</TabsTrigger>
          </TabsList>
        </div>

        <TabsContent value="summary" className="mt-0 space-y-4">
          {error && (
            <Card className="border-destructive p-4 text-sm text-destructive">{error}</Card>
          )}
          {loading && (
            <Card className="p-4">
              <Skeleton className="h-6 w-full" />
              <Skeleton className="mt-2 h-6 w-full" />
              <Skeleton className="mt-2 h-6 w-2/3" />
            </Card>
          )}
          {!loading && report && (
            <>
              {report.warnings.length > 0 && (
                <Card className="border-amber-300 p-4">
                  <div className="text-sm font-medium">Требует внимания</div>
                  <ul className="mt-2 space-y-1 text-xs text-muted-foreground">
                    {report.warnings.map((warning, index) => (
                      <li key={`${warning.code}-${index}`}>{warning.message}</li>
                    ))}
                  </ul>
                </Card>
              )}
              <Card className="p-4">
                <ReportTable report={report} onOpenLine={setOpenLine} />
              </Card>
              <Reconciliation report={report} />
            </>
          )}
        </TabsContent>

        <TabsContent value="payroll" className="mt-0 space-y-4">
          {payrollLedger.error ? (
            <Card className="border-destructive p-4 text-sm text-destructive">
              {payrollLedger.error}
            </Card>
          ) : null}
          {payrollLedger.loading ? (
            <LedgerMessage>Загружаем зарплатный леджер…</LedgerMessage>
          ) : null}
          {!payrollLedger.loading && payrollLedger.data ? (
            <PayrollLedgerView ledger={payrollLedger.data} />
          ) : null}
        </TabsContent>

        <TabsContent value="partners" className="mt-0 space-y-4">
          {partnerLedger.error ? (
            <Card className="border-destructive p-4 text-sm text-destructive">
              {partnerLedger.error}
            </Card>
          ) : null}
          {partnerLedger.loading ? (
            <LedgerMessage>Загружаем расчёты с партнёрами…</LedgerMessage>
          ) : null}
          {!partnerLedger.loading && partnerLedger.data ? (
            <PartnerCommissionLedgerView ledger={partnerLedger.data} />
          ) : null}
        </TabsContent>

        <TabsContent value="goods" className="mt-0 space-y-4">
          {goodsLedger.error || goodsClassifications.error ? (
            <Card className="border-destructive p-4 text-sm text-destructive">
              {goodsLedger.error || goodsClassifications.error}
            </Card>
          ) : null}
          {goodsLedger.loading || goodsClassifications.loading ? (
            <LedgerMessage>Загружаем товарный леджер…</LedgerMessage>
          ) : null}
          {!goodsLedger.loading &&
          !goodsClassifications.loading &&
          goodsLedger.data &&
          goodsClassifications.data ? (
            <GoodsLedgerView
              ledger={goodsLedger.data}
              classifications={goodsClassifications.data}
              savingKey={goodsSavingKey}
              onClassificationChange={saveGoodsClassification}
            />
          ) : null}
        </TabsContent>

        <TabsContent value="recognition" className="mt-0 space-y-4">
          {recognitionLedger.error ? (
            <Card className="border-destructive p-4 text-sm text-destructive">
              {recognitionLedger.error}
            </Card>
          ) : null}
          {recognitionLedger.loading ? (
            <LedgerMessage>Загружаем признание расходов…</LedgerMessage>
          ) : null}
          {!recognitionLedger.loading && recognitionLedger.data ? (
            <RecognitionLedgerView ledger={recognitionLedger.data} />
          ) : null}
        </TabsContent>
      </Tabs>

      <Sheet open={openLine !== null} onOpenChange={(next) => !next && setOpenLine(null)}>
        <SheetContent side="right" className="w-full overflow-y-auto sm:max-w-xl">
          <SheetHeader>
            <SheetTitle>{openedLine?.title ?? drill?.line_title ?? "Расшифровка"}</SheetTitle>
            <SheetDescription>
              {monthLabel(month)}
              {openedLine?.amount !== null && openedLine?.amount !== undefined && (
                <>
                  {" · "}
                  {openedLine.status === "incomplete" ? "≈ " : ""}
                  {formatMoney(openedLine.amount)} ₽
                </>
              )}
            </SheetDescription>
          </SheetHeader>
          <DrillPanel drill={drill} loading={drillLoading} error={drillError} />
        </SheetContent>
      </Sheet>
    </div>
  );
}

export default PnlRoute;
