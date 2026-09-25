import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowDownRight,
  ArrowLeftRight,
  ArrowUpRight,
  CircleCheck,
  Clock,
  HandCoins,
  Undo2,
  type LucideIcon,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { apiErrorMessage } from "@/lib/api";

import { getSettlementLedger, type LedgerRow, type LedgerRowKind } from "./api";
import { formatDate, formatRub } from "./shared";

/** Как строка выглядит в хронологии: значок, знак суммы и её цвет.
 *
 *  Возврат, закрытие решением и оплата чужого документа гасят остаток так же, как документ, —
 *  поэтому со знаком «−», но своим значком: документом они не являются. Выплата дивидендов
 *  остаток не двигает, и знака у неё нет: «+» читался бы как аванс собственнику. */
const ROW_LOOK: Record<LedgerRowKind, { icon: LucideIcon; sign: string; tone: string }> = {
  payment: { icon: ArrowUpRight, sign: "+", tone: "text-sky-700" },
  document: { icon: ArrowDownRight, sign: "−", tone: "text-emerald-700" },
  refund: { icon: Undo2, sign: "−", tone: "text-amber-700" },
  closure: { icon: CircleCheck, sign: "−", tone: "text-violet-700" },
  transfer: { icon: ArrowLeftRight, sign: "−", tone: "text-slate-600" },
  payout: { icon: HandCoins, sign: "", tone: "text-muted-foreground" },
};

/** Подпись движений месяца, которые не платёж и не документ, — чтобы подытог объяснял остаток. */
const MOVE_LABELS: Partial<Record<LedgerRowKind, string>> = {
  refund: "возвраты",
  closure: "закрыто без документа",
  transfer: "оплачены чужие документы",
  payout: "дивиденды",
};

const MONTH_LABELS = [
  "январь",
  "февраль",
  "март",
  "апрель",
  "май",
  "июнь",
  "июль",
  "август",
  "сентябрь",
  "октябрь",
  "ноябрь",
  "декабрь",
];

function monthTitle(month: string): string {
  const [year, index] = month.split("-");
  return `${MONTH_LABELS[Number(index) - 1] ?? month} ${year}`;
}

function periodLabel(row: LedgerRow): string {
  // У займа, дивидендов, возврата и оплаты чужого документа периода услуги нет вовсе —
  // «не заполнен» звало бы его заполнить.
  if (!row.period_start || !row.period_end) {
    return row.owner_settlement || (row.kind !== "payment" && row.kind !== "document")
      ? "—"
      : "не заполнен";
  }
  const start = new Date(row.period_start);
  const end = new Date(row.period_end);
  // Целый календарный месяц — самый частый случай: показываем его словом, а не двумя датами.
  const wholeMonth =
    start.getDate() === 1 &&
    start.getMonth() === end.getMonth() &&
    end.getDate() === new Date(end.getFullYear(), end.getMonth() + 1, 0).getDate();
  const value = wholeMonth
    ? monthTitle(row.period_start.slice(0, 7))
    : `${formatDate(row.period_start)} — ${formatDate(row.period_end)}`;
  return row.period_assumed ? `≈ ${value}` : value;
}

