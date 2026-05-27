import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BadgeCheck,
  DatabaseZap,
  RefreshCw,
  Save,
  Search,
  ShieldAlert,
  UserRoundCheck,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { Button } from "../components/ui/button";
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

const statusOptions: Array<EmployeeStatus | "all"> = [
  "all",
  "needs_setup",
  "active",
  "inactive",
];

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
    <main className="min-h-screen bg-background">
      <div className="mx-auto grid min-h-screen max-w-7xl grid-cols-1 lg:grid-cols-[240px_1fr]">
        <aside className="border-b border-border bg-white px-5 py-5 lg:border-b-0 lg:border-r">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-md bg-primary text-primary-foreground">
              <UserRoundCheck size={18} aria-hidden="true" />
            </div>
            <div>
              <div className="text-base font-semibold">Тепло</div>
              <div className="text-xs text-muted-foreground">staff master</div>
            </div>
          </div>
          <nav className="mt-8 grid gap-1 text-sm">
            {[
              ["Обзор", "/"],
              ["Штат", "/staff"],
              ["График", "/"],
              ["Зарплата", "/payroll/runs"],
              ["ДДС", "/"],
              ["ОПиУ", "/"],
              ["Интеграции", "/"],
            ].map(([item, href]) => (
              <a
                className={`rounded-md px-3 py-2 text-left ${
                  item === "Штат"
                    ? "bg-muted font-medium text-foreground"
                    : "text-muted-foreground hover:bg-muted hover:text-foreground"
                }`}
                href={href}
                key={item}
              >
                {item}
              </a>
            ))}
          </nav>
        </aside>

        <section className="px-5 py-5 sm:px-8">
          <header className="flex flex-col gap-4 border-b border-border pb-5 xl:flex-row xl:items-center xl:justify-between">
            <div>
              <h1 className="text-2xl font-semibold tracking-normal">Штат</h1>
              <p className="mt-1 text-sm text-muted-foreground">
                Единый справочник сотрудников для графиков и зарплаты.
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
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
            </div>
          </header>

          <div className="mt-5 grid gap-3 lg:grid-cols-[220px_220px_1fr]">
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
        </section>
      </div>
    </main>
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
              onChange={(event) =>
                setDraft({ ...draft, is_deputy_senior: event.target.checked })
              }
            />
            Зам
          </label>
        </div>
      </td>
      <td className="px-3 py-3">
        <span
          className={`inline-flex h-7 items-center gap-1 rounded-md px-2 text-xs font-medium ${
            employee.status === "needs_setup"
              ? "bg-accent/20 text-accent-foreground"
              : employee.status === "inactive"
                ? "bg-muted text-muted-foreground"
                : "bg-primary/10 text-primary"
          }`}
        >
          {employee.status === "active" ? <BadgeCheck size={14} aria-hidden="true" /> : null}
          {statusLabels[employee.status]}
        </span>
      </td>
      <td className="px-3 py-3 text-xs text-muted-foreground">
        {employee.iiko_sync_at ? new Date(employee.iiko_sync_at).toLocaleString("ru-RU") : "не было"}
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
