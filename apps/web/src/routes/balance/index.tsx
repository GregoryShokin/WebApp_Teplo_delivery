/** Страница «Финансы → Баланс».
 *
 * ГЛАВНОЕ, ЧТО ЭКРАН ОБЯЗАН ПОКАЗАТЬ ЧЕСТНО — три разные пустоты. Прочерк со словом «нет
 * данных» означает, что величина неизвестна; ноль печатается только там, где владелец
 * подтвердил его отсутствие; «ждёт ввода» — что строка без регистра и её вносит человек.
 * Свести всё к нулю значило бы соврать одинаково в трёх разных случаях, а баланс именно этим
 * и опасен: он всегда выглядит правдоподобно.
 *
 * ВТОРОЕ: равенство «актив = пассив» здесь НЕ печатается как проверка. Капитал выводится как
 * остаток, поэтому равенство выполняется по построению при любых ошибках в данных. Настоящая
 * проверка — сверка приращения капитала с прибылью, и она показана отдельным блоком со своим
 * вердиктом.
 *
 * Дата — через `toIsoDate`, а не срезом ISO-строки: с полуночи до трёх ночи по Москве UTC-дата
 * даёт вчерашний день, и снимок уехал бы в чужой месяц.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { apiErrorMessage } from "@/lib/api";
import { toIsoDate } from "@/lib/date";

import {
  type BalanceFlag,
  type BalanceLine,
  type BalanceView,
  fetchBalancePreview,
  fetchBalanceSnapshot,
  freezeBalance,
} from "./api";

/** Деньги с разделителем разрядов. Строку не парсим ради арифметики — только для показа. */
function formatMoney(value: string): string {
  const amount = Number(value);
  if (!Number.isFinite(amount)) return value;
  return amount.toLocaleString("ru-RU", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

/** Последний день месяца — единственная дата, на которую собирается баланс. */
function lastDayOfMonth(year: number, month: number): string {
  return toIsoDate(new Date(year, month + 1, 0));
}

function defaultAsOf(): string {
  const now = new Date();
  // По умолчанию — конец ПРОШЛОГО месяца: текущий ещё не закончился, и остаток на его конец
  // существовать не может.
  return lastDayOfMonth(now.getFullYear(), now.getMonth() - 1);
}

const STATUS_LABEL: Record<string, string> = {
  approximate: "≈ приблизительно",
  manual: "введено вручную",
  zero_confirmed: "подтверждённый ноль",
  not_applicable: "не применяется",
  missing: "нет данных",
  plug: "выведен как остаток",
};

const SEVERITY_STYLE: Record<string, string> = {
  blocking: "border-destructive/40 bg-destructive/5",
  warning: "border-amber-400/40 bg-amber-50 dark:bg-amber-950/20",
  info: "border-border bg-muted/30",
};

function LineAmount({ line }: { line: BalanceLine }) {
  if (line.amount === null) {
    return (
      <span className="text-muted-foreground text-sm">
        {STATUS_LABEL[line.status] ?? "нет данных"}
      </span>
    );
  }
  return (
    <span className="tabular-nums">
      {formatMoney(line.amount)}
      {line.status === "approximate" ? (
        <span className="text-muted-foreground ml-1" title="Датировка части суммы приблизительна">
          ≈
        </span>
      ) : null}
    </span>
  );
}

function LineRow({ line }: { line: BalanceLine }) {
  const muted = line.kind === "memo";
  return (
    <div
      className={`flex items-start justify-between gap-4 border-b px-3 py-2 last:border-0 ${
        muted ? "text-muted-foreground" : ""
      }`}
    >
      <div className="min-w-0">
        <div className={`text-sm ${muted ? "pl-4 italic" : ""}`}>{line.title}</div>
        {line.source_note ? (
          <div className="text-muted-foreground mt-0.5 text-xs">{line.source_note}</div>
        ) : null}
      </div>
      <div className="shrink-0 text-right text-sm">
        <LineAmount line={line} />
        {line.status !== "ok" && line.amount !== null ? (
          <div className="text-muted-foreground text-xs">{STATUS_LABEL[line.status] ?? ""}</div>
        ) : null}
      </div>
    </div>
  );
}

function FlagCard({ flag }: { flag: BalanceFlag }) {
  return (
    <Card className={`p-3 ${SEVERITY_STYLE[flag.severity] ?? ""}`}>
      <div className="flex items-center justify-between gap-3">
        <div className="text-sm font-medium">{flag.title}</div>
        {Number(flag.amount) !== 0 ? (
          <div className="tabular-nums text-sm">{formatMoney(flag.amount)} ₽</div>
        ) : flag.count ? (
          <div className="text-muted-foreground text-sm">{flag.count} шт</div>
        ) : null}
      </div>
      <div className="text-muted-foreground mt-1 text-xs">{flag.explanation}</div>
    </Card>
  );
}

function ReconciliationCard({ view }: { view: BalanceView }) {
  const recon = view.reconciliation;
  if (!recon) {
    return (
      <Card className="p-4">
        <div className="text-sm font-medium">Сверка приращения капитала</div>
        <p className="text-muted-foreground mt-1 text-sm">
          Это первый снимок: капитал установлен описью, сверять приращение не с чем. Проверка
          появится со следующего месяца — тогда изменение капитала должно совпасть с прибылью за
          вычетом дивидендов и с учётом вкладов.
        </p>
      </Card>
    );
  }
  const ok = recon.explained;
  return (
    <Card className={`p-4 ${ok ? "" : "border-destructive/40 bg-destructive/5"}`}>
      <div className="flex items-center justify-between gap-3">
        <div className="text-sm font-medium">Сверка приращения капитала</div>
        <Badge variant={ok ? "secondary" : "destructive"}>
          {ok ? "сошлось" : "не объяснено"}
        </Badge>
      </div>
      <dl className="mt-3 grid gap-1 text-sm sm:grid-cols-2">
        <div className="flex justify-between gap-4">
          <dt className="text-muted-foreground">Капитал изменился на</dt>
          <dd className="tabular-nums">{formatMoney(recon.delta_equity)}</dd>
        </div>
        <div className="flex justify-between gap-4">
          <dt className="text-muted-foreground">Ожидалось по прибыли</dt>
          <dd className="tabular-nums">{formatMoney(recon.expected)}</dd>
        </div>
        <div className="flex justify-between gap-4">
          <dt className="text-muted-foreground">Необъяснённый остаток</dt>
          <dd className="tabular-nums font-medium">{formatMoney(recon.residual)}</dd>
        </div>
        <div className="flex justify-between gap-4">
          <dt className="text-muted-foreground">Допуск</dt>
          <dd className="tabular-nums">{formatMoney(recon.tolerance)}</dd>
        </div>
      </dl>
      {recon.net_profit === null ? (
        <p className="text-muted-foreground mt-2 text-xs">
          Прибыль за период неизвестна ({recon.net_profit_status ?? "нет данных"}), поэтому это
          не «сошлось» и не «не сошлось» — это отсутствие ответа.
        </p>
      ) : null}
    </Card>
  );
}

export function BalanceRoute() {
  const [asOf, setAsOf] = useState(defaultAsOf);
  const [view, setView] = useState<BalanceView | null>(null);
  const [loading, setLoading] = useState(true);
  const [freezing, setFreezing] = useState(false);

  const load = useCallback(async (date: string) => {
    setLoading(true);
    try {
      // Сначала подписанный снимок: он и есть ответ на вопрос «сколько мы сказали».
      // Нет снимка — показываем предпросмотр, то есть «сколько получается сейчас».
      let data: BalanceView;
      try {
        data = await fetchBalanceSnapshot(date);
      } catch {
        data = await fetchBalancePreview(date);
      }
      // Ответ проверяется на форму, а не принимается на веру: при отсутствии прав или
      // ошибке слой запроса может вернуть тело ошибки вместо баланса, и страница падала бы
      // целиком, показывая пустой экран вместо объяснения.
      if (!data || !Array.isArray(data.lines)) {
        throw new Error("Ответ не похож на баланс — проверьте права доступа");
      }
      setView(data);
    } catch (error) {
      toast.error(apiErrorMessage(error));
      setView(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(asOf);
  }, [asOf, load]);

  const groups = useMemo(() => {
    if (!view || !Array.isArray(view.lines)) return [];
    const byGroup = new Map<string, BalanceLine[]>();
    for (const line of view.lines) {
      const list = byGroup.get(line.group_code) ?? [];
      list.push(line);
      byGroup.set(line.group_code, list);
    }
    return [...byGroup.entries()].map(([group, lines]) => ({
      group,
      block: lines[0]?.block ?? "asset",
      lines,
      total: lines
        .filter((line) => line.kind !== "memo" && line.amount !== null)
        .reduce((sum, line) => sum + Number(line.amount), 0),
    }));
  }, [view]);

  const onFreeze = async () => {
    setFreezing(true);
    try {
      setView(await freezeBalance(asOf));
      toast.success("Черновик баланса собран");
    } catch (error) {
      toast.error(apiErrorMessage(error));
    } finally {
      setFreezing(false);
    }
  };

  const monthOptions = useMemo(() => {
    const now = new Date();
    const out: string[] = [];
    for (let back = 1; back <= 12; back += 1) {
      out.push(lastDayOfMonth(now.getFullYear(), now.getMonth() - back));
    }
    return out;
  }, []);

  return (
    <div className="space-y-4 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Баланс</h1>
          <p className="text-muted-foreground text-sm">
            Остатки на конец месяца. Капитал выводится как актив минус обязательства, поэтому
            равенство сторон здесь не проверка — проверяет сверка приращения.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <select
            className="border-input bg-background h-9 rounded-md border px-2 text-sm"
            value={asOf}
            onChange={(event) => setAsOf(event.target.value)}
          >
            {monthOptions.map((value) => (
              <option key={value} value={value}>
                {new Date(value).toLocaleDateString("ru-RU", {
                  month: "long",
                  year: "numeric",
                })}
              </option>
            ))}
          </select>
          <Button onClick={onFreeze} disabled={freezing || view?.status === "final"}>
            {view?.status === "final" ? "Подписан" : "Собрать черновик"}
          </Button>
        </div>
      </div>

      {loading ? (
        <div className="space-y-2">
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-64 w-full" />
        </div>
      ) : !view ? (
        <Card className="p-6 text-center">
          <div className="text-sm font-medium">Баланс на эту дату собрать не удалось</div>
        </Card>
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-3">
            <Card className="p-4">
              <div className="text-muted-foreground text-xs">Активы</div>
              <div className="tabular-nums text-lg font-semibold">
                {formatMoney(view.asset_total)} ₽
              </div>
            </Card>
            <Card className="p-4">
              <div className="text-muted-foreground text-xs">Обязательства</div>
              <div className="tabular-nums text-lg font-semibold">
                {formatMoney(view.liability_total)} ₽
              </div>
            </Card>
            <Card className="p-4">
              <div className="text-muted-foreground text-xs">
                Капитал {view.method === "inventory" ? "(установлен описью)" : "(выведен)"}
              </div>
              <div className="tabular-nums text-lg font-semibold">
                {formatMoney(view.equity_plug)} ₽
              </div>
            </Card>
          </div>

          <ReconciliationCard view={view} />

          {(view.flags ?? []).length ? (
            <div className="grid gap-2 sm:grid-cols-2">
              {(view.flags ?? []).map((flag) => (
                <FlagCard key={flag.code} flag={flag} />
              ))}
            </div>
          ) : null}

          <div className="grid gap-3 lg:grid-cols-2">
            {groups.map((group) => (
              <Card key={group.group} className="overflow-hidden">
                <div className="bg-muted/40 flex items-center justify-between px-3 py-2">
                  <div className="text-sm font-medium">{group.group}</div>
                  <div className="tabular-nums text-sm">
                    {group.total.toLocaleString("ru-RU", {
                      minimumFractionDigits: 2,
                      maximumFractionDigits: 2,
                    })}
                  </div>
                </div>
                <div>
                  {group.lines.map((line) => (
                    <LineRow key={line.line_code} line={line} />
                  ))}
                </div>
              </Card>
            ))}
          </div>

          <p className="text-muted-foreground text-xs">
            {view.status === "final"
              ? `Снимок подписан ${view.finalized_at ? new Date(view.finalized_at).toLocaleString("ru-RU") : ""} и больше не пересобирается.`
              : view.status === "draft"
                ? "Черновик: пересобирается по кнопке, пока не подписан."
                : "Предпросмотр: цифры живые, снимок ещё не собран."}
          </p>
        </>
      )}
    </div>
  );
}

export default BalanceRoute;
