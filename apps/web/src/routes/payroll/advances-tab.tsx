import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, CircleSlash, LoaderCircle, MoreHorizontal, Plus } from "lucide-react";
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
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import { EmployeeCombobox } from "@/components/ui-app/EmployeeCombobox";
import { PageHeader } from "@/components/ui-app/PageHeader";
import {
  apiErrorMessage,
  cancelPayrollAdvance,
  createPayrollAdvance,
  getAdvanceIssueWallets,
  getEmployees,
  getPayrollAdvanceAvailability,
  getPayrollAdvances,
  revokeKassaPayrollAdvance,
  writeOffPayrollAdvance,
  type PayrollAdvance,
} from "@/lib/api";
import { usePermissions } from "@/lib/permissions";
import { cn } from "@/lib/utils";

const PAYROLL_WALLET = "payroll";
const KASSA_WALLET_CODE = "tk_chernikova";
const KIND_LABEL: Record<string, string> = { advance: "Аванс", loan: "Заём" };
const STATUS_LABEL: Record<string, string> = {
  issued: "Выдан",
  awaiting_payout: "Ожидает выдачи",
  recovered: "Погашен",
  written_off: "Списан",
  cancelled: "Отменён",
};

export function PayrollAdvancesRoute() {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const canIssue = permissions.canPerformAction("payroll.advances.issue");
  const canLoan = permissions.canPerformAction("payroll.loans.issue");
  const canBackdate = permissions.hasPermission("payroll.advances.backdate");

  const [employeeFilter, setEmployeeFilter] = useState("all");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [issueEmployeeId, setIssueEmployeeId] = useState("");
  const [amount, setAmount] = useState("");
  const [kind, setKind] = useState<"advance" | "loan">("advance");
  const [installmentAmount, setInstallmentAmount] = useState("");
  const [recoveryStartDate, setRecoveryStartDate] = useState("");
  const [comment, setComment] = useState("");
  const [overrideCeiling, setOverrideCeiling] = useState(false);
  const [issuedOn, setIssuedOn] = useState(todayIso);
  const [walletId, setWalletId] = useState(PAYROLL_WALLET);

  const employeesQuery = useQuery({ queryKey: ["employees"], queryFn: () => getEmployees({}) });
  const employees = employeesQuery.data ?? [];

  const employeeName = (id: string) =>
    employees.find((employee) => employee.id === id)?.full_name ?? "—";

  const advancesQuery = useQuery({
    queryKey: ["payroll-advances", employeeFilter],
    queryFn: () => getPayrollAdvances(employeeFilter === "all" ? undefined : employeeFilter),
  });
  const advances = advancesQuery.data ?? [];

  const availabilityQuery = useQuery({
    queryKey: ["payroll-advance-availability", issueEmployeeId, issuedOn],
    queryFn: () => getPayrollAdvanceAvailability(issueEmployeeId, issuedOn),
    enabled: Boolean(issueEmployeeId) && dialogOpen,
  });
  const availability = availabilityQuery.data ?? null;
  const available = availability?.available ?? 0;
  const issueWalletsQuery = useQuery({
    queryKey: ["advance-issue-wallets"],
    queryFn: getAdvanceIssueWallets,
    enabled: dialogOpen,
  });
  const issueWallets = issueWalletsQuery.data ?? [];
  const amountNumber = numericAmount(amount);
  const overEarned = amountNumber > available;
  const isLoan = kind === "loan";
  const advanceOverEarned = kind === "advance" && overEarned;
  const isBackdated = issuedOn < todayIso();
  const throughPayroll = walletId === PAYROLL_WALLET;
  // ТК Черникова сегодняшней датой = разрешение на выдачу через кассу (не мгновенно);
  // задним числом деньги уже выданы — остаётся мгновенная фиксация.
  const selectedWallet = issueWallets.find((wallet) => wallet.id === walletId) ?? null;
  const viaKassaPermission =
    !throughPayroll && !isBackdated && selectedWallet?.code === KASSA_WALLET_CODE;

  function resetForm() {
    setIssueEmployeeId("");
    setAmount("");
    setKind("advance");
    setInstallmentAmount("");
    setRecoveryStartDate("");
    setComment("");
    setOverrideCeiling(false);
    setIssuedOn(todayIso());
    setWalletId(PAYROLL_WALLET);
  }

  const issueMutation = useMutation({
    mutationFn: () =>
      createPayrollAdvance({
        employee_id: issueEmployeeId,
        amount: decimalInputPayload(amount),
        kind,
        issued_on: issuedOn,
        payout_method: throughPayroll ? "payroll" : undefined,
        wallet_id: throughPayroll ? undefined : walletId,
        installment_amount:
          isLoan && installmentAmount ? decimalInputPayload(installmentAmount) : undefined,
        recovery_start_date: isLoan && recoveryStartDate ? recoveryStartDate : undefined,
        comment: comment.trim() || undefined,
        override_ceiling: isLoan ? overrideCeiling : false,
      }),
    onSuccess: async (created) => {
      toast.success(
        created.payout_status === "awaiting_kassa"
          ? "Разрешение создано — уйдёт в кассу, выдаст администратор"
          : isLoan
            ? "Заём добавлен в ведомость"
            : "Аванс добавлен в ведомость",
      );
      setDialogOpen(false);
      resetForm();
      await queryClient.invalidateQueries({ queryKey: ["payroll-advances"] });
      await queryClient.invalidateQueries({ queryKey: ["payroll-runs"] });
      await queryClient.invalidateQueries({ queryKey: ["payroll-run"] });
      await queryClient.invalidateQueries({ queryKey: ["payroll-run-lines"] });
      await queryClient.invalidateQueries({ queryKey: ["kassa"] });
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

  // Отзыв разрешения на выдачу через кассу — до исполнения администратором (после — 409).
  const revokeMutation = useMutation({
    mutationFn: (id: string) => revokeKassaPayrollAdvance(id),
    onSuccess: async () => {
      toast.success("Разрешение отозвано");
      await queryClient.invalidateQueries({ queryKey: ["payroll-advances"] });
      await queryClient.invalidateQueries({ queryKey: ["kassa"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось отозвать")),
  });

  const writeOffMutation = useMutation({
    mutationFn: (id: string) => writeOffPayrollAdvance(id),
    onSuccess: async () => {
      toast.success("Списано");
      await queryClient.invalidateQueries({ queryKey: ["payroll-advances"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось списать")),
  });

  // Будущей датой нельзя; прошлой — только с правом backdate.
  const dateValid = issuedOn <= todayIso() && (canBackdate || !isBackdated);
  // Задним числом деньги уже выданы — «через ведомость» недоступно, нужен конкретный счёт.
  const walletValid = !isBackdated || !throughPayroll;
  const canSubmit =
    Boolean(issueEmployeeId) &&
    amountNumber > 0 &&
    dateValid &&
    walletValid &&
    (isLoan ? canLoan : !overEarned);

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
      cell: (row) => {
        // Кассовые состояния: разрешение в пути и отклонение админом кассы.
        if (row.payout_status === "awaiting_kassa") {
          return (
            <Badge className="border-amber-200 bg-amber-50 text-amber-700">Ждёт кассу</Badge>
          );
        }
        if (row.payout_status === "cancelled_by_kassa") {
          return <span className="text-muted-foreground">Отменено кассой</span>;
        }
        return STATUS_LABEL[row.status] ?? row.status;
      },
    },
    {
      key: "actions",
      header: "",
      headerClassName: "w-10",
      cell: (row) =>
        row.payout_status === "awaiting_kassa" && canIssue ? (
          <Button
            size="sm"
            variant="ghost"
            disabled={revokeMutation.isPending}
            onClick={() => revokeMutation.mutate(row.id)}
            title="Отозвать разрешение, пока администратор кассы его не исполнил"
          >
            Отозвать
          </Button>
        ) : row.status === "issued" && (canIssue || canLoan) ? (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="icon" className="h-8 w-8">
                <MoreHorizontal className="h-4 w-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {canIssue ? (
                <DropdownMenuItem
                  disabled={Number(row.recovered_amount) > 0}
                  onClick={() => cancelMutation.mutate(row.id)}
                >
                  <Ban className="mr-2 h-4 w-4" /> Отменить
                </DropdownMenuItem>
              ) : null}
              {canLoan ? (
                <DropdownMenuItem onClick={() => writeOffMutation.mutate(row.id)}>
                  <CircleSlash className="mr-2 h-4 w-4" /> Списать остаток
                </DropdownMenuItem>
              ) : null}
            </DropdownMenuContent>
          </DropdownMenu>
        ) : null,
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
              <Plus className="mr-2 h-4 w-4" /> Добавить аванс
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
        <DialogContent className="grid max-h-[calc(100dvh-2rem)] max-w-lg grid-rows-[auto_minmax(0,1fr)_auto] gap-3 overflow-hidden p-4 sm:p-5">
          <DialogHeader className="space-y-1">
            <DialogTitle>{isLoan ? "Добавить заём" : "Добавить аванс"}</DialogTitle>
            <DialogDescription>
              Аванс — в пределах заработанного. Заём — деньги в долг, гасится в рассрочку.
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-3 overflow-y-auto pr-1">
            <Label className="grid gap-1.5">
              <span>Сотрудник</span>
              <EmployeeCombobox
                employees={employees.filter((employee) => employee.status === "active")}
                value={issueEmployeeId}
                onChange={setIssueEmployeeId}
              />
            </Label>

            <div className="grid gap-1.5">
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

            <Label className="grid gap-1.5">
              <span>Дата выдачи</span>
              <Input
                type="date"
                value={issuedOn}
                max={todayIso()}
                min={canBackdate ? undefined : todayIso()}
                onChange={(event) => {
                  const value = event.target.value || todayIso();
                  setIssuedOn(value);
                  if (
                    value < todayIso() &&
                    walletId === PAYROLL_WALLET &&
                    issueWallets.length > 0
                  ) {
                    setWalletId(issueWallets[0].id);
                  }
                }}
              />
              <span className="text-xs text-muted-foreground">
                {isBackdated
                  ? "Задним числом: деньги уже выданы — выберите счёт выдачи ниже."
                  : canBackdate
                    ? "Сегодня или задним числом. Будущей датой нельзя."
                    : "Только сегодня. Для прошлых дат нужно отдельное право."}
              </span>
            </Label>

            {issueEmployeeId ? (
              <div className="rounded-md border bg-muted/40 p-2.5 text-sm">
                {availabilityQuery.isLoading ? (
                  "Считаем доступное…"
                ) : availability?.note ? (
                  <span className="text-amber-600">{availability.note}</span>
                ) : (
                  <>
                    Доступно к авансу {isBackdated ? `на ${formatDate(issuedOn)}` : "сегодня"}:{" "}
                    <b>{formatMoney(available)}</b>
                  </>
                )}
              </div>
            ) : null}

            <Label className="grid gap-1.5">
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
                Сумма превышает заработанное ({formatMoney(available)}). Это можно выдать только как{" "}
                <b>заём</b> — переключите тип выдачи.
              </div>
            ) : null}

            {isLoan ? (
              <>
                <div className="rounded-md border bg-muted/40 p-2.5 text-sm text-muted-foreground">
                  В пределах заработанного: {formatMoney(Math.min(amountNumber, available))}; эта
                  часть тоже идёт в долг.
                </div>
                <Label className="grid gap-1.5">
                  <span>Сумма удержания за период, ₽</span>
                  <Input
                    type="number"
                    inputMode="decimal"
                    value={installmentAmount}
                    onChange={(event) => setInstallmentAmount(event.target.value)}
                    placeholder="Пусто — весь заём одной ведомостью"
                  />
                </Label>
                <Label className="grid gap-1.5">
                  <span>Удерживать с выплаты</span>
                  <Input
                    type="date"
                    value={recoveryStartDate}
                    onChange={(event) => setRecoveryStartDate(event.target.value)}
                  />
                  <span className="text-xs text-muted-foreground">
                    Пусто — с самой ранней незафинализированной выплаты.
                  </span>
                </Label>
                {canLoan ? null : (
                  <div className="text-sm text-amber-600">У вас нет права на выдачу займов.</div>
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

            <Label className="grid gap-1.5">
              <span>Счёт выдачи</span>
              <select
                className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm disabled:opacity-50"
                value={walletId}
                onChange={(event) => setWalletId(event.target.value)}
              >
                {!isBackdated ? (
                  <option value={PAYROLL_WALLET}>Через ведомость (с ближайшей ЗП)</option>
                ) : null}
                {issueWallets.map((wallet) => (
                  <option key={wallet.id} value={wallet.id}>
                    {wallet.name}
                    {wallet.channel === "bank" ? " (банк)" : ""}
                  </option>
                ))}
              </select>
              <span
                className={cn(
                  "text-xs",
                  viaKassaPermission ? "font-medium text-amber-600" : "text-muted-foreground",
                )}
              >
                {throughPayroll
                  ? "Сумма уйдёт вместе с зарплатой по ближайшей незафинализированной ведомости."
                  : viaKassaPermission
                    ? "Уйдёт в кассу — выдаст администратор. Деньги и удержания появятся после фактической выдачи."
                    : "Наличные — расход в ДДС сразу; банк — черновик платежа в Т-Банке. Удержание — из ближайшей ЗП."}
              </span>
            </Label>

            <Label className="grid gap-1.5">
              <span>Комментарий</span>
              <Input
                value={comment}
                onChange={(event) => setComment(event.target.value)}
                placeholder="Необязательно"
              />
            </Label>
          </div>

          <DialogFooter className="pt-1">
            <Button variant="outline" onClick={() => setDialogOpen(false)}>
              Отмена
            </Button>
            <Button
              disabled={!canSubmit || issueMutation.isPending}
              onClick={() => issueMutation.mutate()}
            >
              {issueMutation.isPending ? (
                <LoaderCircle className="mr-2 h-4 w-4 animate-spin" />
              ) : null}
              {isLoan ? "Добавить заём" : "Добавить аванс"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function todayIso() {
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
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
