import * as React from "react";
import { Calendar, ChevronLeft, ChevronRight, X } from "lucide-react";

import { cn } from "@/lib/utils";

const MONTHS = [
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
const WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

const pad = (n: number) => String(n).padStart(2, "0");
const toISO = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const parseISO = (s: string) => {
  const [y, m, d] = s.split("-").map(Number);
  return new Date(y, m - 1, d);
};
const shortLabel = (s: string) => {
  const [, m, d] = s.split("-");
  return `${d}.${m}`;
};
const fullLabel = (s: string) => {
  const [y, m, d] = s.split("-");
  return `${d}.${m}.${y}`;
};

/**
 * Диапазонный календарь в одном поповере: клик по первой дате, затем по второй —
 * выделяется весь период. Самодостаточный (без popover/day-picker), в стиле Combobox:
 * кнопка-триггер + выпадающая панель, закрытие по клику вне и Escape.
 */
export function DateRangePopover({
  from,
  to,
  onChange,
  placeholder = "Все даты",
  className,
  id,
}: {
  from: string | null;
  to: string | null;
  onChange: (from: string | null, to: string | null) => void;
  placeholder?: string;
  className?: string;
  id?: string;
}) {
  const [open, setOpen] = React.useState(false);
  // Начало диапазона держим ВНУТРИ компонента между первым и вторым кликом: наружу отдаём
  // только завершённый диапазон (или очистку/пресет). Иначе контролируемый родитель, хранящий
  // лишь полный период (график), теряет промежуточное состояние и диапазон не завершается.
  const [draftFrom, setDraftFrom] = React.useState<string | null>(null);
  const anchor = from ? parseISO(from) : new Date();
  const [view, setView] = React.useState({ y: anchor.getFullYear(), m: anchor.getMonth() });
  const rootRef = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    if (!open) {
      setDraftFrom(null);
      return;
    }
    const base = from ? parseISO(from) : new Date();
    setView({ y: base.getFullYear(), m: base.getMonth() });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  React.useEffect(() => {
    if (!open) return;
    function onPointerDown(event: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false);
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  function pick(iso: string) {
    if (draftFrom === null) {
      // первый клик — запоминаем начало внутри компонента, наружу пока не отдаём
      setDraftFrom(iso);
      return;
    }
    // второй клик — завершаем диапазон и коммитим полный период
    const [lo, hi] = iso < draftFrom ? [iso, draftFrom] : [draftFrom, iso];
    setDraftFrom(null);
    onChange(lo, hi);
    setOpen(false);
  }

  function shift(delta: number) {
    setView((cur) => {
      const next = cur.m + delta;
      if (next < 0) return { y: cur.y - 1, m: 11 };
      if (next > 11) return { y: cur.y + 1, m: 0 };
      return { y: cur.y, m: next };
    });
  }

  function preset(kind: "7d" | "month" | "clear") {
    setDraftFrom(null);
    if (kind === "clear") {
      onChange(null, null);
      setOpen(false);
      return;
    }
    const now = new Date();
    if (kind === "7d") {
      const start = new Date(now);
      start.setDate(start.getDate() - 6);
      onChange(toISO(start), toISO(now));
      setView({ y: start.getFullYear(), m: start.getMonth() });
    } else {
      const start = new Date(now.getFullYear(), now.getMonth(), 1);
      const end = new Date(now.getFullYear(), now.getMonth() + 1, 0);
      onChange(toISO(start), toISO(end));
      setView({ y: start.getFullYear(), m: start.getMonth() });
    }
    setOpen(false);
  }

  const label = from
    ? to
      ? `${shortLabel(from)} – ${fullLabel(to)}`
      : `с ${fullLabel(from)}`
    : placeholder;

  const offset = (new Date(view.y, view.m, 1).getDay() + 6) % 7;
  const daysInMonth = new Date(view.y, view.m + 1, 0).getDate();
  const cells: Array<string | null> = [];
  for (let i = 0; i < offset; i += 1) cells.push(null);
  for (let d = 1; d <= daysInMonth; d += 1) cells.push(toISO(new Date(view.y, view.m, d)));
  const todayIso = toISO(new Date());
  // Во время выбора подсвечиваем незавершённый диапазон: начало — из драфта, конца ещё нет.
  const activeFrom = draftFrom ?? from;
  const activeTo = draftFrom ? null : to;

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        id={id}
        onClick={() => setOpen((cur) => !cur)}
        className={cn(
          "flex h-9 items-center gap-2 rounded-md border border-input bg-background px-3 text-sm ring-offset-background focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2",
          from ? "font-medium" : "text-muted-foreground",
          className,
        )}
      >
        <Calendar className="h-4 w-4 shrink-0 opacity-70" aria-hidden="true" />
        <span className="line-clamp-1">{label}</span>
        {from ? (
          <X
            className="h-3.5 w-3.5 shrink-0 opacity-60 hover:opacity-100"
            aria-hidden="true"
            onClick={(event) => {
              event.stopPropagation();
              setDraftFrom(null);
              onChange(null, null);
            }}
          />
        ) : null}
      </button>
      {open ? (
        <div className="absolute z-50 mt-1 w-[280px] rounded-md border bg-popover p-3 text-popover-foreground shadow-md">
          <div className="mb-2 flex items-center justify-between">
            <button
              type="button"
              onClick={() => shift(-1)}
              aria-label="Предыдущий месяц"
              className="flex h-7 w-7 items-center justify-center rounded-md hover:bg-accent"
            >
              <ChevronLeft className="h-4 w-4" aria-hidden="true" />
            </button>
            <span className="text-sm font-medium">
              {MONTHS[view.m]} {view.y}
            </span>
            <button
              type="button"
              onClick={() => shift(1)}
              aria-label="Следующий месяц"
              className="flex h-7 w-7 items-center justify-center rounded-md hover:bg-accent"
            >
              <ChevronRight className="h-4 w-4" aria-hidden="true" />
            </button>
          </div>
          <div className="grid grid-cols-7 gap-1">
            {WEEKDAYS.map((w) => (
              <div key={w} className="flex h-6 items-center justify-center text-xs text-muted-foreground">
                {w}
              </div>
            ))}
            {cells.map((iso, index) => {
              if (iso === null) return <div key={`e${index}`} />;
              const isEnd = iso === activeFrom || iso === activeTo;
              const inRange = !!activeFrom && !!activeTo && iso >= activeFrom && iso <= activeTo;
              return (
                <button
                  type="button"
                  key={iso}
                  onClick={() => pick(iso)}
                  className={cn(
                    "flex h-8 items-center justify-center rounded-md text-sm",
                    !isEnd && !inRange && "hover:bg-accent",
                    inRange && !isEnd && "bg-accent",
                    isEnd && "bg-primary font-medium text-primary-foreground",
                    !isEnd && iso === todayIso && "ring-1 ring-inset ring-primary/50",
                  )}
                >
                  {Number(iso.slice(-2))}
                </button>
              );
            })}
          </div>
          <div className="mt-3 flex items-center gap-1 border-t pt-3">
            <button
              type="button"
              onClick={() => preset("7d")}
              className="rounded-md px-2 py-1 text-xs hover:bg-accent"
            >
              7 дней
            </button>
            <button
              type="button"
              onClick={() => preset("month")}
              className="rounded-md px-2 py-1 text-xs hover:bg-accent"
            >
              Этот месяц
            </button>
            <button
              type="button"
              onClick={() => preset("clear")}
              className="ml-auto rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-accent"
            >
              Очистить
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
