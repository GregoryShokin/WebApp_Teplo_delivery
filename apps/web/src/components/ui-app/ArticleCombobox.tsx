import { Check, ChevronDown } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

/**
 * Выпадающий список статей ДДС с поиском по мере ввода (фильтр по названию).
 * Свой combobox, а не Radix Select — Radix Select перехватывает ввод, поэтому
 * собираем лёгкий вариант по образцу EmployeeCombobox. Пустое value → плейсхолдер.
 */
export function ArticleCombobox({
  articles,
  value,
  onChange,
  placeholder = "Статья ДДС",
  disabled,
  className,
}: {
  articles: ReadonlyArray<{ id: string; name: string }>;
  value: string;
  onChange: (id: string) => void;
  placeholder?: string;
  disabled?: boolean;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const containerRef = useRef<HTMLDivElement>(null);

  const selected = articles.find((article) => article.id === value) ?? null;
  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) {
      return articles;
    }
    return articles.filter((article) => article.name.toLowerCase().includes(query));
  }, [articles, search]);

  useEffect(() => {
    if (!open) {
      setSearch("");
      return;
    }
    const handlePointer = (event: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handlePointer);
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("mousedown", handlePointer);
      document.removeEventListener("keydown", handleKey);
    };
  }, [open]);

  const select = (id: string) => {
    onChange(id);
    setOpen(false);
  };

  return (
    <div className={cn("relative", className)} ref={containerRef}>
      <button
        aria-expanded={open}
        className="flex h-10 w-full items-center justify-between rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
        disabled={disabled}
        onClick={() => setOpen((current) => !current)}
        type="button"
      >
        <span className={cn("line-clamp-1", !selected && "text-muted-foreground")}>
          {selected ? selected.name : placeholder}
        </span>
        <ChevronDown className="h-4 w-4 shrink-0 opacity-50" aria-hidden="true" />
      </button>

      {open ? (
        <div className="absolute z-50 mt-1 w-full rounded-md border bg-popover text-popover-foreground shadow-md">
          <div className="border-b p-1">
            <Input
              autoFocus
              className="h-9"
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Поиск статьи"
              value={search}
            />
          </div>
          <div className="max-h-80 overflow-y-auto p-1">
            {filtered.length === 0 ? (
              <div className="px-2 py-2 text-sm text-muted-foreground">Статьи не найдены</div>
            ) : (
              filtered.map((article) => (
                <button
                  className={cn(
                    "flex w-full items-center justify-between gap-2 rounded-sm px-2 py-1.5 text-left text-sm outline-none hover:bg-accent hover:text-accent-foreground focus:bg-accent focus:text-accent-foreground",
                    article.id === value && "bg-accent/60",
                  )}
                  key={article.id}
                  onClick={() => select(article.id)}
                  type="button"
                >
                  <span className="line-clamp-1">{article.name}</span>
                  {article.id === value ? (
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
