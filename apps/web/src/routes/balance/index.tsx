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
import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
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
import { Skeleton } from "@/components/ui/skeleton";
import { apiErrorMessage } from "@/lib/api";
import { toIsoDate } from "@/lib/date";

import {
  type BalanceDriftRow,
  type BalanceFlag,
  type BalanceLine,
  type BalanceView,
  fetchBalanceDrift,
  fetchBalancePreview,
  fetchBalanceSnapshot,
  freezeBalance,
  setBalanceManualValue,
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

/** Начало управленческого учёта. Раньше этой даты остатков в системе нет, и сервер их не отдаёт
 *  (`ACCOUNTING_START` в `app/services/accounting_periods.py`). Константа продублирована здесь
 *  намеренно: тянуть её ручкой ради одного числа, которое меняется раз в жизни бизнеса, дороже,
 *  чем держать рядом с местом, где она нужна. */
const ACCOUNTING_START = "2026-07-01";

/** Последний день месяца — единственная дата, на которую собирается баланс. */
function lastDayOfMonth(year: number, month: number): string {
  return toIsoDate(new Date(year, month + 1, 0));
}

/** Дата среза словами. Название месяца в селекторе двусмысленно: «июль 2026» одинаково читается
 *  как «на начало июля» и «на конец июля», а это два разных баланса. Пишем день. */
function asOfLabel(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: "numeric" });
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

/** Человеческие подписи полей расшифровки сигнала. Ключи приходят с сервера как есть, и
 *  показывать владельцу `line_code` вместо названия строки — то же самое, что не показывать
 *  ничего. */
const FLAG_ROW_LABELS: Record<string, string> = {
  line: "строка",
  product: "позиция",
  why: "почему",
  month: "месяц",
  title: "начисление",
  amount: "сумма",
  due: "срок",
  wallet: "счёт",
  balance: "остаток",
};

/** Что показать в строке расшифровки: сначала человеческое имя, потом всё остальное.
 *  `code` намеренно не печатается — он для меня, не для владельца. */
function flagRowText(row: Record<string, unknown>): string {
  const head = row.line ?? row.product ?? row.title ?? row.wallet ?? row.month;
  const rest = Object.entries(row)
    .filter(([key, value]) => key !== "code" && value != null && value !== head)
    .map(([key, value]) => `${FLAG_ROW_LABELS[key] ?? key}: ${String(value)}`);
  return [head ? String(head) : null, rest.length ? rest.join(" · ") : null]
    .filter(Boolean)
    .join(" — ");
}

function FlagCard({ flag }: { flag: BalanceFlag }) {
  const [open, setOpen] = useState(false);
  const rows = Array.isArray(flag.rows) ? flag.rows : [];
  // Сигнал со счётчиком «3 шт» и без расшифровки — это предупреждение, на которое нельзя
  // ответить: владелец видит, что что-то не так, и не видит что именно. Раскрываем.
  const expandable = rows.length > 0;
  return (
    <Card className={`p-3 ${SEVERITY_STYLE[flag.severity] ?? ""}`}>
      <button
        type="button"
        className={`flex w-full items-center justify-between gap-3 text-left ${
          expandable ? "cursor-pointer" : "cursor-default"
        }`}
        onClick={expandable ? () => setOpen((value) => !value) : undefined}
        aria-expanded={expandable ? open : undefined}
        disabled={!expandable}
      >
        <div className="text-sm font-medium">
          {flag.title}
          {expandable ? (
            <span className="text-muted-foreground ml-2 text-xs font-normal">
              {open ? "свернуть" : "показать"}
            </span>
          ) : null}
        </div>
        {Number(flag.amount) !== 0 ? (
          <div className="tabular-nums shrink-0 text-sm">{formatMoney(flag.amount)} ₽</div>
        ) : flag.count ? (
          <div className="text-muted-foreground shrink-0 text-sm">{flag.count} шт</div>
        ) : null}
      </button>
      <div className="text-muted-foreground mt-1 text-xs">{flag.explanation}</div>
      {expandable && open ? (
        <ul className="mt-2 space-y-1 border-t pt-2">
          {rows.map((row, index) => (
            <li key={index} className="text-xs">
              {flagRowText(row)}
            </li>
          ))}
        </ul>
      ) : null}
    </Card>
  );
}

