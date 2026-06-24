import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, Check, CircleSlash, LoaderCircle, MoreHorizontal, Plus } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import { AccountSelect } from "@/components/ui-app/AccountSelect";
import { EmployeeCombobox } from "@/components/ui-app/EmployeeCombobox";
import { PageHeader } from "@/components/ui-app/PageHeader";
import {
  apiErrorMessage,
  cancelPayrollAdvance,
  createPayrollAdvance,
  getEmployees,
  getPayrollAdvanceAvailability,
  getPayrollAdvances,
  markPayrollAdvancePaid,
  writeOffPayrollAdvance,
  type PayrollAdvance,
  type PayrollAdvancePayoutStatus,
} from "@/lib/api";
import { usePermissions } from "@/lib/permissions";
import { cn } from "@/lib/utils";

const KIND_LABEL: Record<string, string> = { advance: "Аванс", loan: "Заём" };
const STATUS_LABEL: Record<string, string> = {
  issued: "Выдан",
  awaiting_payout: "Ожидает выплаты",
  recovered: "Погашен",
  written_off: "Списан",
  cancelled: "Отменён",
};

const PAYOUT_STATUS_LABEL: Record<PayrollAdvancePayoutStatus, string> = {
  disbursed: "Выплачено",
  sent_to_bank: "Отправлено в банк",
  awaiting_payout: "Ожидает выплаты",
  failed: "Отклонено банком",
  cancelled: "Отменено",
};

const PAYOUT_STATUS_VARIANT: Record<
  PayrollAdvancePayoutStatus,
  "default" | "secondary" | "destructive" | "outline"
> = {
  disbursed: "secondary",
  sent_to_bank: "outline",
  awaiting_payout: "default",
  failed: "destructive",
  cancelled: "outline",
};
const PAYOUT_METHODS = [
  { value: "transfer", label: "Перевод" },
  { value: "cash", label: "Наличные" },
  { value: "business_card", label: "Бизнес-карта" },
  { value: "other", label: "Другое" },
];

