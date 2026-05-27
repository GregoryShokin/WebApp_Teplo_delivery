import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

const roleLabels: Record<string, string> = {
  admin: "Администратор",
  owner: "Владелец",
  accountant: "Финансы",
  payroll: "Зарплата",
  manager: "Менеджер",
  guest: "Гость",
};

const roleClasses: Record<string, string> = {
  admin: "border-emerald-200 bg-emerald-50 text-emerald-700",
  owner: "border-blue-200 bg-blue-50 text-blue-700",
  accountant: "border-slate-200 bg-slate-50 text-slate-700",
  payroll: "border-teal-200 bg-teal-50 text-teal-700",
  manager: "border-zinc-200 bg-zinc-50 text-zinc-700",
  guest: "border-zinc-200 bg-zinc-50 text-zinc-600",
};

export function RoleBadge({ role }: { role?: string | null }) {
  const value = role || "guest";

  return (
    <Badge
      className={cn(
        "rounded-md border font-medium shadow-none",
        roleClasses[value] ?? roleClasses.guest,
      )}
    >
      {roleLabels[value] ?? value}
    </Badge>
  );
}