function DriftCard({
  rows,
  status,
  onRebuild,
  busy,
}: {
  rows: BalanceDriftRow[];
  status: BalanceView["status"];
  onRebuild: () => void;
  busy: boolean;
}) {
  const final = status === "final";
  return (
    <Card className="border-amber-400/50 bg-amber-50 p-4 dark:bg-amber-950/20">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="text-sm font-medium">
          {final
            ? "Подписанный снимок разошёлся с сегодняшним расчётом"
            : "Черновик собран раньше — цифры на экране устарели"}
        </div>
        {final ? null : (
          <Button size="sm" variant="secondary" onClick={onRebuild} disabled={busy}>
            Пересобрать
          </Button>
        )}
      </div>
      <p className="text-muted-foreground mt-1 text-xs">
        {final
          ? "Снимок не пересобирается — так и задумано. Расхождение означает, что первичку правили после подписи; ниже видно, где именно."
          : "Ниже показаны цифры на момент сборки черновика, а не текущие. Нажмите «Пересобрать», чтобы обновить."}
      </p>
      <div className="mt-3 space-y-1">
        {rows.slice(0, 8).map((row) => (
          <div key={row.line_code} className="flex items-center justify-between gap-4 text-sm">
            <span className="min-w-0 truncate">{row.title}</span>
            <span className="tabular-nums shrink-0">
              {row.frozen_amount === null ? "нет данных" : formatMoney(row.frozen_amount)}
              {" → "}
              {row.live_amount === null ? "нет данных" : formatMoney(row.live_amount)}
            </span>
          </div>
        ))}
        {rows.length > 8 ? (
          <div className="text-muted-foreground text-xs">…и ещё {rows.length - 8}</div>
        ) : null}
      </div>
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
        <Badge variant={ok ? "secondary" : "destructive"}>{ok ? "сошлось" : "не объяснено"}</Badge>
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
          Прибыль за период неизвестна ({recon.net_profit_status ?? "нет данных"}), поэтому это не
          «сошлось» и не «не сошлось» — это отсутствие ответа.
        </p>
      ) : recon.net_profit_status && recon.net_profit_status !== "ok" ? (
        // Неполная прибыль хуже неизвестной: она выглядит как цифра. Слагаемых в ОПиУ не
        // хватает, значит прибыль завышена на неизвестную величину, и «сошлось» здесь означает
        // «сошлось с неполной прибылью» — это надо сказать вслух, а не прятать в поле таблицы.
        <p className="text-muted-foreground mt-2 text-xs">
          Прибыль взята со статусом «{recon.net_profit_status}»: в ОПиУ известны не все слагаемые,
          поэтому сама она неполная. Расхождение ниже настолько же приблизительно.
        </p>
      ) : null}
    </Card>
  );
}

type LinePart = { name: string; amount: string; quantity?: string };

/** Состав строки, если сервер его прислал. Строка без состава остаётся обычной — не всякая
 *  величина раскладывается (капитал выведен как остаток, у него слагаемых нет). */
function linePartsOf(line: BalanceLine): LinePart[] {
  const parts = (line.detail as { parts?: unknown } | null)?.parts;
  return Array.isArray(parts) ? (parts as LinePart[]) : [];
}

/** Строка таблицы плюс её расшифровка.
 *
 *  ИТОГ БЕЗ СОСТАВА НЕПРОВЕРЯЕМ. «Выданные авансы поставщикам 252 461,46» — цифра, с которой
 *  владельцу нечего делать: она либо совпадает с ожиданием, либо нет, и во втором случае искать
 *  причину негде. Раскрытие превращает вопрос «откуда столько?» в «почему у этого столько?»,
 *  на который уже можно ответить. */
