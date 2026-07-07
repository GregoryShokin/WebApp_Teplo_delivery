import { Check, ChevronDown } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { Input } from "@/components/ui/input";
import type { Employee } from "@/lib/api";
import { cn } from "@/lib/utils";

const ALL_OPTION_VALUE = "all";

function employeeOptionLabel(employee: Employee) {
  const suffix =
    employee.status === "inactive"
      ? " · уволен"
      : employee.status === "dismissing"
        ? " · в увольнении"
        : "";
  return `${employee.full_name}${suffix}`;
}

/**
 * Выпадающий список сотрудников с поиском прямо внутри списка (без cmdk/Popover —
 * Radix Select перехватывает ввод, поэтому собираем лёгкий собственный вариант).
 * Если задан `allOptionLabel`, в начало добавляется опция со значением "all".
 */
export function EmployeeCombobox({
  allOptionLabel,
  className,
  disabled,
  employees,
  onChange,
  placeholder = "Выберите сотрудника",
  value,
}: {
  allOptionLabel?: string;
  className?: string;
  disabled?: boolean;
  employees: Employee[];
  onChange: (id: string) => void;
  placeholder?: string;
  value: string;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const containerRef = useRef<HTMLDivElement>(null);

  const selected = employees.find((employee) => employee.id === value) ?? null;
  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) {
      return employees;
    }
    return employees.filter((employee) =>
      employee.full_name.toLowerCase().includes(query),
    );
  }, [employees, search]);

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

  const triggerLabel =
    allOptionLabel && value === ALL_OPTION_VALUE
      ? allOptionLabel
      : selected
        ? employeeOptionLabel(selected)
        : placeholder;
  const isPlaceholder = !selected && !(allOptionLabel && value === ALL_OPTION_VALUE);

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
        <span className={cn("line-clamp-1", isPlaceholder && "text-muted-foreground")}>
          {triggerLabel}
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
              placeholder="Поиск сотрудника"
              value={search}
            />
          </div>
          <div className="max-h-64 overflow-y-auto p-1">
            {allOptionLabel ? (
              <button
                className={cn(
                  "flex w-full items-center justify-between gap-2 rounded-sm px-2 py-1.5 text-left text-sm outline-none hover:bg-accent hover:text-accent-foreground focus:bg-accent focus:text-accent-foreground",
                  value === ALL_OPTION_VALUE && "bg-accent/60",
                )}
                onClick={() => select(ALL_OPTION_VALUE)}
                type="button"
              >
                <span className="line-clamp-1">{allOptionLabel}</span>
                {value === ALL_OPTION_VALUE ? (
                  <Check className="h-4 w-4 shrink-0" aria-hidden="true" />
                ) : null}
              </button>
            ) : null}
            {filtered.length === 0 ? (
              <div className="px-2 py-2 text-sm text-muted-foreground">
                Сотрудники не найдены
              </div>
            ) : (
              filtered.map((employee) => (
                <button
                  className={cn(
                    "flex w-full items-center justify-between gap-2 rounded-sm px-2 py-1.5 text-left text-sm outline-none hover:bg-accent hover:text-accent-foreground focus:bg-accent focus:text-accent-foreground",
                    employee.id === value && "bg-accent/60",
                  )}
                  key={employee.id}
                  onClick={() => select(employee.id)}
                  type="button"
                >
                  <span className="line-clamp-1">{employeeOptionLabel(employee)}</span>
                  {employee.id === value ? (
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