function StatusCell({ row }: { row: LedgerRow }) {
  if (row.kind === "payout") {
    return <span className="text-xs text-muted-foreground">выплата собственнику · не долг</span>;
  }
  if (row.kind === "refund") {
    // Излишек сверх открытой дебиторки — обычный приход: гасить ему нечего, и молчать о нём
    // нельзя — иначе непонятно, почему остаток сдвинулся меньше суммы возврата.
    return row.uncovered > 0 ? (
      <Badge variant="outline" className="border-amber-200 bg-amber-50 text-amber-800">
        не по предоплате: {formatRub(row.uncovered)}
      </Badge>
    ) : (
      <span className="text-xs text-muted-foreground">деньги вернулись · аванс погашен</span>
    );
  }
  if (row.kind === "closure") {
    return <span className="text-xs text-muted-foreground">решение человека · без документа</span>;
  }
  if (row.kind === "transfer") {
    return <span className="text-xs text-muted-foreground">долг гасится у получателя</span>;
  }
  if (row.kind === "document") {
    // Самоакт называем своим именем: это НАШЕ признание расхода, а не документ поставщика.
    // Спутать их нельзя — от этого зависит, попадёт ли расход в налоговую базу.
    if (row.self_billed) {
      return (
        <Badge variant="outline" className="border-violet-200 bg-violet-50 text-violet-700">
          признано нами · без первички
        </Badge>
      );
    }
    return row.uncovered > 0 ? (
      <Badge variant="outline" className="border-amber-200 bg-amber-50 text-amber-800">
        не оплачен: {formatRub(row.uncovered)}
      </Badge>
    ) : (
      <span className="text-xs text-muted-foreground">закрыт</span>
    );
  }
  // Раньше статуса «ok»: у такой строки документа нет и не будет, и «закрыт документом» про
  // неё было бы неправдой.
  if (row.owner_settlement) {
    return (
      <span className="text-xs text-muted-foreground">
        расчёт с собственником · документа не будет
      </span>
    );
  }
  if (row.status === "ok") {
    // Закрыт — ещё не значит «документом»: предоплату мог закрыть человек или вернуть поставщик.
    return (
      <span className="text-xs text-muted-foreground">
        {row.closed_by === "decision"
          ? "закрыт решением"
          : row.closed_by === "refund"
            ? "деньги вернули"
            : "закрыт документом"}
      </span>
    );
  }
  if (row.status === "waiting") {
    return (
      <Badge variant="outline" className="border-slate-200 bg-slate-50 text-slate-600">
        <Clock size={12} className="mr-1" aria-hidden="true" />
        ждём документ{row.expected_by ? ` до ${formatDate(row.expected_by)}` : ""}
      </Badge>
    );
  }
  return (
    <Badge variant="outline" className="border-rose-200 bg-rose-50 text-rose-700">
      <AlertTriangle size={12} className="mr-1" aria-hidden="true" />
      документа нет · {row.days_overdue} дн
    </Badge>
  );
}

/** Сверка расчётов: платежи и закрывающие документы одной хронологией.
 *
 *  Отвечает на вопрос, ради которого экран и сделан: «мы заплатили — закрыли ли это
 *  документом?». Бегущий остаток читается как банковская выписка: положительный —
 *  мы заплатили вперёд (дебиторка), отрицательный — должны мы (кредиторка). Его итог
 *  сходится с плиткой «Остатки» на странице ДЗ/КЗ: обе цифры считаются по одним аллокациям.
 */