/** Строка без регистра: величину вносит человек.
 *
 *  ЗАЧЕМ ЭТО ОКНО. Сигнал «строки ждут ручного ввода» висел на каждом срезе, а ввести значение
 *  было НЕГДЕ: ручка `PUT /balance/manual/…` и клиент к ней существовали, но ни один экран их
 *  не звал. Владелец 15.08.2026 спросил прямо: «куда вводить?» — и правильный ответ был
 *  «никуда». Предупреждение, на которое нельзя ответить, — это шум, а не сигнал.
 *
 *  ОСНОВАНИЕ ОБЯЗАТЕЛЬНО (сервер требует три символа). Цифра без объяснения в балансе
 *  неотличима от опечатки: через месяц никто не вспомнит, откуда взялись эти 87 868,08.
 *
 *  ЗНАЧЕНИЕ ЖИВЁТ НА ОДНУ ДАТУ. Остаток на 31 июля не переносится на 31 августа сам — это не
 *  забывчивость, а смысл: величина без регистра на новую дату может быть любой, и подставить
 *  прошлую значило бы выдать позапрошлый эквайринг за сегодняшний. */
function ManualValueDialog({
  line,
  asOf,
  onSaved,
}: {
  line: BalanceLine;
  asOf: string;
  onSaved: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [document, setDocument] = useState("");
  const [saving, setSaving] = useState(false);

  const entered = line.status === "manual";

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const normalized = amount.trim().replace(/\s/g, "").replace(",", ".");
    if (!normalized || !Number.isFinite(Number(normalized))) {
      toast.error("Сумма — число, например 87868.08");
      return;
    }
    if (reason.trim().length < 3) {
      toast.error("Напишите основание: откуда взялась эта величина");
      return;
    }
    setSaving(true);
    try {
      await setBalanceManualValue(asOf, line.line_code, {
        amount: normalized,
        reason: reason.trim(),
        source_document: document.trim() || null,
      });
      toast.success(`«${line.title}» — величина внесена`);
      setOpen(false);
      setAmount("");
      setReason("");
      setDocument("");
      onSaved();
    } catch (error) {
      toast.error(apiErrorMessage(error));
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <button
        type="button"
        className="text-primary text-xs underline-offset-2 hover:underline"
        onClick={() => setOpen(true)}
      >
        {entered ? "изменить" : "ввести"}
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{line.title}</DialogTitle>
            <DialogDescription>
              У строки нет регистра — величину на {asOfLabel(asOf)} вносит человек. Пока она пуста,
              её молча вбирает в себя капитал: он выводится как остаток.
            </DialogDescription>
          </DialogHeader>
          <form className="space-y-3" onSubmit={submit}>
            <div className="space-y-1">
              <Label htmlFor="manual-amount">Сумма, ₽</Label>
              <Input
                id="manual-amount"
                value={amount}
                placeholder="87868.08"
                onChange={(event) => setAmount(event.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="manual-reason">Основание</Label>
              <Input
                id="manual-reason"
                value={reason}
                placeholder="Выписка эквайринга за 31.07, зачисление 01.08"
                onChange={(event) => setReason(event.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="manual-document">Документ</Label>
              <Input
                id="manual-document"
                value={document}
                placeholder="необязательно"
                onChange={(event) => setDocument(event.target.value)}
              />
            </div>
            <p className="text-muted-foreground text-xs">
              Значение относится к одной дате среза. На следующий месяц оно не переносится: величина
              без регистра там может быть другой, и подставить прошлую значило бы выдать её за
              свежую.
            </p>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => setOpen(false)}
                disabled={saving}
              >
                Отмена
              </Button>
              <Button type="submit" disabled={saving}>
                {saving ? "Сохраняем…" : "Сохранить"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}

function LineRows({
  line,
  asOf,
  onSaved,
}: {
  line: BalanceLine;
  asOf: string;
  onSaved: () => void;
}) {
  const [open, setOpen] = useState(false);
  const parts = linePartsOf(line);
  const muted = line.kind === "memo";
  // Ручная строка узнаётся по источнику, а не по пустоте: уже введённую величину тоже надо
  // уметь поправить, а её статус — `manual`, не `missing`.
  const handEntered = line.source_kind === "manual";
  return (
    <>
      <tr className={`border-b last:border-0 ${muted ? "text-muted-foreground" : ""}`}>
        <td className="px-4 py-1.5">
          <div className={muted ? "pl-4 italic" : ""}>
            {parts.length ? (
              <button
                type="button"
                className="text-left underline-offset-2 hover:underline"
                onClick={() => setOpen((value) => !value)}
                aria-expanded={open}
              >
                {line.title}
                <span className="text-muted-foreground ml-2 text-xs">
                  {open ? "свернуть" : `${parts.length}`}
                </span>
              </button>
            ) : (
              line.title
            )}
          </div>
          {line.source_note ? (
            <div className="text-muted-foreground mt-0.5 text-xs">{line.source_note}</div>
          ) : null}
        </td>
        <td className="px-4 py-1.5 text-right align-top">
          <LineAmount line={line} />
          {line.status !== "ok" && line.amount !== null ? (
            <div className="text-muted-foreground text-xs">{STATUS_LABEL[line.status] ?? ""}</div>
          ) : null}
          {handEntered ? (
            <div className="mt-0.5">
              <ManualValueDialog line={line} asOf={asOf} onSaved={onSaved} />
            </div>
          ) : null}
        </td>
      </tr>
      {open
        ? parts.map((part, index) => (
            <tr key={`${line.line_code}-${index}`} className="bg-muted/10 border-b last:border-0">
              <td className="text-muted-foreground py-1 pl-8 pr-4 text-xs">
                {part.name}
                {part.quantity != null ? (
                  <span className="ml-2 tabular-nums">× {part.quantity}</span>
                ) : null}
              </td>
              <td className="tabular-nums text-muted-foreground px-4 py-1 text-right text-xs">
                {formatMoney(part.amount)}
              </td>
            </tr>
          ))
        : null}
    </>
  );
}

/** Одна сторона баланса — актив или пассив — обычной таблицей.
 *
 *  ПОЧЕМУ ТАБЛИЦА, А НЕ КАРТОЧКИ ПО ГРУППАМ. Баланс читают глазами бухгалтера: сверху вниз по
 *  разделам, с подытогом у каждого и одним итогом внизу. Карточки, разложенные по сетке,
 *  показывают те же строки, но порядок в них задаёт вёрстка, а не смысл, и сравнить две стороны
 *  невозможно — они оказываются на разной высоте.
 *
 *  ПАССИВ — ЭТО ОБЯЗАТЕЛЬСТВА ПЛЮС КАПИТАЛ, поэтому итоги сторон совпадают всегда. Равенство
 *  здесь не проверка, а следствие того, что капитал выведен как остаток; ради этого под
 *  таблицей стоит отдельная оговорка. */
function SideTable({
  title,
  total,
  groups,
  note,
  asOf,
  onSaved,
}: {
  title: string;
  total: string;
  groups: { group: string; lines: BalanceLine[]; total: number }[];
  note?: string;
  asOf: string;
  onSaved: () => void;
}) {
  return (
    <Card className="overflow-hidden">
      <div className="bg-muted/40 flex items-baseline justify-between gap-3 border-b px-4 py-2.5">
        <div className="text-sm font-semibold uppercase tracking-wide">{title}</div>
        <div className="tabular-nums text-base font-semibold">{formatMoney(total)} ₽</div>
      </div>
      <table className="w-full text-sm">
        <tbody>
          {groups.map((group) => (
            <Fragment key={group.group}>
              <tr className="bg-muted/20 border-b">
                <th
                  scope="rowgroup"
                  className="px-4 py-1.5 text-left text-xs font-medium uppercase tracking-wide"
                >
                  {group.group}
                </th>
                <td className="tabular-nums px-4 py-1.5 text-right text-xs font-medium">
                  {group.total.toLocaleString("ru-RU", {
                    minimumFractionDigits: 2,
                    maximumFractionDigits: 2,
                  })}
                </td>
              </tr>
              {group.lines.map((line) => (
                <LineRows key={line.line_code} line={line} asOf={asOf} onSaved={onSaved} />
              ))}
            </Fragment>
          ))}
          <tr className="bg-muted/40 border-t">
            <td className="px-4 py-2 text-sm font-semibold">Итого {title.toLowerCase()}</td>
            <td className="tabular-nums px-4 py-2 text-right text-sm font-semibold">
              {formatMoney(total)}
            </td>
          </tr>
        </tbody>
      </table>
      {note ? <div className="text-muted-foreground px-4 py-2 text-xs">{note}</div> : null}
    </Card>
  );
}

export function BalanceRoute() {
  const [asOf, setAsOf] = useState(defaultAsOf);
  const [view, setView] = useState<BalanceView | null>(null);
  const [drift, setDrift] = useState<BalanceDriftRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [freezing, setFreezing] = useState(false);

  const load = useCallback(async (date: string) => {
    setLoading(true);
    try {
      // Сначала подписанный снимок: он и есть ответ на вопрос «сколько мы сказали».
      // Нет снимка — показываем предпросмотр, то есть «сколько получается сейчас».
      let data: BalanceView;
      let rows: BalanceDriftRow[] = [];
      try {
        data = await fetchBalanceSnapshot(date);
        // Снимок числа НЕ пересчитывает — в этом весь его смысл. Но тогда экран обязан
        // сказать, что живой расчёт с ним разошёлся: иначе владелец смотрит на цифры
        // недельной давности и уверен, что видит сегодняшние. Ровно так и вышло 08.08.2026 —
        // черновик, собранный до правок, показывал прежний актив, и это выглядело как «правки
        // не приехали», хотя модуль работал верно.
        rows = await fetchBalanceDrift(date).catch(() => []);
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
      setDrift(Array.isArray(rows) ? rows : []);
    } catch (error) {
      toast.error(apiErrorMessage(error));
      setView(null);
      setDrift([]);
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

  // Пассив = обязательства + капитал. Разделение на блоки приходит с сервера, здесь только
  // раскладка по двум сторонам: порядок внутри каждой уже задан сортировкой строк.
  const assetGroups = useMemo(() => groups.filter((g) => g.block === "asset"), [groups]);
  const liabilityGroups = useMemo(
    () => groups.filter((g) => g.block === "liability" || g.block === "equity"),
    [groups],
  );
  const liabilitySideTotal = view
    ? (Number(view.liability_total) + Number(view.equity_plug)).toFixed(2)
    : "0";

  const onFreeze = async () => {
    setFreezing(true);
    try {
      setView(await freezeBalance(asOf));
      // Пересобрали — расхождение снято по построению, спрашивать сервер незачем.
      setDrift([]);
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
    for (let back = 1; back <= 24; back += 1) {
      const value = lastDayOfMonth(now.getFullYear(), now.getMonth() - back);
      // Ниже начала учёта не спускаемся: сервер такую дату отвергает, и предложить её значило бы
      // звать владельца в ошибку. Список кончается там, где кончаются данные.
      if (value < ACCOUNTING_START) break;
      out.push(value);
    }
    return out;
  }, []);

  return (
    <div className="space-y-4 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">
            Баланс <span className="text-muted-foreground font-normal">на {asOfLabel(asOf)}</span>
          </h1>
          <p className="text-muted-foreground text-sm">
            Состояние на конец дня — то есть итог месяца, а не его начало. Капитал выводится как
            актив минус обязательства, поэтому равенство сторон здесь не проверка — проверяет сверка
            приращения.
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
                на {asOfLabel(value)}
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
          {/* Расхождение — ПЕРЕД цифрами: узнать, что смотришь на устаревшее, надо до того,
              как поверишь итогам, а не после. */}
          {drift.length ? (
            <DriftCard rows={drift} status={view.status} onRebuild={onFreeze} busy={freezing} />
          ) : null}

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

          <div className="grid items-start gap-3 lg:grid-cols-2">
            <SideTable
              title="Актив"
              total={view.asset_total}
              groups={assetGroups}
              asOf={asOf}
              onSaved={() => void load(asOf)}
            />
            <SideTable
              title="Пассив"
              total={liabilitySideTotal}
              groups={liabilityGroups}
              asOf={asOf}
              onSaved={() => void load(asOf)}
              note={
                "Пассив = обязательства + капитал, а капитал выведен как актив минус " +
                "обязательства. Поэтому итоги сторон совпадают всегда — это следствие " +
                "построения, а не проверка. Проверяет сверка приращения выше."
              }
            />
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
