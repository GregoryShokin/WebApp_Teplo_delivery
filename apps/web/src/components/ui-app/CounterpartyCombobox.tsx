import { Check, ChevronDown } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

export type CounterpartyOption = {
  counterparty_id: string;
  name: string;
  inn: string | null;
};

/**
 * Выпадающий список контрагентов с поиском по названию и ИНН (Radix Select перехватывает
 * ввод — собираем лёгкий вариант по образцу EmployeeCombobox и ArticleCombobox).
 *
 * `pinnedIds` — контрагенты, закреплённые за выбранной статьёй: они идут первыми под
 * заголовком. Порядок здесь не украшение: у статьи «Оплата поставщикам» закреплённых может
 * быть три, а всего контрагентов сотни, и без разделителя нужный тонет в общем списке.
 *
 * ИНН показываем второй строкой — у контрагентов бывают тёзки («ИП Иванов»), и по имени
 * они неразличимы.
 */
export function CounterpartyCombobox({
  className,
  clearLabel = "Кому платим: не указан",
  counterparties,
  disabled,
  onChange,
  pinnedIds,
  placeholder = "Кому платим",
  value,
}: {
  className?: string;
  clearLabel?: string;
  counterparties: ReadonlyArray<CounterpartyOption>;
  disabled?: boolean;
  onChange: (counterpartyId: string) => void;
  pinnedIds?: ReadonlySet<string>;
  placeholder?: string;
  value: string;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const containerRef = useRef<HTMLDivElement>(null);

  const selected = counterparties.find((item) => item.counterparty_id === value) ?? null;

  const { pinned, rest } = useMemo(() => {
    const query = search.trim().toLowerCase();
    const matched = query
      ? counterparties.filter(
          (item) => item.name.toLowerCase().includes(query) || (item.inn ?? "").includes(query),
        )
      : counterparties;
    return {
      pinned: matched.filter((item) => pinnedIds?.has(item.counterparty_id)),
      rest: matched.filter((item) => !pinnedIds?.has(item.counterparty_id)),
    };
  }, [counterparties, pinnedIds, search]);

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

  const select = (counterpartyId: string) => {
    onChange(counterpartyId);
    setOpen(false);
  };

  const renderOption = (item: CounterpartyOption) => (
    <button
      className={cn(
        "flex w-full items-center justify-between gap-2 rounded-sm px-2 py-1.5 text-left text-sm outline-none hover:bg-accent hover:text-accent-foreground focus:bg-accent focus:text-accent-foreground",
        item.counterparty_id === value && "bg-accent/60",
      )}
      key={item.counterparty_id}
      onClick={() => select(item.counterparty_id)}
      type="button"
    >
      <span className="min-w-0">
        <span className="line-clamp-1">{item.name}</span>
        {item.inn ? (
          <span className="block text-[11px] text-muted-foreground">ИНН {item.inn}</span>
        ) : null}
      </span>
      {item.counterparty_id === value ? (
        <Check className="h-4 w-4 shrink-0" aria-hidden="true" />
      ) : null}
    </button>
  );

  return (
    <div className={cn("relative", className)} ref={containerRef}>
      <button
        aria-expanded={open}
        aria-label={placeholder}
        className="flex h-8 w-full items-center justify-between rounded-md border border-input bg-background px-3 py-1 text-sm ring-offset-background focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
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
              placeholder="Название или ИНН"
              value={search}
            />
          </div>
          <div className="max-h-72 overflow-y-auto p-1">
            <button
              className={cn(
                "flex w-full items-center justify-between gap-2 rounded-sm px-2 py-1.5 text-left text-sm text-muted-foreground outline-none hover:bg-accent hover:text-accent-foreground",
                !value && "bg-accent/60",
              )}
              onClick={() => select("")}
              type="button"
            >
              {clearLabel}
            </button>
            {pinned.length > 0 ? (
              <>
                <div className="px-2 pb-0.5 pt-2 text-[11px] uppercase text-muted-foreground">
                  Закреплены за статьёй
                </div>
                {pinned.map(renderOption)}
                {rest.length > 0 ? (
                  <div className="px-2 pb-0.5 pt-2 text-[11px] uppercase text-muted-foreground">
                    Остальные
                  </div>
                ) : null}
              </>
            ) : null}
            {rest.map(renderOption)}
            {pinned.length === 0 && rest.length === 0 ? (
              <div className="px-2 py-2 text-sm text-muted-foreground">Контрагенты не найдены</div>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
