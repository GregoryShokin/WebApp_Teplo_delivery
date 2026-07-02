import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  apiErrorMessage,
  deleteAdminSalaryOverride,
  getAdminSalaries,
  getDishwasherPool,
  getEmployees,
  putAdminPayoutMode,
  putAdminSalaryDefault,
  putAdminSalaryOverride,
  putDishwasherPool,
  setAdminPayrollExclusion,
  type AdminPayoutMode,
} from "@/lib/api";

const PAYOUT_MODE_OPTIONS: Array<{ value: AdminPayoutMode; label: string }> = [
  { value: "split", label: "Пополам (½ + ½)" },
  { value: "first_half", label: "Всё на 15-е" },
  { value: "second_half", label: "Всё на 1-е" },
  { value: "on_demand", label: "По требованию (долг)" },
];

function defaultEffectiveFrom() {
  // 1-е число прошлого месяца: дефолт «Действует с» так, чтобы только что заданный
  // оклад применился к текущей расчётной ведомости (самый недавний полумесячный
  // период всегда начинается не раньше 1-го числа прошлого месяца). Для повышения
  // оклада позже пользователь ставит дату вручную.
  const now = new Date();
  const isJanuary = now.getMonth() === 0;
  const year = isJanuary ? now.getFullYear() - 1 : now.getFullYear();
  const month = isJanuary ? 12 : now.getMonth();
  return `${year}-${String(month).padStart(2, "0")}-01`;
}

function formatMoney(value: number | null) {
  if (value === null) {
    return "— не задан";
  }
  return new Intl.NumberFormat("ru-RU", {
    maximumFractionDigits: 0,
    style: "currency",
    currency: "RUB",
  }).format(value);
}

