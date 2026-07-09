import * as React from "react";
import { Check, ChevronDown } from "lucide-react";

import { cn } from "@/lib/utils";

export type ComboboxOption = {
  value: string;
  label: string;
  // Дополнительный текст для поиска (например ИНН) — не показывается, но матчится по вводу.
  keywords?: string;
};

/**
 * Встроенный (не выпадающий) поиск-список: поле поиска + высокий прокручиваемый список прямо
 * в потоке. Для модалок выбора, где выпадающий поповер тесноват — список виден целиком.
 */
export function InlineOptionList({
  options,
  value,
  onChange,
  searchPlaceholder = "Поиск…",
  emptyMessage = "Ничего не найдено",
  listClassName = "max-h-72",
  autoFocus = true,
}: {
  options: ComboboxOption[];
  value: string;
  onChange: (value: string) => void;
  searchPlaceholder?: string;
  emptyMessage?: string;
  listClassName?: string;
  autoFocus?: boolean;
}) {
  const [query, setQuery] = React.useState("");
  const filtered = React.useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return options;
    return options.filter((option) =>
      `${option.label} ${option.keywords ?? ""}`.toLowerCase().includes(needle),
    );
  }, [options, query]);

  return (
    <div className="overflow-hidden rounded-md border">
      <div className="border-b p-2">
        <input
          // eslint-disable-next-line jsx-a11y/no-autofocus
          autoFocus={autoFocus}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={searchPlaceholder}
          className="h-9 w-full rounded-sm bg-transparent px-2 text-sm outline-none placeholder:text-muted-foreground"
        />
      </div>
      <div className={cn("overflow-y-auto p-1", listClassName)}>
        {filtered.length === 0 ? (
          <div className="px-2 py-4 text-center text-sm text-muted-foreground">{emptyMessage}</div>
        ) : (
          filtered.map((option) => (
            <button
              type="button"
              key={option.value}
              onClick={() => onChange(option.value)}
              className={cn(
                "flex w-full items-center justify-between gap-2 rounded-sm px-2 py-2 text-left text-sm outline-none hover:bg-accent hover:text-accent-foreground",
                option.value === value ? "bg-accent text-accent-foreground" : "",
              )}
            >
              <span className="line-clamp-1">{option.label}</span>
              {option.value === value ? (
                <Check className="h-4 w-4 shrink-0" aria-hidden="true" />
              ) : null}
            </button>
          ))
        )}
      </div>
    </div>
  );
}

/**
 * Поиск-селект с фильтрацией по мере ввода. Самодостаточный (без cmdk/popover):
 * кнопка-триггер + выпадающая панель с полем поиска и отфильтрованным списком.
 * Закрывается по клику вне и Escape, навигация стрелками + Enter.
 */
export function Combobox({
  options,
  value,
  onChange,
  placeholder = "Выберите…",
  searchPlaceholder = "Поиск…",
  emptyMessage = "Ничего не найдено",
  disabled = false,
  className,
  id,
}: {
  options: ComboboxOption[];
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  searchPlaceholder?: string;
  emptyMessage?: string;
  disabled?: boolean;
  className?: string;
  id?: string;
}) {
  const [open, setOpen] = React.useState(false);
  const [query, setQuery] = React.useState("");
  const [active, setActive] = React.useState(0);
  const rootRef = React.useRef<HTMLDivElement>(null);
  const inputRef = React.useRef<HTMLInputElement>(null);
  const listRef = React.useRef<HTMLDivElement>(null);

  const selected = options.find((option) => option.value === value) ?? null;

  const filtered = React.useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return options;
    return options.filter((option) =>
      `${option.label} ${option.keywords ?? ""}`.toLowerCase().includes(needle),
    );
  }, [options, query]);

  // Клик вне — закрываем.
  React.useEffect(() => {
    if (!open) return;
    function onPointerDown(event: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [open]);

  // При открытии — сброс запроса и фокус в поиск.
  React.useEffect(() => {
    if (!open) return;
    setQuery("");
    setActive(0);
    const timer = setTimeout(() => inputRef.current?.focus(), 0);
    return () => clearTimeout(timer);
  }, [open]);

  React.useEffect(() => setActive(0), [query]);

  // Держим активный пункт в зоне видимости при навигации стрелками.
  React.useEffect(() => {
    if (!open) return;
    listRef.current
      ?.querySelector<HTMLElement>(`[data-index="${active}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [active, open]);

  function choose(next: string) {
    onChange(next);
    setOpen(false);
  }

  function onKeyDown(event: React.KeyboardEvent) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((current) => Math.min(current + 1, filtered.length - 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((current) => Math.max(current - 1, 0));
    } else if (event.key === "Enter") {
      event.preventDefault();
      const option = filtered[active];
      if (option) choose(option.value);
    } else if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
    }
  }

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        id={id}
        disabled={disabled}
        onClick={() => setOpen((current) => !current)}
        className={cn(
          "flex h-10 w-full items-center justify-between rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50",
          !selected && "text-muted-foreground",
          className,
        )}
      >
        <span className="line-clamp-1 text-left">{selected ? selected.label : placeholder}</span>
        <ChevronDown className="h-4 w-4 shrink-0 opacity-50" aria-hidden="true" />
      </button>
      {open ? (
        <div className="absolute z-50 mt-1 w-full overflow-hidden rounded-md border bg-popover text-popover-foreground shadow-md">
          <div className="border-b p-2">
            <input
              ref={inputRef}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={onKeyDown}
              placeholder={searchPlaceholder}
              className="h-8 w-full rounded-sm bg-transparent px-2 text-sm outline-none placeholder:text-muted-foreground"
            />
          </div>
          <div ref={listRef} className="max-h-60 overflow-y-auto p-1">
            {filtered.length === 0 ? (
              <div className="px-2 py-3 text-center text-sm text-muted-foreground">
                {emptyMessage}
              </div>
            ) : (
              filtered.map((option, index) => (
                <button
                  type="button"
                  key={option.value}
                  data-index={index}
                  onClick={() => choose(option.value)}
                  onMouseEnter={() => setActive(index)}
                  className={cn(
                    "flex w-full items-center justify-between gap-2 rounded-sm px-2 py-1.5 text-left text-sm outline-none",
                    index === active ? "bg-accent text-accent-foreground" : "",
                  )}
                >
                  <span className="line-clamp-1">{option.label}</span>
                  {option.value === value ? (
                    <Check className="h-4 w-4 shrink-0" aria-hidden="true" />
                  ) : null}
                </button>
              ))
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}
