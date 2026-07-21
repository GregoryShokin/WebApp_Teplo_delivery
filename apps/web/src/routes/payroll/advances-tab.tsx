import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, ChevronDown, CircleSlash, LoaderCircle, MoreHorizontal, Plus } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
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
  getUpcomingPayslips,
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
  // Выбранная ведомость для удержания займа «через ведомость» (индекс среди ближайших).
  const [payslipIdx, setPayslipIdx] = useState(0);
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
  // День выплаты: заработанное уходит с ведомостью — аванс за период уже недоступен.
  const payoutReached = availability?.payout_reached ?? false;
  const issueWalletsQuery = useQuery({
    queryKey: ["advance-issue-wallets"],
    queryFn: getAdvanceIssueWallets,
    enabled: dialogOpen,
  });
  const issueWallets = useMemo(() => issueWalletsQuery.data ?? [], [issueWalletsQuery.data]);
  // Ведомости для удержания займа: все созданные нефинализированные и ближайшие по расписанию.
  const payslipsQuery = useQuery({
    // Без сотрудника — недельное расписание по умолчанию (выплаты по вторникам); с
    // сотрудником — под его пайплайн (недельный/полумесячный).
    queryKey: ["advance-upcoming-payslips", issueEmployeeId || "default"],
    queryFn: () => getUpcomingPayslips(issueEmployeeId || undefined),
    enabled: kind === "loan" && walletId === PAYROLL_WALLET && dialogOpen,
  });
  const payslips = useMemo(() => payslipsQuery.data ?? [], [payslipsQuery.data]);
  const amountNumber = numericAmount(amount);
  const overEarned = amountNumber > available;
  const isLoan = kind === "loan";
  const advanceOverEarned = kind === "advance" && overEarned;
  const availabilityReady = Boolean(issueEmployeeId) && !availabilityQuery.isLoading;
  // Заём в день выплаты: available уже 0, но справочно показываем полное заработанное.
  const earnedToDate = availability?.earned_to_date ?? 0;
  const loanReference = payoutReached ? earnedToDate : available;
  // Note-строку не дублируем, когда её перекрывает amber-box (аванс + день выплаты + сумма),
  // и не показываем payout-note про аванс при выбранном займе (к займу неприменима).
  const showAvailabilityNote =
    Boolean(availability?.note) && !(payoutReached && (isLoan || advanceOverEarned));
  const isBackdated = issuedOn < todayIso();
  const throughPayroll = walletId === PAYROLL_WALLET;
  const selectedPayslip = payslips[payslipIdx] ?? payslips[0] ?? null;
  // ТК Черникова сегодняшней датой = разрешение на выдачу через кассу (не мгновенно);
  // задним числом деньги уже выданы — остаётся мгновенная фиксация.
  const selectedWallet = issueWallets.find((wallet) => wallet.id === walletId) ?? null;
  const viaKassaPermission =
    !throughPayroll && !isBackdated && selectedWallet?.code === KASSA_WALLET_CODE;

  // Аванс — это досрочная выдача уже заработанного ЖИВЫМИ деньгами (нал/банк). «Через
  // ведомость» для аванса бессмысленно: деньги ушли бы вместе с ЗП в день выплаты, когда
  // заработанное уже обнуляется выплатой. Поэтому канал «через ведомость» — только у займа;
  // для аванса выбираем конкретный счёт (по умолчанию — первый доступный).
  useEffect(() => {
    if (kind === "advance" && walletId === PAYROLL_WALLET && issueWallets.length > 0) {
      setWalletId(issueWallets[0].id);
    }
  }, [kind, walletId, issueWallets]);

  // «Через ведомость»: выбор ведомости = с какой ЗП удерживать. Первая (ближайшая) →
  // recovery_start пусто (с ближайшей); последующие → удержание с начала выбранного периода.
  useEffect(() => {
    if (!throughPayroll || payslips.length === 0) return;
    const idx = payslipIdx < payslips.length ? payslipIdx : 0;
    if (idx !== payslipIdx) setPayslipIdx(idx);
    setRecoveryStartDate(idx === 0 ? "" : payslips[idx].period_start);
  }, [throughPayroll, payslips, payslipIdx]);

  // Дата ближайшей выплаты, из которой удержим (для «через ведомость» — выбранная).
  const nearestPayoutLabel = throughPayroll
    ? selectedPayslip
      ? formatDate(selectedPayslip.payout_date)
      : null
    : availability?.period_end
      ? formatDate(availability.period_end)
      : null;

  function resetForm() {
    setIssueEmployeeId("");
    setAmount("");
    setKind("advance");
    setInstallmentAmount("");
    setRecoveryStartDate("");
    setPayslipIdx(0);
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
        <DialogContent className="max-h-[calc(100dvh-2rem)] max-w-2xl overflow-hidden p-0">
          <div className="grid grid-cols-1 sm:grid-cols-[minmax(0,1.35fr)_minmax(0,1fr)]">
            <div className="max-h-[calc(100dvh-2rem)] overflow-y-auto p-5">
              <DialogHeader className="space-y-1 text-left">
                <DialogTitle>{isLoan ? "Выдать заём" : "Выдать аванс"}</DialogTitle>
                <DialogDescription>
                  {isLoan
                    ? "Деньги в долг сверх заработанного — гасятся из будущих зарплат."
                    : "Часть уже заработанной зарплаты, досрочно. Удержим из ближайшей выплаты."}
                </DialogDescription>
              </DialogHeader>

              <div className="mt-4 grid gap-4">
                <div className="grid gap-2.5">
                  <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                    Кому и сколько
                  </span>
                  <EmployeeCombobox
                    employees={employees.filter((employee) => employee.status === "active")}
                    value={issueEmployeeId}
                    onChange={setIssueEmployeeId}
                  />
                  <div className="grid grid-cols-2 gap-2">
                    <button
                      type="button"
                      onClick={() => setKind("advance")}
                      className={cn(
                        "rounded-md border py-2 text-center text-sm",
                        !isLoan
                          ? "border-primary bg-primary/5 font-medium text-primary ring-1 ring-primary"
                          : "border-input hover:bg-muted/50",
                      )}
                    >
                      Аванс
                    </button>
                    <button
                      type="button"
                      disabled={!canLoan}
                      onClick={() => setKind("loan")}
                      className={cn(
                        "rounded-md border py-2 text-center text-sm disabled:cursor-not-allowed disabled:opacity-50",
                        isLoan
                          ? "border-primary bg-primary/5 font-medium text-primary ring-1 ring-primary"
                          : "border-input hover:bg-muted/50",
                      )}
                    >
                      Заём
                    </button>
                  </div>
                  <Input
                    type="text"
                    inputMode="decimal"
                    value={amount}
                    onChange={(event) => setAmount(event.target.value)}
                    placeholder="Сумма, ₽"
                  />
                  {issueEmployeeId ? (
                    availabilityQuery.isLoading ? (
                      <span className="text-xs text-muted-foreground">Считаем заработанное…</span>
                    ) : showAvailabilityNote ? (
                      <span className="text-xs text-amber-600">{availability!.note}</span>
                    ) : (
                      <span className="text-xs text-muted-foreground">
                        {isLoan ? "В пределах заработанного" : "Доступно к авансу"}:{" "}
                        <b className="font-medium text-foreground">
                          {formatMoney(isLoan ? loanReference : available)}
                        </b>
                      </span>
                    )
                  ) : null}
                  {availabilityReady && advanceOverEarned ? (
                    payoutReached ? (
                      <div className="rounded-md border border-amber-300 bg-amber-50 p-2.5 text-xs text-amber-800">
                        Наступил день выплаты — заработанное уходит с ведомостью. Аванс за этот
                        период уже недоступен{canLoan ? "; можно оформить заём" : ""}.
                      </div>
                    ) : (
                      <div className="rounded-md border border-amber-300 bg-amber-50 p-2.5 text-xs text-amber-800">
                        Превышает заработанное ({formatMoney(available)}) — выдать можно только как{" "}
                        <b>заём</b>.
                      </div>
                    )
                  ) : null}
                </div>

                {isLoan ? (
                  <div className="grid gap-2.5">
                    <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                      Как удержать
                    </span>
                    <div
                      className={cn("grid gap-2", throughPayroll ? "grid-cols-1" : "grid-cols-2")}
                    >
                      <Label className="grid gap-1.5">
                        <span className="text-sm">Удержание за период</span>
                        <Input
                          type="text"
                          inputMode="decimal"
                          value={installmentAmount}
                          onChange={(event) => setInstallmentAmount(event.target.value)}
                          placeholder="весь заём сразу"
                        />
                      </Label>
                      {/* «Через ведомость» задаёт старт удержания через выбор ведомости ниже. */}
                      {throughPayroll ? null : (
                        <Label className="grid gap-1.5">
                          <span className="text-sm">Удерживать с</span>
                          <Input
                            type="date"
                            value={recoveryStartDate}
                            onChange={(event) => setRecoveryStartDate(event.target.value)}
                          />
                        </Label>
                      )}
                    </div>
                    {canLoan ? null : (
                      <div className="text-xs text-amber-600">У вас нет права на выдачу займов.</div>
                    )}
                    <label className="flex items-center gap-2 text-sm text-muted-foreground">
                      <input
                        type="checkbox"
                        checked={overrideCeiling}
                        onChange={(event) => setOverrideCeiling(event.target.checked)}
                      />
                      Превысить потолок займа (подтверждаю)
                    </label>
                  </div>
                ) : null}

                <div className="grid gap-2.5">
                  <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                    Как выдать
                  </span>
                  <div className="grid grid-cols-2 gap-2">
                    <Label className="grid gap-1.5">
                      <span className="text-sm">Счёт выдачи</span>
                      <div className="relative">
                        <select
                          className="h-10 w-full appearance-none rounded-md border border-input bg-background pl-3 pr-9 text-sm disabled:opacity-50"
                          value={walletId}
                          onChange={(event) => setWalletId(event.target.value)}
                        >
                          {isLoan && !isBackdated ? (
                            <option value={PAYROLL_WALLET}>Через ведомость</option>
                          ) : null}
                          {issueWallets.map((wallet) => (
                            <option key={wallet.id} value={wallet.id}>
                              {wallet.name}
                              {wallet.channel === "bank" ? " (банк)" : ""}
                            </option>
                          ))}
                        </select>
                        <ChevronDown
                          className="pointer-events-none absolute right-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
                          aria-hidden="true"
                        />
                      </div>
                    </Label>
                    {throughPayroll ? (
                      <Label className="grid gap-1.5">
                        <span className="text-sm">Удержать из ЗП</span>
                        <div className="relative">
                          <select
                            className="h-10 w-full appearance-none rounded-md border border-input bg-background pl-3 pr-9 text-sm disabled:opacity-50"
                            value={payslipIdx}
                            disabled={payslips.length === 0}
                            onChange={(event) => setPayslipIdx(Number(event.target.value))}
                          >
                            {payslips.length === 0 ? (
                              <option value={0}>
                                {payslipsQuery.isLoading ? "Загрузка…" : "Нет ближайших ведомостей"}
                              </option>
                            ) : (
                              payslips.map((payslip, idx) => (
                                <option key={payslip.payout_date} value={idx}>
                                  ЗП {formatDate(payslip.payout_date)}
                                  {idx === 0 ? " (ближайшая)" : ""}
                                </option>
                              ))
                            )}
                          </select>
                          <ChevronDown
                            className="pointer-events-none absolute right-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
                            aria-hidden="true"
                          />
                        </div>
                      </Label>
                    ) : (
                      <Label className="grid gap-1.5">
                        <span className="text-sm">Дата выдачи</span>
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
                      </Label>
                    )}
                  </div>
                  {viaKassaPermission ? (
                    <span className="text-xs font-medium text-amber-600">
                      Уйдёт в кассу — выдаст администратор. Деньги и удержание появятся после
                      фактической выдачи.
                    </span>
                  ) : null}
                  <Label className="grid gap-1.5">
                    <span className="text-sm">Комментарий</span>
                    <Input
                      value={comment}
                      onChange={(event) => setComment(event.target.value)}
                      placeholder="Необязательно"
                    />
                  </Label>
                </div>
              </div>
            </div>

            <div className="border-t p-5 sm:border-l sm:border-t-0">
              <div className="rounded-xl border bg-muted/40 p-4">
                <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  Что произойдёт
                </span>
                {issueEmployeeId ? (
                  <div className="mt-3 grid gap-3">
                    <div className="flex items-center gap-2.5">
                      <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-primary/10 text-xs font-medium text-primary">
                        {employeeName(issueEmployeeId)
                          .split(" ")
                          .filter(Boolean)
                          .slice(0, 2)
                          .map((word) => word[0])
                          .join("")
                          .toUpperCase()}
                      </div>
                      <div className="min-w-0">
                        <div className="truncate text-sm font-medium">
                          {employeeName(issueEmployeeId)}
                        </div>
                        <div className="truncate text-xs text-muted-foreground">
                          {employees.find((employee) => employee.id === issueEmployeeId)?.position ??
                            "—"}
                        </div>
                      </div>
                    </div>
                    <div className="grid gap-2 text-sm">
                      <div className="flex items-baseline justify-between gap-2">
                        <span className="text-muted-foreground">Получит</span>
                        <b className="font-medium tabular-nums">{formatMoney(amountNumber)}</b>
                      </div>
                      <div className="flex items-baseline justify-between gap-2">
                        <span className="text-muted-foreground">Способ</span>
                        <span className="text-right">
                          {throughPayroll ? "через ведомость" : (selectedWallet?.name ?? "—")}
                        </span>
                      </div>
                      <div className="flex items-baseline justify-between gap-2">
                        <span className="text-muted-foreground">Удержим из</span>
                        <span className="text-right">
                          {nearestPayoutLabel ? `ЗП ${nearestPayoutLabel}` : "ближайшей ЗП"}
                        </span>
                      </div>
                      <div className="flex items-baseline justify-between gap-2">
                        <span className="text-muted-foreground">{isLoan ? "Оформлен" : "Выдан"}</span>
                        <span className="text-right">{formatDate(issuedOn)}</span>
                      </div>
                    </div>
                    {availabilityReady && advanceOverEarned ? (
                      <div className="rounded-md bg-amber-50 p-2.5 text-xs text-amber-800">
                        {payoutReached
                          ? "День выплаты — аванс за этот период недоступен, оформите заём."
                          : "Сумма больше заработанного — переключите на заём."}
                      </div>
                    ) : null}
                  </div>
                ) : (
                  <p className="mt-3 text-sm text-muted-foreground">
                    Выберите сотрудника и сумму — здесь появится итог.
                  </p>
                )}
                <Button
                  className="mt-4 w-full"
                  disabled={!canSubmit || issueMutation.isPending}
                  onClick={() => issueMutation.mutate()}
                >
                  {issueMutation.isPending ? (
                    <LoaderCircle className="mr-2 h-4 w-4 animate-spin" />
                  ) : null}
                  {isLoan ? "Выдать заём" : "Выдать аванс"}
                </Button>
              </div>
              <Button
                variant="ghost"
                className="mt-2 w-full"
                onClick={() => setDialogOpen(false)}
              >
                Отмена
              </Button>
            </div>
          </div>
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
