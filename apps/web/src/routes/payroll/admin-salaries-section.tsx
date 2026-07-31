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
  getDishwasherShiftRate,
  getEmployees,
  putAdminPayoutMode,
  putAdminSalaryDefault,
  putAdminSalaryOverride,
  putDishwasherShiftRate,
  setAdminPayrollExclusion,
  type AdminPayoutMode,
} from "@/lib/api";

const PAYOUT_MODE_OPTIONS: Array<{ value: AdminPayoutMode; label: string }> = [
  { value: "split", label: "Пополам (½ + ½)" },
  { value: "first_half", label: "Всё на 15-е" },
  { value: "second_half", label: "Всё на 1-е" },
  { value: "on_demand", label: "По требованию (долг)" },
];

// Помощника менеджера сейчас исполняет кассир: назначается точечно через персональный оклад
// (override с этой должностью на конкретного кассира) — он идёт в админ-ведомость СВЕРХ своей
// производственной ЗП. Выбор — только из кассиров.
const ASSISTANT_POSITION = "Помощник менеджера";
const CASHIER_POSITION = "Кассир";

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

  const shiftRateQuery = useQuery({
    queryKey: ["dishwasher-shift-rate"],
    queryFn: getDishwasherShiftRate,
  });

  const [effectiveFrom, setEffectiveFrom] = useState(defaultEffectiveFrom());
  const [defaultAmounts, setDefaultAmounts] = useState<Record<string, string>>({});
  const [overrideAmounts, setOverrideAmounts] = useState<Record<string, string>>({});
  const [shiftRateDraft, setShiftRateDraft] = useState<string>("");
  const [assistantCashierId, setAssistantCashierId] = useState("");
  const [assistantAmount, setAssistantAmount] = useState("");

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

  // «Помощник менеджера» — исполняет кассир. Назначение = персональный оклад с этой должностью.
  const cashiers = (employeesQuery.data ?? []).filter(
    (employee) => employee.position === CASHIER_POSITION,
  );
  const assistantOverride = overrides.find((item) => item.position === ASSISTANT_POSITION) ?? null;
  const assignedCashierId = assistantOverride?.employee_id ?? "";
  const selectedCashierId = assistantCashierId || assignedCashierId;
  const assistantDefaultAmount = defaultByPosition.get(ASSISTANT_POSITION)?.amount ?? null;
  const selectedCashier = (employeesQuery.data ?? []).find((e) => e.id === selectedCashierId);
  const assistantExcluded = Boolean(selectedCashier?.admin_payroll_excluded);
  const assistantCurrentAmount =
    assistantOverride && assistantOverride.employee_id === selectedCashierId
      ? assistantOverride.amount
      : assistantDefaultAmount;
  // Пусто в «Персональный» = взять оклад по должности (6000), не заставляем перепечатывать.
  const assistantEffectiveAmount =
    assistantAmount.trim() !== "" ? Number(assistantAmount) : (assistantDefaultAmount ?? 0);
  const canSaveAssistant =
    canWrite && Boolean(selectedCashierId) && Number.isFinite(assistantEffectiveAmount) && assistantEffectiveAmount > 0;
  const showAssistantRow = defaultByPosition.has(ASSISTANT_POSITION);

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

  const saveAssistant = useMutation({
    mutationFn: async (vars: { employeeId: string; amount: number }) => {
      // Помощник менеджера — один: при смене кассира снимаем прежнее назначение.
      if (assistantOverride && assistantOverride.employee_id !== vars.employeeId) {
        await deleteAdminSalaryOverride(assistantOverride.employee_id);
      }
      return putAdminSalaryOverride(vars.employeeId, {
        position: ASSISTANT_POSITION,
        amount: vars.amount,
        effective_from: effectiveFrom,
      });
    },
    onSuccess: async () => {
      setAssistantAmount("");
      setAssistantCashierId("");
      await invalidate();
      toast.success("Помощник менеджера назначен — пересчитайте ведомость");
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

  const saveShiftRate = useMutation({
    mutationFn: (rate: number) => putDishwasherShiftRate(rate),
    onSuccess: async () => {
      setShiftRateDraft("");
      await queryClient.invalidateQueries({ queryKey: ["dishwasher-shift-rate"] });
      toast.success("Ставка мойщиц сохранена — пересчитайте ведомость");
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
    saveAssistant.isPending ||
    savePayoutMode.isPending ||
    saveShiftRate.isPending ||
    toggleExclusion.isPending;

  const shiftRate = shiftRateQuery.data?.rate ?? null;
  const shiftRateParsed = Number(shiftRateDraft);
  const canSaveShiftRate = canWrite && shiftRateDraft.trim() !== "" && shiftRateParsed > 0;

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
        <div className="text-sm font-semibold">Ставка мойщиц</div>
        <div className="rounded-lg border bg-card p-4">
          <div className="flex flex-wrap items-end gap-3">
            <Label className="grid gap-1 text-sm">
              <span className="text-muted-foreground">Ставка за смену, ₽</span>
              <Input
                className="w-44"
                disabled={!canWrite || isBusy}
                inputMode="numeric"
                onChange={(event) => setShiftRateDraft(event.target.value)}
                placeholder={shiftRate != null ? String(shiftRate) : "0"}
                value={shiftRateDraft}
              />
            </Label>
            <Button
              disabled={!canSaveShiftRate || isBusy}
              onClick={() => saveShiftRate.mutate(shiftRateParsed)}
              size="sm"
              variant="outline"
            >
              Сохранить
            </Button>
            <div className="text-sm tabular-nums text-muted-foreground">
              Текущая ставка: {formatMoney(shiftRate)}
            </div>
          </div>
          <div className="mt-2 text-xs text-muted-foreground">
            Мойщица получает (её смены в периоде) × ставку — одна ставка на всех мойщиц. Смены
            отмечает управляющий в «График сотрудников → График мойщиц».
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
        ) : adminEmployees.length === 0 && !showAssistantRow ? (
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
                {showAssistantRow ? (
                  <tr className="border-t bg-primary/5">
                    <td className="px-3 py-2">
                      <Select
                        disabled={!canWrite || isBusy}
                        onValueChange={setAssistantCashierId}
                        value={selectedCashierId}
                      >
                        <SelectTrigger className="w-48">
                          <SelectValue placeholder="Выберите кассира" />
                        </SelectTrigger>
                        <SelectContent>
                          {cashiers.length === 0 ? (
                            <div className="px-2 py-1.5 text-xs text-muted-foreground">
                              Нет кассиров в штате
                            </div>
                          ) : (
                            cashiers.map((cashier) => (
                              <SelectItem key={cashier.id} value={cashier.id}>
                                {cashier.full_name}
                              </SelectItem>
                            ))
                          )}
                        </SelectContent>
                      </Select>
                    </td>
                    <td className="px-3 py-2 font-medium">{ASSISTANT_POSITION}</td>
                    <td className="px-3 py-2 tabular-nums">
                      {assistantExcluded ? "—" : formatMoney(assistantCurrentAmount)}
                      {assistantOverride &&
                      assistantOverride.employee_id === selectedCashierId &&
                      !assistantExcluded ? (
                        <span className="ml-1 text-xs text-primary">(персональный)</span>
                      ) : null}
                    </td>
                    <td className="px-3 py-2">
                      <Input
                        className="w-40"
                        disabled={!canWrite || isBusy || !selectedCashierId}
                        inputMode="numeric"
                        onChange={(event) => setAssistantAmount(event.target.value)}
                        placeholder={
                          assistantDefaultAmount != null ? String(assistantDefaultAmount) : "оклад"
                        }
                        value={assistantAmount}
                      />
                    </td>
                    <td className="px-3 py-2 text-center">
                      <Checkbox
                        aria-label="Не платить помощнику менеджера"
                        checked={assistantExcluded}
                        disabled={!canWrite || isBusy || !selectedCashierId}
                        onChange={(event) =>
                          toggleExclusion.mutate({
                            employeeId: selectedCashierId,
                            excluded: event.target.checked,
                          })
                        }
                      />
                    </td>
                    <td className="px-3 py-2 text-right">
                      <div className="flex justify-end gap-2">
                        <Button
                          disabled={!canSaveAssistant || isBusy}
                          onClick={() =>
                            saveAssistant.mutate({
                              employeeId: selectedCashierId,
                              amount: assistantEffectiveAmount,
                            })
                          }
                          size="sm"
                          variant="outline"
                        >
                          Сохранить
                        </Button>
                        {assistantOverride ? (
                          <Button
                            disabled={!canWrite || isBusy}
                            onClick={() => clearOverride.mutate(assistantOverride.employee_id)}
                            size="sm"
                            variant="ghost"
                          >
                            Сбросить
                          </Button>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                ) : null}
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
