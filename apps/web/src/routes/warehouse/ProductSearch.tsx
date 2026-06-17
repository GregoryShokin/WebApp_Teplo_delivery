import { useMemo, useState } from "react";

import { Input } from "@/components/ui/input";

import type { WarehouseProduct } from "./api";

// Переиспользуемый пикер номенклатуры (GOODS): один список iiko, один синк. Используется
// в накладных и в чеках кассы. При выборе товара отдаёт name + unit + id.
export function ProductSearch({
  value,
  products,
  onPick,
  onTextChange,
  placeholder = "Товар (GOODS)",
}: {
  value: string;
  products: WarehouseProduct[];
  onPick: (product: WarehouseProduct) => void;
  onTextChange: (text: string) => void;
  placeholder?: string;
}) {
  const [focused, setFocused] = useState(false);

  const matches = useMemo(() => {
    const q = value.trim().toLowerCase();
    if (!q) return products.slice(0, 12);
    return products
      .filter(
        (p) => p.name.toLowerCase().includes(q) || (p.code ?? "").toLowerCase().includes(q),
      )
      .slice(0, 12);
  }, [products, value]);

  return (
    <div className="relative">
      <Input
        value={value}
        placeholder={placeholder}
        onChange={(e) => onTextChange(e.target.value)}
        onFocus={() => setFocused(true)}
        onBlur={() => setTimeout(() => setFocused(false), 150)}
      />
      {focused && matches.length > 0 ? (
        <div className="absolute z-50 mt-1 max-h-56 w-full overflow-auto rounded-md border bg-popover p-1 shadow-md">
          {matches.map((p) => (
            <button
              key={p.id}
              type="button"
              className="flex w-full items-center justify-between gap-2 rounded px-2 py-1.5 text-left text-sm hover:bg-muted"
              onMouseDown={(e) => {
                e.preventDefault();
                onPick(p);
                setFocused(false);
              }}
            >
              <span className="truncate">{p.name}</span>
              <span className="shrink-0 text-xs text-muted-foreground">{p.unit ?? ""}</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
