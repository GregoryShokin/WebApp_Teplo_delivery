import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { DatabaseZap, RefreshCw, Save, Search, ShieldAlert } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { Button } from "../components/ui/button";
import { PageHeader } from "../components/ui-app/PageHeader";
import { StatusBadge } from "../components/ui-app/StatusBadge";
import {
  type Employee,
  type EmployeePatch,
  type EmployeeStatus,
  getEmployees,
  patchEmployee,
  syncEmployees,
} from "../lib/api";

const statusLabels: Record<EmployeeStatus, string> = {
  active: "Активен",
  inactive: "Архив",
  needs_setup: "Настроить",
};

const statusOptions: Array<EmployeeStatus | "all"> = ["all", "needs_setup", "active", "inactive"];

type Draft = Pick<Employee, "position" | "category" | "is_senior" | "is_deputy_senior">;

export function StaffRoute() {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<EmployeeStatus | "all">("all");
  const [category, setCategory] = useState("");
  const [search, setSearch] = useState("");
  const [syncMessage, setSyncMessage] = useState<string | null>(null);

  const employeesQuery = useQuery({
    queryKey: ["employees", status, category],
    queryFn: () => getEmployees({ status, category: category || undefined }),
  });

  const syncMutation = useMutation({
    mutationFn: syncEmployees,
    onSuccess: (result) => {
      setSyncMessage(
        `Создано ${result.created}, обновлено ${result.updated}, деактивировано ${result.deactivated}`,
      );
      void queryClient.invalidateQueries({ queryKey: ["employees"] });
    },
  });

  const employees = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return (employeesQuery.data ?? []).filter((employee) =>
      needle ? employee.full_name.toLowerCase().includes(needle) : true,
    );
  }, [employeesQuery.data, search]);

  const categories = useMemo(() => {
    const values = new Set<string>();
    for (const employee of employeesQuery.data ?? []) {
      if (employee.category) {
        values.add(employee.category);
      }
    }
    return [...values].sort((a, b) => a.localeCompare(b, "ru"));
  }, [employeesQuery.data]);

  return (
    <div className="space-y-5">
      <PageHeader
        title="Штат"
        description="Единый справочник сотрудников для графиков и зарплаты."
        action={
          <>
            {syncMessage ? (
              <div className="rounded-md border border-border bg-white px-3 py-2 text-sm text-muted-foreground">
                {syncMessage}
              </div>
            ) : null}
            {syncMutation.isError ? (
              <div className="flex items-center gap-2 rounded-md border border-destructive/30 bg-white px-3 py-2 text-sm text-destructive">
                <ShieldAlert size={16} aria-hidden="true" />
                {(syncMutation.error as Error).message}
              </div>
            ) : null}
            <Button onClick={() => syncMutation.mutate()} disabled={syncMutation.isPending}>
              <RefreshCw
                className={syncMutation.isPending ? "animate-spin" : ""}
                size={16}
                aria-hidden="true"
              />
              Запустить sync
            </Button>
          </>
        }
      />

      <div className="grid gap-3 lg:grid-cols-[220px_220px_1fr]">
        <label className="grid gap-1 text-sm">
          <span className="text-xs font-medium uppercase text-muted-foreground">Статус</span>
          <select
            className="h-9 rounded-md border border-border bg-white px-3"
            value={status}
            onChange={(event) => setStatus(event.target.value as EmployeeStatus | "all")}
          >
            {statusOptions.map((option) => (
              <option value={option} key={option}>
                {option === "all" ? "Все" : statusLabels[option]}
              </option>
            ))}
          </select>
        </label>
        <label className="grid gap-1 text-sm">
          <span className="text-xs font-medium uppercase text-muted-foreground">Категория</span>
          <select
            className="h-9 rounded-md border border-border bg-white px-3"
            value={category}
            onChange={(event) => setCategory(event.target.value)}
          >
            <option value="">Все</option>
            {categories.map((value) => (
              <option value={value} key={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
        <label className="grid gap-1 text-sm">
          <span className="text-xs font-medium uppercase text-muted-foreground">Поиск</span>
          <div className="flex h-9 items-center gap-2 rounded-md border border-border bg-white px-3">
            <Search size={16} className="text-muted-foreground" aria-hidden="true" />
            <input
              className="min-w-0 flex-1 bg-transparent outline-none"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </div>
        </label>
      </div>

      <section className="mt-5 overflow-hidden rounded-lg border border-border bg-white">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[980px] border-collapse text-sm">
            <thead className="bg-muted/70 text-left text-xs uppercase text-muted-foreground">
              <tr>
                <th className="w-[290px] px-4 py-3 font-semibold">Имя</th>
                <th className="w-[180px] px-3 py-3 font-semibold">Должность</th>
                <th className="w-[150px] px-3 py-3 font-semibold">Категория</th>
                <th className="w-[170px] px-3 py-3 font-semibold">Надбавки</th>
                <th className="w-[130px] px-3 py-3 font-semibold">Статус</th>
                <th className="w-[150px] px-3 py-3 font-semibold">iiko sync</th>
                <th className="w-[76px] px-3 py-3 text-right font-semibold"> </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {employees.map((employee) => (
                <StaffRow employee={employee} key={employee.id} />
              ))}
            </tbody>
          </table>
        </div>
        {employeesQuery.isLoading ? (
          <div className="px-4 py-10 text-center text-sm text-muted-foreground">Загрузка</div>
        ) : null}
        {!employeesQuery.isLoading && employees.length === 0 ? (
          <div className="px-4 py-10 text-center text-sm text-muted-foreground">
            Нет сотрудников по выбранным фильтрам
          </div>
        ) : null}
      </section>
    </div>
  );
}

function StaffRow({ employee }: { employee: Employee }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<Draft>(() => toDraft(employee));

  useEffect(() => {
    setDraft(toDraft(employee));
  }, [employee]);

  const mutation = useMutation({
    mutationFn: (patch: EmployeePatch) => patchEmployee(employee.id, patch),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["employees"] });
    },
  });

  const dirty =
    draft.position !== employee.position ||
    draft.category !== employee.category ||
    draft.is_senior !== employee.is_senior ||
    draft.is_deputy_senior !== employee.is_deputy_senior;

  return (
    <tr className="align-middle hover:bg-muted/40">
      <td className="px-4 py-3">
        <div className="flex items-center gap-2">
          <input
            className="h-9 min-w-0 flex-1 rounded-md border border-border bg-muted px-3 text-foreground"
            value={employee.full_name}
            disabled
            title="синхронизируется из iiko"
          />
          <span
            className="inline-flex h-9 w-9 items-center justify-center rounded-md border border-border text-primary"
            title="синхронизируется из iiko"
          >
            <DatabaseZap size={16} aria-hidden="true" />
          </span>
        </div>
      </td>
      <td className="px-3 py-3">
        <input
          className="h-9 w-full rounded-md border border-border bg-white px-3"
          value={draft.position ?? ""}
          onChange={(event) => setDraft({ ...draft, position: event.target.value || null })}
        />
      </td>
      <td className="px-3 py-3">
        <input
          className="h-9 w-full rounded-md border border-border bg-white px-3"
          value={draft.category ?? ""}
          onChange={(event) => setDraft({ ...draft, category: event.target.value || null })}
        />
      </td>
      <td className="px-3 py-3">
        <div className="flex flex-wrap gap-3">
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={draft.is_senior}
              onChange={(event) => setDraft({ ...draft, is_senior: event.target.checked })}
            />
            Старший
          </label>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={draft.is_deputy_senior}
              onChange={(event) => setDraft({ ...draft, is_deputy_senior: event.target.checked })}
            />
            Зам
          </label>
        </div>
      </td>
      <td className="px-3 py-3">
        <StatusBadge status={employee.status} />
      </td>
      <td className="px-3 py-3 text-xs text-muted-foreground">
        {employee.iiko_sync_at
          ? new Date(employee.iiko_sync_at).toLocaleString("ru-RU")
          : "не было"}
      </td>
      <td className="px-3 py-3 text-right">
        <Button
          size="icon"
          variant={dirty ? "default" : "outline"}
          disabled={!dirty || mutation.isPending}
          onClick={() => mutation.mutate(draft)}
          title="Сохранить"
        >
          <Save size={16} aria-hidden="true" />
        </Button>
      </td>
    </tr>
  );
}

function toDraft(employee: Employee): Draft {
  return {
    position: employee.position,
    category: employee.category,
    is_senior: employee.is_senior,
    is_deputy_senior: employee.is_deputy_senior,
  };
}
