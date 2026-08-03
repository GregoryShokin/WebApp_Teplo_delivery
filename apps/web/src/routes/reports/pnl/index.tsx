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

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";

import { fetchPnlReport, type LineStatus, type PnlLine, type PnlReport } from "./api";

const MONTH_NAMES = [
  "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
  "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
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
};

const STATUS_VARIANT: Partial<Record<LineStatus, "secondary" | "outline" | "destructive">> = {
  overdue_document: "destructive",
  needs_review: "destructive",
  waiting_document: "outline",
  manual: "outline",
};

function LineAmount({ line }: { line: PnlLine }) {
  if (line.kind === "ratio") {
    return <span className="tabular-nums">{formatPercent(line.amount)}</span>;
  }
  if (line.amount === null) {
    const label = STATUS_LABEL[line.status] ?? line.status;
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

function ReportTable({ report }: { report: PnlReport }) {
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
            return (
              <tr
                key={line.code}
                className={[
                  "border-b border-muted/40",
                  isTotal ? "bg-muted/40 font-semibold" : "",
                  isRatio ? "text-muted-foreground" : "",
                  line.status === "not_used" ? "opacity-45" : "",
                ].join(" ")}
              >
                <td className="py-1.5" style={{ paddingLeft: `${line.level * 16}px` }}>
                  {line.title}
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

export function PnlRoute() {
  const months = useMemo(() => availableMonths(), []);
  const [month, setMonth] = useState(months[0] ?? FIRST_MONTH);
  const [report, setReport] = useState<PnlReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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
            <ReportTable report={report} />
          </Card>
          <Reconciliation report={report} />
        </>
      )}
    </div>
  );
}

export default PnlRoute;