export function SettlementLedgerSection({ counterpartyId }: { counterpartyId: string }) {
  const query = useQuery({
    queryKey: ["counterparties", "ledger", counterpartyId],
    queryFn: () => getSettlementLedger(counterpartyId),
  });

  const monthByKey = useMemo(() => {
    const map = new Map<string, { paid: number; documented: number; gap: number }>();
    (query.data?.months ?? []).forEach((month) => map.set(month.month, month));
    return map;
  }, [query.data?.months]);

  // Месяцы, где все строки — расчёты с собственником. «Закрыт полностью» там читалось бы как
  // «документы получены», хотя их не было и не будет.
  const ownerOnlyMonths = useMemo(() => {
    const owner = new Map<string, boolean>();
    (query.data?.rows ?? []).forEach((row) => {
      const key = row.row_date.slice(0, 7);
      owner.set(key, (owner.get(key) ?? true) && row.owner_settlement);
    });
    return owner;
  }, [query.data?.rows]);

  // Движения месяца помимо платежей и документов: без них подытог «платежи · документы» не
  // объяснял бы, почему остаток месяца сдвинулся (вернули деньги, закрыли решением…).
  const movesByMonth = useMemo(() => {
    const moves = new Map<string, Map<LedgerRowKind, number>>();
    (query.data?.rows ?? []).forEach((row) => {
      if (!MOVE_LABELS[row.kind]) return;
      const key = row.row_date.slice(0, 7);
      const month = moves.get(key) ?? new Map<LedgerRowKind, number>();
      month.set(row.kind, (month.get(row.kind) ?? 0) + row.amount);
      moves.set(key, month);
    });
    return moves;
  }, [query.data?.rows]);

  if (query.isLoading) {
    return <p className="text-sm text-muted-foreground">Загружаем сверку…</p>;
  }
  if (query.isError) {
    return (
      <p className="text-sm text-rose-700">
        {apiErrorMessage(query.error, "Не удалось загрузить сверку")}
      </p>
    );
  }

  const ledger = query.data;
  if (!ledger) return null;

  if (ledger.rows.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        С этим контрагентом ещё не было ни платежей, ни закрывающих документов.
        {ledger.has_barter ? " Бартерные обязательства смотрите на вкладке «Общая информация»." : ""}
      </p>
    );
  }

  // Разделители месяцев вставляем на лету: строки идут свежими сверху, и подытог
  // показывается перед первой строкой своего месяца.
  let lastMonth: string | null = null;
  // Остаток держат расчёты с собственником, а документа не ждёт ни один платёж: «ждём документы»
  // в подсказке было бы неправдой — в том числе у собственника-подрядчика с закрытым УПД.
  const ownerNoDocuments =
    ledger.rows.some((row) => row.owner_settlement) &&
    !ledger.rows.some((row) => row.kind === "payment" && !row.owner_settlement && row.uncovered > 0);

  return (
    <div className="space-y-3">
      <div className="grid gap-2 sm:grid-cols-3 lg:grid-cols-4">
        <SummaryTile
          title="Остаток расчётов"
          value={formatRub(Math.abs(ledger.closing_balance))}
          hint={
            ledger.closing_balance === 0
              ? "расчёты закрыты"
              : ledger.closing_balance > 0
                ? ownerNoDocuments
                  ? "расчёты с собственником — документов по ним не будет"
                  : "мы заплатили вперёд — ждём документы"
                : "мы должны по документам"
          }
          tone={ledger.closing_balance > 0 ? "sky" : ledger.closing_balance < 0 ? "rose" : "muted"}
        />
        <SummaryTile
          title="Без документов"
          value={formatRub(ledger.overdue_amount)}
          hint={
            ledger.overdue_amount > 0
              ? "срок закрывающего документа прошёл"
              : "просроченных документов нет"
          }
          tone={ledger.overdue_amount > 0 ? "rose" : "muted"}
        />
        {ledger.self_billed_amount > 0 ? (
          <SummaryTile
            title="Признано без первички"
            value={formatRub(ledger.self_billed_amount)}
            hint="есть в P&L, в налоговых расходах — нет"
            tone="violet"
          />
        ) : null}
        <SummaryTile
          title="Всего за период"
          value={`${formatRub(ledger.total_paid)} / ${formatRub(ledger.total_documented)}`}
          hint="заплачено / подтверждено документами"
          tone="muted"
        />
      </div>

      {ledger.has_barter ? (
        <p className="rounded-md bg-muted/50 p-2 text-xs text-muted-foreground">
          У контрагента есть бартерные обязательства — они гасятся товаром и в эту сверку не
          входят: у бартера свой нетто-контур.
        </p>
      ) : null}

      {/* overflow-x-auto, а не hidden: на узком экране таблица должна прокручиваться,
          а не отрезать колонку остатка. */}
      <div className="overflow-x-auto rounded-lg border bg-background">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-24">Дата</TableHead>
              <TableHead>Событие</TableHead>
              <TableHead className="w-36">Период услуги</TableHead>
              <TableHead className="w-32 text-right">Сумма</TableHead>
              <TableHead className="w-52">Состояние</TableHead>
              <TableHead className="w-32 text-right">Остаток</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {ledger.rows.map((row) => {
              // По дате события — тот же ключ, что и в месячных подытогах на бэкенде.
              const monthKey = row.row_date.slice(0, 7);
              const showMonth = monthKey !== lastMonth;
              lastMonth = monthKey;
              const month = monthByKey.get(monthKey);
              const look = ROW_LOOK[row.kind] ?? ROW_LOOK.payment;
              const RowIcon = look.icon;
              return (
                <>
                  {showMonth && month ? (
                    <TableRow key={`m:${monthKey}`} className="bg-muted/40 hover:bg-muted/40">
                      <TableCell colSpan={6} className="py-1.5 text-xs">
                        <span className="font-medium uppercase">{monthTitle(monthKey)}</span>
                        <span className="ml-3 text-muted-foreground">
                          платежи {formatRub(month.paid)} · документы {formatRub(month.documented)}
                          {Array.from(movesByMonth.get(monthKey) ?? []).map(([kind, amount]) => (
                            <span key={kind}>
                              {" "}
                              · {MOVE_LABELS[kind]} {formatRub(amount)}
                            </span>
                          ))}
                        </span>
                        {month.gap > 0 ? (
                          <span className="ml-3 font-medium text-rose-700">
                            без документов {formatRub(month.gap)}
                          </span>
                        ) : ownerOnlyMonths.get(monthKey) ? (
                          <span className="ml-3 text-muted-foreground">документов не ждём</span>
                        ) : (
                          <span className="ml-3 text-emerald-700">закрыт полностью</span>
                        )}
                      </TableCell>
                    </TableRow>
                  ) : null}
                  <TableRow key={`${row.kind}:${row.id}`}>
                    <TableCell className="whitespace-nowrap text-sm">
                      {formatDate(row.row_date)}
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center gap-1.5 text-sm font-medium">
                        <RowIcon size={14} className={look.tone} aria-hidden="true" />
                        {row.title}
                      </div>
                      {row.subtitle ? (
                        <div className="text-xs text-muted-foreground">{row.subtitle}</div>
                      ) : null}
                    </TableCell>
                    <TableCell
                      className="text-xs text-muted-foreground"
                      title={
                        row.period_assumed
                          ? "Предположение по дате платежа — период не заполнен вручную"
                          : undefined
                      }
                    >
                      {periodLabel(row)}
                    </TableCell>
                    <TableCell className={`text-right font-semibold tabular-nums ${look.tone}`}>
                      {look.sign}
                      {formatRub(row.amount)}
                    </TableCell>
                    <TableCell>
                      <StatusCell row={row} />
                    </TableCell>
                    <TableCell
                      className={`text-right tabular-nums ${
                        row.balance_after > 0
                          ? "text-sky-700"
                          : row.balance_after < 0
                            ? "text-rose-700"
                            : "text-muted-foreground"
                      }`}
                    >
                      {formatRub(row.balance_after)}
                    </TableCell>
                  </TableRow>
                </>
              );
            })}
          </TableBody>
        </Table>
      </div>
      <p className="text-xs text-muted-foreground">
        Остаток в строке — состояние расчётов после неё: положительный значит, что мы заплатили
        вперёд и ждём закрывающий документ, отрицательный — что документ пришёл, а оплаты по нему
        ещё нет. Возврат денег, закрытие предоплаты решением и оплата документа другого
        контрагента гасят остаток без документа; выплата дивидендов видна, но остаток не двигает —
        это не долг. Итог сходится с плиткой «Остатки» на странице ДЗ/КЗ.
      </p>
    </div>
  );
}

function SummaryTile({
  title,
  value,
  hint,
  tone,
}: {
  title: string;
  value: string;
  hint: string;
  tone: "sky" | "rose" | "violet" | "muted";
}) {
  const toneClass =
    tone === "sky"
      ? "text-sky-700"
      : tone === "rose"
        ? "text-rose-700"
        : tone === "violet"
          ? "text-violet-700"
          : "text-foreground";
  return (
    <div className="rounded-lg border bg-background p-3">
      <div className="text-xs uppercase text-muted-foreground">{title}</div>
      <div className={`text-lg font-semibold tabular-nums ${toneClass}`}>{value}</div>
      <div className="text-xs text-muted-foreground">{hint}</div>
    </div>
  );
}
