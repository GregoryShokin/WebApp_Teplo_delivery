import { Badge } from "@/components/ui/badge";
import { EMPLOYEE_STATUS_COLORS, EMPLOYEE_STATUS_LABELS } from "@/lib/i18n/employee";
import { cn } from "@/lib/utils";

const statusLabels: Record<string, string> = {
  ...EMPLOYEE_STATUS_LABELS,
  needs_setup: EMPLOYEE_STATUS_LABELS.requires_setup,
  open: "Открыт",
  in_progress: "В работе",
  running: "Считается",
  blocked: "Блокеры",
  completed: "Готов",
  failed: "Ошибка",
  finalized: "Финализирован",
  final: "Финализирован",
};

const colorClasses: Record<string, string> = {
  green: "border-emerald-200 bg-emerald-50 text-emerald-700",
  yellow: "border-amber-200 bg-amber-50 text-amber-700",
  zinc: "border-zinc-200 bg-zinc-50 text-zinc-600",
};

const statusClasses: Record<string, string> = {
  active: colorClasses[EMPLOYEE_STATUS_COLORS.active],
  inactive: colorClasses[EMPLOYEE_STATUS_COLORS.inactive],
  requires_setup: colorClasses[EMPLOYEE_STATUS_COLORS.requires_setup],
  needs_setup: colorClasses[EMPLOYEE_STATUS_COLORS.requires_setup],
  open: "border-sky-200 bg-sky-50 text-sky-700",
  in_progress: "border-blue-200 bg-blue-50 text-blue-700",
  running: "border-blue-200 bg-blue-50 text-blue-700",
  blocked: "border-red-200 bg-red-50 text-red-700",
  completed: "border-emerald-200 bg-emerald-50 text-emerald-700",
  failed: "border-red-200 bg-red-50 text-red-700",
  finalized: "border-slate-200 bg-slate-50 text-slate-700",
  final: "border-slate-200 bg-slate-50 text-slate-700",
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
