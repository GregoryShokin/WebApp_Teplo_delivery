import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
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
  confirmEmployeePayout,
  createEmployeePayout,
  getDdsBankOperations,
  getNewPaymentContext,
  type EmployeePayout,
} from "@/lib/api";

function todayInput(): string {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

function daysAgoInput(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${month}-${day}`;
}

function normalizeAmount(value: string): string {
  return value.trim().replace(",", ".");
}

/**
 * Разовая выплата сотруднику (оклад «по востребованию»): отдельный поток со своим
 * двухшаговым подтверждением — создать черновик, затем привязать операцию из выписки.
 * Вынесен из «Нового платежа» (тот стал построчным конструктором обычных платежей).
 */
export function EmployeePayoutDialog({
  open,
  onOpenChange,
  presetArticleCode = "zarplata_administrativnogo_personala",
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  presetArticleCode?: string | null;
}) {
  const queryClient = useQueryClient();

  const [articleId, setArticleId] = useState("");
  const [walletId, setWalletId] = useState("");
  const [employeeId, setEmployeeId] = useState("");
  const [amount, setAmount] = useState("");
  const [payoutDate, setPayoutDate] = useState(todayInput());
  const [note, setNote] = useState("");
  const [step, setStep] = useState<"form" | "link">("form");
  const [pendingPayout, setPendingPayout] = useState<EmployeePayout | null>(null);
  const [operationId, setOperationId] = useState("");

  const contextQuery = useQuery({
    queryKey: ["new-payment", "context"],
    queryFn: getNewPaymentContext,
    enabled: open,
  });
  const articles = useMemo(
    () => (contextQuery.data?.articles ?? []).filter((a) => a.flow === "employee_payout"),
    [contextQuery.data],
  );
  const wallets = useMemo(() => contextQuery.data?.wallets ?? [], [contextQuery.data]);
  const employees = useMemo(() => contextQuery.data?.employees ?? [], [contextQuery.data]);

  const tbankWallet = wallets.find((wallet) => wallet.bank_code === "tbank") ?? null;
  const selectedWallet = wallets.find((wallet) => wallet.id === walletId) ?? null;

  useEffect(() => {
    if (open) {
      setEmployeeId("");
      setAmount("");
      setPayoutDate(todayInput());
      setNote("");
      setStep("form");
      setPendingPayout(null);
      setOperationId("");
    }
  }, [open]);

  // Статья по умолчанию: пресет, иначе первая доступная зарплатная.
  useEffect(() => {
    if (!open || articleId || articles.length === 0) {
      return;
    }
    const preset = presetArticleCode
      ? articles.find((a) => a.code === presetArticleCode)
      : null;
    setArticleId((preset ?? articles[0]).id);
  }, [open, presetArticleCode, articles, articleId]);

  useEffect(() => {
    if (open && !walletId && tbankWallet) {
      setWalletId(tbankWallet.id);
    }
  }, [open, walletId, tbankWallet]);

  async function invalidate() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["dds"] }),
      queryClient.invalidateQueries({ queryKey: ["cashflow"] }),
      queryClient.invalidateQueries({ queryKey: ["payroll"] }),
      queryClient.invalidateQueries({ queryKey: ["new-payment"] }),
    ]);
  }

  const createMutation = useMutation({
    mutationFn: () =>
      createEmployeePayout({
        employee_id: employeeId,
        amount: Number(normalizeAmount(amount)),
        wallet_id: walletId,
        payout_date: payoutDate,
        kind: "owner_salary",
        article_id: articleId,
        note: note.trim() ? note.trim() : null,
      }),
    onSuccess: async (payout) => {
      await invalidate();
      if (payout.status === "pending") {
        setPendingPayout(payout);
        setStep("link");
        toast.success("Черновик платежа создан — привяжите операцию из выписки");
        return;
      }
      if (payout.status === "failed") {
        toast.error("Банк отклонил черновик платежа");
        onOpenChange(false);
        return;
      }
      toast.success("Выплата проведена");
      onOpenChange(false);
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось создать выплату")),
  });

  const operationsQuery = useQuery({
    queryKey: ["new-payment", "payout-operations"],
    queryFn: () => getDdsBankOperations({ from: daysAgoInput(45), to: todayInput(), limit: 100 }),
    enabled: open && step === "link",
  });
  const operations = useMemo(
    () =>
      (operationsQuery.data?.items ?? []).filter(
        (op) => op.direction === "out" && op.cashflow_transaction_id === null,
      ),
    [operationsQuery.data],
  );
  const confirmMutation = useMutation({
    mutationFn: () => confirmEmployeePayout(pendingPayout?.id ?? "", operationId),
    onSuccess: async () => {
      await invalidate();
      toast.success("Выплата подтверждена и привязана к операции");
      onOpenChange(false);
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось подтвердить выплату")),
  });

  const numericAmount = Number(normalizeAmount(amount));
  const canSubmit =
    Boolean(articleId) &&
    Boolean(walletId) &&
    Boolean(employeeId) &&
    Boolean(payoutDate) &&
    numericAmount > 0 &&
    !createMutation.isPending;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        {step === "form" ? (
          <>
            <DialogHeader>
              <DialogTitle>Выплата сотруднику</DialogTitle>
              <DialogDescription>
                Разовая выплата сотруднику с окладом «по востребованию». Черновик уходит в
                банк, подтверждение — привязкой операции из выписки.
              </DialogDescription>
            </DialogHeader>

            <div className="space-y-3">
              <Label className="block space-y-1">
                <span className="text-sm">Счёт списания</span>
                <Select onValueChange={setWalletId} value={walletId}>
                  <SelectTrigger>
                    <SelectValue placeholder="Выберите счёт" />
                  </SelectTrigger>
                  <SelectContent>
                    {wallets.map((wallet) => (
                      <SelectItem key={wallet.id} value={wallet.id}>
                        {wallet.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Label>

              <Label className="block space-y-1">
                <span className="text-sm">Статья ДДС</span>
                <Select onValueChange={setArticleId} value={articleId}>
                  <SelectTrigger>
                    <SelectValue placeholder="Выберите статью" />
                  </SelectTrigger>
                  <SelectContent>
                    {articles.map((article) => (
                      <SelectItem key={article.id} value={article.id}>
                        {article.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Label>

              <Label className="block space-y-1">
                <span className="text-sm">Сотрудник</span>
                <Select onValueChange={setEmployeeId} value={employeeId}>
                  <SelectTrigger>
                    <SelectValue placeholder="Выберите сотрудника" />
                  </SelectTrigger>
                  <SelectContent>
                    {employees.map((employee) => (
                      <SelectItem
                        disabled={!employee.on_demand}
                        key={employee.id}
                        value={employee.id}
                      >
                        {employee.full_name}
                        {!employee.on_demand ? " — доступны аванс или займ" : ""}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <span className="text-xs text-muted-foreground">
                  Выплата доступна сотрудникам с окладом «по востребованию».
                </span>
              </Label>

              <div className="grid grid-cols-2 gap-3">
                <Label className="block space-y-1">
                  <span className="text-sm">Сумма, ₽</span>
                  <Input
                    inputMode="decimal"
                    onChange={(event) => setAmount(event.target.value)}
                    placeholder="0"
                    value={amount}
                  />
                </Label>
                <Label className="block space-y-1">
                  <span className="text-sm">Дата выплаты</span>
                  <Input
                    onChange={(event) => setPayoutDate(event.target.value)}
                    type="date"
                    value={payoutDate}
                  />
                </Label>
              </div>

              <Label className="block space-y-1">
                <span className="text-sm">Комментарий</span>
                <Input
                  maxLength={210}
                  onChange={(event) => setNote(event.target.value)}
                  placeholder="Необязательно"
                  value={note}
                />
              </Label>

              <div className="rounded-md border bg-muted/40 p-3 text-sm text-muted-foreground">
                Черновик на карту сотрудника. После оплаты выплата свяжется с зарплатной
                ведомостью и уменьшит сумму к выдаче.
                {selectedWallet && selectedWallet.bank_code !== "tbank" ? (
                  <span className="mt-1 block text-xs">
                    Счёт не в Т-Банке: черновик в банке не создаётся — подтвердите выплату
                    привязкой операции из выписки.
                  </span>
                ) : null}
              </div>
            </div>

            <DialogFooter>
              <Button onClick={() => onOpenChange(false)} type="button" variant="outline">
                Отмена
              </Button>
              <Button disabled={!canSubmit} onClick={() => createMutation.mutate()} type="button">
                {createMutation.isPending ? (
                  <LoaderCircle className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
                ) : null}
                Создать выплату
              </Button>
            </DialogFooter>
          </>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>Привязать операцию</DialogTitle>
              <DialogDescription>
                Черновик отправлен в банк. Выберите исходящую операцию из выписки, чтобы
                подтвердить выплату (заведёт перевод на Сейф с резервом).
              </DialogDescription>
            </DialogHeader>

            <div className="max-h-[320px] space-y-2 overflow-y-auto">
              {operationsQuery.isLoading ? (
                <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
                  <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden="true" />
                  Загрузка операций…
                </div>
              ) : operations.length === 0 ? (
                <div className="py-6 text-sm text-muted-foreground">
                  Нет несопоставленных исходящих операций за последние 45 дней. Операция
                  появится после импорта выписки — привяжите позже.
                </div>
              ) : (
                operations.map((op) => (
                  <button
                    className={`w-full rounded-md border p-2 text-left text-sm transition hover:bg-muted/50 ${
                      operationId === op.id ? "border-primary bg-muted/50" : "border-border"
                    }`}
                    key={op.id}
                    onClick={() => setOperationId(op.id)}
                    type="button"
                  >
                    <div className="flex justify-between gap-2">
                      <span className="font-medium tabular-nums">{op.amount} ₽</span>
                      <span className="text-muted-foreground">{op.operation_date}</span>
                    </div>
                    <div className="truncate text-xs text-muted-foreground">
                      {op.counterparty_name_raw || op.payment_purpose || "—"}
                    </div>
                  </button>
                ))
              )}
            </div>

            <DialogFooter>
              <Button onClick={() => onOpenChange(false)} type="button" variant="outline">
                Позже
              </Button>
              <Button
                disabled={!operationId || confirmMutation.isPending}
                onClick={() => confirmMutation.mutate()}
                type="button"
              >
                {confirmMutation.isPending ? (
                  <LoaderCircle className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
                ) : null}
                Подтвердить выплату
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
