import { Info } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { cn } from "@/lib/utils";

type InfoHintProps = {
  children: ReactNode;
  /** 'alert' — красная иконка: пояснение к проблеме, а не к методике. */
  tone?: "muted" | "alert";
  /** Что поясняем — попадает в aria-label («Пояснение к строке …»). */
  label?: string;
  className?: string;
};

/** Иконка «i» с пояснением: методика, нормы, происхождение цифры.
 *
 * Канал для текста, которого не должно быть на экране (решение владельца 27.07.2026:
 * «деньги и действие видны, методология — под иконкой»). Поэтому подсказка обязана
 * ВЫДЕРЖИВАТЬ длинный текст, а не обрезать его:
 *
 * * открывается по наведению И фиксируется кликом (до повторного клика / Esc);
 * * зафиксированная кликабельна и выделяема — текст можно скопировать бухгалтеру;
 * * длинное пояснение прокручивается внутри, а не уезжает за край экрана.
 */
export function InfoHint({ children, tone = "muted", label, className }: InfoHintProps) {
  const [pinned, setPinned] = useState(false);
  const wrapRef = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!pinned) {
      return;
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setPinned(false);
      }
    };
    const onClickOutside = (event: MouseEvent) => {
      if (!wrapRef.current?.contains(event.target as Node)) {
        setPinned(false);
      }
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onClickOutside);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onClickOutside);
    };
  }, [pinned]);

  return (
    <span className={cn("group relative inline-flex align-middle", className)} ref={wrapRef}>
      <button
        aria-expanded={pinned}
        aria-label={label ? `Пояснение: ${label}` : "Пояснение"}
        className={cn(
          "inline-flex size-4 items-center justify-center rounded-full transition-colors",
          tone === "alert"
            ? "text-rose-500 hover:text-rose-700"
            : "text-muted-foreground hover:text-foreground",
          pinned && "text-foreground",
        )}
        onClick={() => setPinned((value) => !value)}
        type="button"
      >
        <Info aria-hidden="true" className="size-3.5" />
      </button>
      <span
        className={cn(
          "absolute right-0 top-5 z-30 w-80 max-w-[min(20rem,calc(100vw-2rem))] rounded-md border bg-card p-2.5 text-left text-xs font-normal normal-case leading-5 text-card-foreground shadow-md",
          "max-h-64 overflow-y-auto",
          pinned
            ? "block"
            : "pointer-events-none hidden group-focus-within:block group-hover:block",
        )}
        role="tooltip"
      >
        {children}
      </span>
    </span>
  );
}