export function PayrollAdvancesRoute() {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const canIssue = permissions.canPerformAction("payroll.advances.issue");
  const canLoan = permissions.canPerformAction("payroll.loans.issue");

  const [employeeFilter, setEmployeeFilter] = useState("all");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [issueEmployeeId, setIssueEmployeeId] = useState("");
  const [amount, setAmount] = useState("");
  const [kind, setKind] = useState<"advance" | "loan">("advance");
  const [payoutMethod, setPayoutMethod] = useState("transfer");
  const [walletId, setWalletId] = useState("");
  const [installmentAmount, setInstallmentAmount] = useState("");
  const [recoveryStartDate, setRecoveryStartDate] = useState("");
  const [comment, setComment] = useState("");
  const [overrideCeiling, setOverrideCeiling] = useState(false);

  const employeesQuery = useQuery({ queryKey: ["employees"], queryFn: () => getEmployees({}) });
  const employees = employeesQuery.data ?? [];
  const employeeName = (id: string) =>
    employees.find((employee) => employee.id === id)?.full_name ?? "—";

  const advancesQuery = useQuery({
    queryKey: ["payroll-advances", employeeFilter],
    queryFn: () =>
      getPayrollAdvances(employeeFilter === "all" ? undefined : employeeFilter),
  });
  const advances = advancesQuery.data ?? [];

  const availabilityQuery = useQuery({
    queryKey: ["payroll-advance-availability", issueEmployeeId],
    queryFn: () => getPayrollAdvanceAvailability(issueEmployeeId),
    enabled: Boolean(issueEmployeeId) && dialogOpen,
  });
  const availability = availabilityQuery.data ?? null;
  const available = availability?.available ?? 0;
  const amountNumber = numericAmount(amount);
  const overEarned = amountNumber > available;
  const isLoan = kind === "loan";
  const advanceOverEarned = kind === "advance" && overEarned;

  function resetForm() {
    setIssueEmployeeId("");
    setAmount("");
    setKind("advance");
    setPayoutMethod("transfer");
    setWalletId("");
    setInstallmentAmount("");
    setRecoveryStartDate("");
    setComment("");
    setOverrideCeiling(false);
  }

  const issueMutation = useMutation({
    mutationFn: () =>
      createPayrollAdvance({
        employee_id: issueEmployeeId,
        amount: decimalInputPayload(amount),
        kind,
        payout_method: payoutMethod,
        wallet_id: walletId || undefined,
        installment_amount:
          isLoan && installmentAmount ? decimalInputPayload(installmentAmount) : undefined,
        recovery_start_date: isLoan && recoveryStartDate ? recoveryStartDate : undefined,
        comment: comment.trim() || undefined,
        override_ceiling: isLoan ? overrideCeiling : false,
      }),
    onSuccess: async () => {
      toast.success(isLoan ? "Заём выдан" : "Аванс выдан");
      setDialogOpen(false);
      resetForm();
      await queryClient.invalidateQueries({ queryKey: ["payroll-advances"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось выдать")),
  });

  const cancelMutation = useMutation({
    mutationFn: (id: string) => cancelPayrollAdvance(id),
    onSuccess: async () => {
      toast.success("Отменено");
      await queryClient.invalidateQueries({ queryKey: ["payroll-advances"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось отменить")),
  });

  const markPaidMutation = useMutation({
    mutationFn: (id: string) => markPayrollAdvancePaid(id),
    onSuccess: async () => {
      toast.success("Выдача подтверждена — списано с Сейфа");
      await queryClient.invalidateQueries({ queryKey: ["payroll-advances"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось подтвердить выдачу")),
  });

  const writeOffMutation = useMutation({
    mutationFn: (id: string) => writeOffPayrollAdvance(id),
    onSuccess: async () => {
      toast.success("Списано");
      await queryClient.invalidateQueries({ queryKey: ["payroll-advances"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось списать")),
  });

  const canSubmit =
    Boolean(issueEmployeeId) && amountNumber > 0 && (isLoan ? canLoan : !overEarned);

  const columns: Array<DataTableColumn<PayrollAdvance>> = [
    { key: "employee", header: "Сотрудник", cell: (row) => employeeName(row.employee_id) },
    {
      key: "kind",
      header: "Тип",
      cell: (row) => (
        <Badge variant={row.kind === "loan" ? "destructive" : "secondary"}>
          {KIND_LABEL[row.kind] ?? row.kind}
        </Badge>
      ),
    },
    { key: "amount", header: "Сумма", cell: (row) => formatMoney(row.amount) },
    { key: "recovered", header: "Погашено", cell: (row) => formatMoney(row.recovered_amount) },
    {
      key: "outstanding",
      header: "Остаток",
      cell: (row) => formatMoney(row.amount - row.recovered_amount),
    },
    { key: "issued_on", header: "Дата", cell: (row) => formatDate(row.issued_on) },
    {
      key: "status",
      header: "Статус",
      cell: (row) =>
        row.payout_status === "disbursed" ? (
          STATUS_LABEL[row.status] ?? row.status
        ) : (
          <Badge variant={PAYOUT_STATUS_VARIANT[row.payout_status]}>
            {PAYOUT_STATUS_LABEL[row.payout_status]}
          </Badge>
        ),
    },
    {
      key: "actions",
      header: "",
      headerClassName: "w-32",
      cell: (row) => {
        const canMarkPaid = row.payout_status === "awaiting_payout" && (canIssue || canLoan);
        const canCancel =
          canIssue &&
          Number(row.recovered_amount) === 0 &&
          (row.status === "issued" || row.status === "awaiting_payout");
        const canWriteOff = canLoan && row.status === "issued";
        if (!canMarkPaid && !canCancel && !canWriteOff) {
          return null;
        }
        return (
          <div className="flex items-center justify-end gap-1">
            {canMarkPaid ? (
              <Button
                size="sm"
                disabled={markPaidMutation.isPending}
                onClick={() => markPaidMutation.mutate(row.id)}
              >
                <Check className="mr-1 h-4 w-4" /> Выплачено
              </Button>
            ) : null}
            {canCancel || canWriteOff ? (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button variant="ghost" size="icon" className="h-8 w-8">
                    <MoreHorizontal className="h-4 w-4" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  {canCancel ? (
                    <DropdownMenuItem onClick={() => cancelMutation.mutate(row.id)}>
                      <Ban className="mr-2 h-4 w-4" /> Отменить
                    </DropdownMenuItem>
                  ) : null}
                  {canWriteOff ? (
                    <DropdownMenuItem onClick={() => writeOffMutation.mutate(row.id)}>
                      <CircleSlash className="mr-2 h-4 w-4" /> Списать остаток
                    </DropdownMenuItem>
                  ) : null}
                </DropdownMenuContent>
              </DropdownMenu>
            ) : null}
          </div>
        );
      },
    },
  ];

  return (
    <div className="space-y-5">
      <PageHeader
        title="Авансы и займы"
        description="Выдача денег в счёт зарплаты: аванс в пределах заработанного, заём — сверх."
        action={
          canIssue ? (
            <Button onClick={() => setDialogOpen(true)}>
              <Plus className="mr-2 h-4 w-4" /> Выдать аванс
            </Button>
          ) : null
        }
      />

      <EmployeeCombobox
        allOptionLabel="Все сотрудники"
        employees={employees}
        value={employeeFilter}
        onChange={setEmployeeFilter}
        className="w-72"
      />

      <DataTable
        columns={columns}
        rows={advances}
        isLoading={advancesQuery.isLoading}
        getRowKey={(row) => row.id}
        emptyMessage="Авансов и займов пока нет"
      />

      <Dialog
        open={dialogOpen}
        onOpenChange={(open) => {
          setDialogOpen(open);
          if (!open) resetForm();
        }}
      >
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>{isLoan ? "Выдать заём" : "Выдать аванс"}</DialogTitle>
            <DialogDescription>
              Аванс — в пределах заработанного, гасится разом. Заём — деньги в долг,
              гасятся в рассрочку с выбранной даты.
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-4">
            <Label className="grid gap-2">
              <span>Сотрудник</span>
              <EmployeeCombobox
                employees={employees.filter((employee) => employee.status === "active")}
                value={issueEmployeeId}
                onChange={setIssueEmployeeId}
              />
            </Label>

            <div className="grid gap-2">
              <span className="text-sm font-medium">Тип выдачи</span>
              <div className="inline-flex w-fit overflow-hidden rounded-md border">
                <button
                  type="button"
                  className={cn(
                    "px-4 py-1.5 text-sm",
                    !isLoan && "bg-primary/10 font-medium text-primary",
                  )}
                  onClick={() => setKind("advance")}
                >
                  Аванс
                </button>
                <button
                  type="button"
                  disabled={!canLoan}
                  className={cn(
                    "px-4 py-1.5 text-sm disabled:cursor-not-allowed disabled:opacity-50",
                    isLoan && "bg-primary/10 font-medium text-primary",
                  )}
                  onClick={() => setKind("loan")}
                >
                  Заём
                </button>
              </div>
            </div>

            {issueEmployeeId ? (
              <div className="rounded-md border bg-muted/40 p-3 text-sm">
                {availabilityQuery.isLoading ? (
                  "Считаем доступное…"
                ) : availability?.note ? (
                  <span className="text-amber-600">{availability.note}</span>
                ) : (
                  <>
                    Доступно к авансу сегодня: <b>{formatMoney(available)}</b>
                  </>
                )}
              </div>
            ) : null}

            <Label className="grid gap-2">
              <span>Сумма, ₽</span>
              <Input
                type="number"
                inputMode="decimal"
                value={amount}
                onChange={(event) => setAmount(event.target.value)}
                placeholder="0"
              />
            </Label>

            {advanceOverEarned ? (
              <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-800">
                Сумма превышает заработанное ({formatMoney(available)}). Это можно выдать только
                как <b>заём</b> — переключите тип выдачи.
              </div>
            ) : null}

            {isLoan ? (
              <>
                <div className="rounded-md border bg-muted/40 p-3 text-sm text-muted-foreground">
                  Заём гасится в рассрочку. Часть в пределах заработанного (
                  {formatMoney(Math.min(amountNumber, available))}) тоже идёт в долг, а не
                  списывается сразу.
                </div>
                <Label className="grid gap-2">
                  <span>Сумма удержания за период, ₽</span>
                  <Input
                    type="number"
                    inputMode="decimal"
                    value={installmentAmount}
                    onChange={(event) => setInstallmentAmount(event.target.value)}
                    placeholder="Пусто — весь заём одной ведомостью"
                  />
                </Label>
                <Label className="grid gap-2">
                  <span>Удерживать с (необязательно)</span>
                  <Input
                    type="date"
                    value={recoveryStartDate}
                    onChange={(event) => setRecoveryStartDate(event.target.value)}
                  />
                </Label>
                {canLoan ? null : (
                  <div className="text-sm text-amber-600">
                    У вас нет права на выдачу займов.
                  </div>
                )}
                <label className="flex items-center gap-2 text-sm text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={overrideCeiling}
                    onChange={(event) => setOverrideCeiling(event.target.checked)}
                  />
                  Превысить потолок займа (подтверждаю)
                </label>
              </>
            ) : null}

            <Label className="grid gap-2">
              <span>Способ выплаты</span>
              <Select value={payoutMethod} onValueChange={setPayoutMethod}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {PAYOUT_METHODS.map((method) => (
                    <SelectItem key={method.value} value={method.value}>
                      {method.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Label>

            <Label className="grid gap-2">
              <span>Счёт списания</span>
              <AccountSelect value={walletId} onChange={setWalletId} />
              <span className="text-xs text-muted-foreground">
                Наличные (ТК Черникова / Сейф) — прямой расход в ДДС; банк — через черновик и
                перевод на Сейф.
              </span>
            </Label>

            <Label className="grid gap-2">
              <span>Комментарий</span>
              <Input
                value={comment}
                onChange={(event) => setComment(event.target.value)}
                placeholder="Необязательно"
              />
            </Label>
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => setDialogOpen(false)}>
              Отмена
            </Button>
            <Button disabled={!canSubmit || issueMutation.isPending} onClick={() => issueMutation.mutate()}>
              {issueMutation.isPending ? (
                <LoaderCircle className="mr-2 h-4 w-4 animate-spin" />
              ) : null}
              {isLoan ? "Выдать заём" : "Выдать аванс"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function formatMoney(value: number) {
  return new Intl.NumberFormat("ru-RU", {
    maximumFractionDigits: 0,
    style: "currency",
    currency: "RUB",
  }).format(value);
}

function formatDate(value: string | null | undefined) {
  if (!value) {
    return "—";
  }
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "2-digit",
  }).format(new Date(`${value}T00:00:00`));
}

function numericAmount(value: string | number | null | undefined) {
  const amount = Number(String(value ?? "0").replace(",", "."));
  return Number.isFinite(amount) ? amount : 0;
}

function decimalInputPayload(value: string) {
  return String(numericAmount(value).toFixed(2));
}