export function AdminSalariesSection({ canWrite }: { canWrite: boolean }) {
  const queryClient = useQueryClient();
  const salariesQuery = useQuery({
    queryKey: ["payroll-admin-salaries"],
    queryFn: getAdminSalaries,
  });
  const employeesQuery = useQuery({
    queryKey: ["employees", "admin-oklady"],
    queryFn: () => getEmployees({ status: "active" }),
  });

  const poolQuery = useQuery({
    queryKey: ["dishwasher-pool"],
    queryFn: getDishwasherPool,
  });

  const [effectiveFrom, setEffectiveFrom] = useState(defaultEffectiveFrom());
  const [defaultAmounts, setDefaultAmounts] = useState<Record<string, string>>({});
  const [overrideAmounts, setOverrideAmounts] = useState<Record<string, string>>({});
  const [poolDraft, setPoolDraft] = useState<string>("");

  const defaults = salariesQuery.data?.defaults ?? [];
  const overrides = salariesQuery.data?.overrides ?? [];
  const defaultByPosition = new Map(defaults.map((item) => [item.position, item]));
  const overrideByEmployee = new Map(overrides.map((item) => [item.employee_id, item]));
  // Список окладных должностей ведём от сервера (реестр должностей), а НЕ хардкодом —
  // иначе новые должности (напр. «Помощник менеджера») не появятся в таблице.
  const adminPositions = defaults.map((item) => item.position);
  const adminEmployees = (employeesQuery.data ?? []).filter((employee) =>
    adminPositions.includes(employee.position ?? ""),
  );

  const onMutationError = (error: unknown) => {
    toast.error(apiErrorMessage(error));
  };
  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["payroll-admin-salaries"] });

  const saveDefault = useMutation({
    mutationFn: (vars: { position: string; amount: number }) =>
      putAdminSalaryDefault({
        position: vars.position,
        amount: vars.amount,
        effective_from: effectiveFrom,
      }),
    onSuccess: async (_data, vars) => {
      setDefaultAmounts((current) => {
        const next = { ...current };
        delete next[vars.position];
        return next;
      });
      await invalidate();
      toast.success("Оклад по должности сохранён");
    },
    onError: onMutationError,
  });

  const saveOverride = useMutation({
    mutationFn: (vars: { employeeId: string; position: string; amount: number }) =>
      putAdminSalaryOverride(vars.employeeId, {
        position: vars.position,
        amount: vars.amount,
        effective_from: effectiveFrom,
      }),
    onSuccess: async (_data, vars) => {
      setOverrideAmounts((current) => {
        const next = { ...current };
        delete next[vars.employeeId];
        return next;
      });
      await invalidate();
      toast.success("Персональный оклад сохранён");
    },
    onError: onMutationError,
  });

  const clearOverride = useMutation({
    mutationFn: (employeeId: string) => deleteAdminSalaryOverride(employeeId),
    onSuccess: async () => {
      await invalidate();
      toast.success("Переопределение снято — действует оклад должности");
    },
    onError: onMutationError,
  });

  const savePayoutMode = useMutation({
    mutationFn: (vars: { position: string; mode: AdminPayoutMode }) =>
      putAdminPayoutMode(vars.position, vars.mode),
    onSuccess: async () => {
      await invalidate();
      toast.success("Режим выплаты обновлён — пересчитайте ведомость");
    },
    onError: onMutationError,
  });

  const savePool = useMutation({
    mutationFn: (pool: number) => putDishwasherPool(pool),
    onSuccess: async () => {
      setPoolDraft("");
      await queryClient.invalidateQueries({ queryKey: ["dishwasher-pool"] });
      toast.success("Пул мойщиц сохранён — пересчитайте ведомость");
    },
    onError: onMutationError,
  });

  const toggleExclusion = useMutation({
    mutationFn: (vars: { employeeId: string; excluded: boolean }) =>
      setAdminPayrollExclusion(vars.employeeId, vars.excluded),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["employees", "admin-oklady"] });
      toast.success("Список ведомости обновлён — пересчитайте ведомость");
    },
    onError: onMutationError,
  });

  const isBusy =
    saveDefault.isPending ||
    saveOverride.isPending ||
    clearOverride.isPending ||
    savePayoutMode.isPending ||
    savePool.isPending ||
    toggleExclusion.isPending;

  const pool = poolQuery.data?.pool ?? null;
  const poolParsed = Number(poolDraft);
  const canSavePool = canWrite && poolDraft.trim() !== "" && poolParsed >= 0;

  return (
    <div className="space-y-6">
      <section className="rounded-lg border bg-card p-4">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <div className="font-semibold">Оклады администрации</div>
            <div className="mt-1 text-sm text-muted-foreground">
              Месячные оклады по должностям. Полумесячная выплата = ½ оклада. Переопределение на
              сотрудника имеет приоритет над окладом должности.
            </div>
          </div>
          <Label className="grid gap-1 text-sm">
            <span className="text-muted-foreground">Действует с</span>
            <Input
              className="w-44"
              disabled={!canWrite || isBusy}
              onChange={(event) => setEffectiveFrom(event.target.value)}
              type="date"
              value={effectiveFrom}
            />
          </Label>
        </div>
      </section>

      <section className="space-y-3">
        <div className="text-sm font-semibold">Оклад по должности</div>
        <div className="overflow-hidden rounded-lg border">
          <table className="w-full text-sm">
            <thead className="bg-muted text-xs uppercase text-muted-foreground">
              <tr>
                <th className="px-3 py-2 text-left">Должность</th>
                <th className="px-3 py-2 text-left">Текущий оклад</th>
                <th className="px-3 py-2 text-left">Новый оклад, ₽/мес</th>
                <th className="px-3 py-2 text-left">Режим выплаты</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody>
              {adminPositions.map((position) => {
                const current = defaultByPosition.get(position);
                const draftValue = defaultAmounts[position] ?? "";
                const parsed = Number(draftValue);
                const canSave = canWrite && draftValue.trim() !== "" && parsed > 0;
                const payoutMode: AdminPayoutMode = current?.payout_mode ?? "split";
                return (
                  <tr key={position} className="border-t">
                    <td className="px-3 py-2 font-medium">{position}</td>
                    <td className="px-3 py-2 tabular-nums">
                      {formatMoney(current?.amount ?? null)}
                    </td>
                    <td className="px-3 py-2">
                      <Input
                        className="w-40"
                        disabled={!canWrite || isBusy}
                        inputMode="numeric"
                        onChange={(event) =>
                          setDefaultAmounts((map) => ({ ...map, [position]: event.target.value }))
                        }
                        placeholder={current?.amount != null ? String(current.amount) : "0"}
                        value={draftValue}
                      />
                    </td>
                    <td className="px-3 py-2">
                      <Select
                        key={payoutMode}
                        disabled={!canWrite || isBusy}
                        onValueChange={(value) =>
                          savePayoutMode.mutate({
                            position,
                            mode: value as AdminPayoutMode,
                          })
                        }
                        value={payoutMode}
                      >
                        <SelectTrigger className="w-48">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {PAYOUT_MODE_OPTIONS.map((option) => (
                            <SelectItem key={option.value} value={option.value}>
                              {option.label}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </td>
                    <td className="px-3 py-2 text-right">
                      <Button
                        disabled={!canSave || isBusy}
                        onClick={() => saveDefault.mutate({ position, amount: parsed })}
                        size="sm"
                        variant="outline"
                      >
                        Сохранить
                      </Button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      <section className="space-y-3">
        <div className="text-sm font-semibold">Пул мойщиц</div>
        <div className="rounded-lg border bg-card p-4">
          <div className="flex flex-wrap items-end gap-3">
            <Label className="grid gap-1 text-sm">
              <span className="text-muted-foreground">Пул мойщиц, ₽/мес</span>
              <Input
                className="w-44"
                disabled={!canWrite || isBusy}
                inputMode="numeric"
                onChange={(event) => setPoolDraft(event.target.value)}
                placeholder={pool != null ? String(pool) : "0"}
                value={poolDraft}
              />
            </Label>
            <Button
              disabled={!canSavePool || isBusy}
              onClick={() => savePool.mutate(poolParsed)}
              size="sm"
              variant="outline"
            >
              Сохранить
            </Button>
            <div className="text-sm tabular-nums text-muted-foreground">
              Текущий пул: {formatMoney(pool)}
            </div>
          </div>
          <div className="mt-2 text-xs text-muted-foreground">
            Делится между мойщицами: ставка за смену = пул ÷ дней месяца. Каждая получает (её смены
            в периоде) × ставку. Смены отмечает управляющий в «График сотрудников → График мойщиц».
          </div>
        </div>
      </section>

      <section className="space-y-3">
        <div className="text-sm font-semibold">Персональные оклады</div>
        <div className="text-xs text-muted-foreground">
          Пусто = действует оклад должности. Заполните, чтобы переопределить для конкретного
          сотрудника. «Не платить» — исключить из ведомости (собственники, системные/AI-аккаунты),
          даже если оклад по должности задан.
        </div>
        {employeesQuery.isLoading ? (
          <div className="flex items-center gap-2 px-1 py-4 text-sm text-muted-foreground">
            <LoaderCircle className="animate-spin" size={16} aria-hidden="true" /> Загрузка
            сотрудников…
          </div>
        ) : adminEmployees.length === 0 ? (
          <div className="rounded-md border bg-card px-3 py-4 text-sm text-muted-foreground">
            Нет активных сотрудников на админ-должностях.
          </div>
        ) : (
          <div className="overflow-hidden rounded-lg border">
            <table className="w-full text-sm">
              <thead className="bg-muted text-xs uppercase text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 text-left">Сотрудник</th>
                  <th className="px-3 py-2 text-left">Должность</th>
                  <th className="px-3 py-2 text-left">Текущий оклад</th>
                  <th className="px-3 py-2 text-left">Персональный, ₽/мес</th>
                  <th className="px-3 py-2 text-center">Не платить</th>
                  <th className="px-3 py-2" />
                </tr>
              </thead>
              <tbody>
                {adminEmployees.map((employee) => {
                  const position = employee.position ?? "";
                  const override = overrideByEmployee.get(employee.id);
                  const fallback = defaultByPosition.get(position)?.amount ?? null;
                  const effective = override?.amount ?? fallback;
                  const draftValue = overrideAmounts[employee.id] ?? "";
                  const parsed = Number(draftValue);
                  const excluded = Boolean(employee.admin_payroll_excluded);
                  const canSave = canWrite && !excluded && draftValue.trim() !== "" && parsed > 0;
                  return (
                    <tr key={employee.id} className={excluded ? "border-t opacity-50" : "border-t"}>
                      <td className="px-3 py-2 font-medium">{employee.full_name}</td>
                      <td className="px-3 py-2 text-muted-foreground">{position}</td>
                      <td className="px-3 py-2 tabular-nums">
                        {excluded ? "—" : formatMoney(effective)}
                        {override && !excluded ? (
                          <span className="ml-1 text-xs text-primary">(персональный)</span>
                        ) : null}
                      </td>
                      <td className="px-3 py-2">
                        <Input
                          className="w-40"
                          disabled={!canWrite || isBusy || excluded}
                          inputMode="numeric"
                          onChange={(event) =>
                            setOverrideAmounts((map) => ({
                              ...map,
                              [employee.id]: event.target.value,
                            }))
                          }
                          placeholder={override?.amount != null ? String(override.amount) : "по должности"}
                          value={draftValue}
                        />
                      </td>
                      <td className="px-3 py-2 text-center">
                        <Checkbox
                          aria-label="Исключить из ведомости"
                          checked={excluded}
                          disabled={!canWrite || isBusy}
                          onChange={(event) =>
                            toggleExclusion.mutate({
                              employeeId: employee.id,
                              excluded: event.target.checked,
                            })
                          }
                        />
                      </td>
                      <td className="px-3 py-2 text-right">
                        <div className="flex justify-end gap-2">
                          <Button
                            disabled={!canSave || isBusy}
                            onClick={() =>
                              saveOverride.mutate({
                                employeeId: employee.id,
                                position,
                                amount: parsed,
                              })
                            }
                            size="sm"
                            variant="outline"
                          >
                            Сохранить
                          </Button>
                          {override ? (
                            <Button
                              disabled={!canWrite || isBusy}
                              onClick={() => clearOverride.mutate(employee.id)}
                              size="sm"
                              variant="ghost"
                            >
                              Сбросить
                            </Button>
                          ) : null}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
