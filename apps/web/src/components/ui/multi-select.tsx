import * as React from "react";
import { Check, ChevronDown } from "lucide-react";

import { cn } from "@/lib/utils";

export type MultiSelectOption = {
  value: string;
  label: string;
  // Дополнительный текст для поиска (ИНН и т.п.) — матчится, но не показывается.
  keywords?: string;
};

/**
 * Мультивыбор с поиском и чекбоксами. Самодостаточный (без cmdk/popover), в стиле
 * Combobox: кнопка-триггер + выпадающая панель с полем поиска и списком. Закрывается
 * по клику вне и Escape. Выбор возвращается массивом значений.
 */
export function MultiSelect({
  options,
  selected,
  onChange,
  placeholder = "Выберите…",
  searchPlaceholder = "Поиск…",
  emptyMessage = "Ничего не найдено",
  icon,
  className,
  id,
}: {
  options: MultiSelectOption[];
  selected: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  searchPlaceholder?: string;
  emptyMessage?: string;
  icon?: React.ReactNode;
  className?: string;
  id?: string;
}) {
  const [open, setOpen] = React.useState(false);
  const [query, setQuery] = React.useState("");
  const rootRef = React.useRef<HTMLDivElement>(null);
  const inputRef = React.useRef<HTMLInputElement>(null);
  const selectedSet = React.useMemo(() => new Set(selected), [selected]);

  const filtered = React.useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return options;
    return options.filter((option) =>
      `${option.label} ${option.keywords ?? ""}`.toLowerCase().includes(needle),
    );
  }, [options, query]);

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

  React.useEffect(() => {
    if (!open) return;
    setQuery("");
    const timer = setTimeout(() => inputRef.current?.focus(), 0);
    return () => clearTimeout(timer);
  }, [open]);

  function toggle(value: string) {
    const next = new Set(selectedSet);
    if (next.has(value)) {
      next.delete(value);
    } else {
      next.add(value);
    }
    onChange([...next]);
  }

  const triggerLabel =
    selected.length === 0
      ? placeholder
      : selected.length === 1
        ? (options.find((option) => option.value === selected[0])?.label ?? placeholder)
        : `Выбрано: ${selected.length}`;

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        id={id}
        onClick={() => setOpen((cur) => !cur)}
        className={cn(
          "flex h-9 items-center gap-2 rounded-md border border-input bg-background px-3 text-sm ring-offset-background focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2",
          selected.length === 0 ? "text-muted-foreground" : "font-medium",
          className,
        )}
      >
        {icon ? (
          <span className="shrink-0 opacity-70" aria-hidden="true">
            {icon}
          </span>
        ) : null}
        <span className="line-clamp-1">{triggerLabel}</span>
        <ChevronDown className="h-4 w-4 shrink-0 opacity-50" aria-hidden="true" />
      </button>
      {open ? (
        <div className="absolute z-50 mt-1 w-[260px] overflow-hidden rounded-md border bg-popover text-popover-foreground shadow-md">
          <div className="flex items-center justify-between gap-2 border-b p-2">
            <input
              ref={inputRef}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={searchPlaceholder}
              className="h-7 flex-1 bg-transparent px-1 text-sm outline-none placeholder:text-muted-foreground"
            />
            {selected.length > 0 ? (
              <button
                type="button"
                onClick={() => onChange([])}
                className="shrink-0 rounded-sm px-1.5 py-0.5 text-xs text-muted-foreground hover:bg-accent"
              >
                Сбросить
              </button>
            ) : null}
          </div>
          <div className="max-h-64 overflow-y-auto p-1">
            {filtered.length === 0 ? (
              <div className="px-2 py-3 text-center text-sm text-muted-foreground">
                {emptyMessage}
              </div>
            ) : (
              filtered.map((option) => {
                const isSelected = selectedSet.has(option.value);
                return (
                  <button
                    type="button"
                    key={option.value}
                    onClick={() => toggle(option.value)}
                    className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-sm outline-none hover:bg-accent"
                  >
                    <span
                      className={cn(
                        "flex h-4 w-4 shrink-0 items-center justify-center rounded-sm border",
                        isSelected
                          ? "border-primary bg-primary text-primary-foreground"
                          : "border-input",
                      )}
                    >
                      {isSelected ? <Check className="h-3 w-3" aria-hidden="true" /> : null}
                    </span>
                    <span className="line-clamp-1">{option.label}</span>
                  </button>
                );
              })
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}
