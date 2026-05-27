import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

const statusLabels: Record<string, string> = {
  active: "Активен",
  inactive: "Архив",
  needs_setup: "Требует настройки",
  running: "Считается",
  blocked: "Блокеры",
  completed: "Готов",
  failed: "Ошибка",
  finalized: "Закрыт",
};

const statusClasses: Record<string, string> = {
  active: "border-emerald-200 bg-emerald-50 text-emerald-700",
  inactive: "border-zinc-200 bg-zinc-50 text-zinc-600",
  needs_setup: "border-amber-200 bg-amber-50 text-amber-700",
  running: "border-blue-200 bg-blue-50 text-blue-700",
  blocked: "border-red-200 bg-red-50 text-red-700",
  completed: "border-emerald-200 bg-emerald-50 text-emerald-700",
  failed: "border-red-200 bg-red-50 text-red-700",
  finalized: "border-slate-200 bg-slate-50 text-slate-700",
};

export function StatusBadge({ status }: { status: string }) {
  return (
    <Badge
      className={cn(
        "rounded-md border font-medium shadow-none",
        statusClasses[status] ?? statusClasses.inactive,
      )}
    >
      {statusLabels[status] ?? status}
    </Badge>
  );
}
