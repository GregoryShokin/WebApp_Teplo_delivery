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
  getDdsArticles,
  getDdsBankOperations,
  getDdsWallets,
  getOnDemandEmployees,
  type EmployeePayout,
  type WalletRead,
} from "@/lib/api";

const BANK_WALLET_TYPES = new Set(["bank", "bank_account"]);
// Допустимые счета-источники выплаты: Сейф, Сбербанк, Тинькофф рублёвый, Торговая касса Черникова.
const PAYOUT_WALLET_CODES = new Set(["cash_safe", "sber_main", "tbank_main", "tk_chernikova"]);
const DEFAULT_ARTICLE_CODE = "zarplata_administrativnogo_personala";

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

function walletLabel(wallet: WalletRead): string {
  return BANK_WALLET_TYPES.has(wallet.type) ? `${wallet.name} · банк` : wallet.name;
}

export function CreateEmployeePayoutDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [step, setStep] = useState<"form" | "link">("form");
  const [pending, setPending] = useState<EmployeePayout | null>(null);
  const [operationId, setOperationId] = useState("");

  const [employeeId, setEmployeeId] = useState("");
  const [amount, setAmount] = useState("");
  const [walletId, setWalletId] = useState("");
  const [articleId, setArticleId] = useState("");
  const [payoutDate, setPayoutDate] = useState(todayInput());
  const [note, setNote] = useState("");

  const employeesQuery = useQuery({
    queryKey: ["fab-on-demand-employees"],
    queryFn: getOnDemandEmployees,
    enabled: open,
  });
  const walletsQuery = useQuery({
    queryKey: ["dds-wallets"],
    queryFn: getDdsWallets,
    enabled: open,
  });
  const articlesQuery = useQuery({
    queryKey: ["dds-articles"],
    queryFn: getDdsArticles,
    enabled: open,
  });

  const employees = useMemo(
    () =>
      (employeesQuery.data ?? [])
        .slice()
        .sort((a, b) => a.full_name.localeCompare(b.full_name, "ru")),
    [employeesQuery.data],
  );
  const wallets = useMemo(
    () =>
      (walletsQuery.data ?? []).filter(
        (wallet) => wallet.status === "active" && PAYOUT_WALLET_CODES.has(wallet.code),
      ),
    [walletsQuery.data],
  );
  const articles = useMemo(
    () =>
      (articlesQuery.data ?? []).filter(
        (article) => article.movement_type === "outflow" && article.is_active,
      ),
    [articlesQuery.data],
  );
  const selectedWallet = wallets.find((wallet) => wallet.id === walletId) ?? null;

  useEffect(() => {
    if (open) {
      setStep("form");
      setPending(null);
      setOperationId("");
      setEmployeeId("");
      setAmount("");
      setWalletId("");
      setPayoutDate(todayInput());
      setNote("");
    }
  }, [open]);

  useEffect(() => {
    if (!open || articleId || articles.length === 0) {
      return;
    }
    const salary = articles.find((a) => a.code === DEFAULT_ARTICLE_CODE);
    if (salary) {
      setArticleId(salary.id);
    }
  }, [open, articles, articleId]);

  const createMutation = useMutation({
    mutationFn: () =>
      createEmployeePayout({
        employee_id: employeeId,
        amount: Number(amount),
        wallet_id: walletId,
        payout_date: payoutDate,
        kind: "owner_salary",
        article_id: articleId || null,
        note: note.trim() ? note.trim() : null,
      }),
    onSuccess: async (payout) => {
      await invalidate();
      if (payout.status === "pending") {
        // Банковская выплата: черновик создан, переходим к привязке операции.
        setPending(payout);
        setStep("link");
        toast.success("Черновик платежа создан — привяжите операцию из выписки");
      } else if (payout.status === "failed") {
        toast.error("Банк отклонил черновик платежа");
        onOpenChange(false);
      } else {
        toast.success("Выплата проведена");
        onOpenChange(false);
      }
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось создать выплату"));
    },
  });

  const operationsQuery = useQuery({
    queryKey: ["fab-payout-operations"],
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
    mutationFn: () => confirmEmployeePayout(pending?.id ?? "", operationId),
    onSuccess: async () => {
      await invalidate();
      toast.success("Выплата подтверждена и привязана к операции");
      onOpenChange(false);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось подтвердить выплату"));
    },
  });

  async function invalidate() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["dds"] }),
      queryClient.invalidateQueries({ queryKey: ["cashflow"] }),
    ]);
  }

  const amountValue = Number(amount);
  const amountValid = Number.isFinite(amountValue) && amountValue > 0;
  const canCreate =
    Boolean(employeeId) &&
    amountValid &&
    Boolean(walletId) &&
    Boolean(articleId) &&
    Boolean(payoutDate) &&
    !createMutation.isPending;
  const isBank = selectedWallet !== null && BANK_WALLET_TYPES.has(selectedWallet.type);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        {step === "form" ? (
          <>
            <DialogHeader>
              <DialogTitle>Создать выплату сотруднику</DialogTitle>
              <DialogDescription>
                Наличная/сейфовая выплата проводится сразу. Банковский счёт создаёт черновик
                по реквизитам ИП — затем привяжите операцию из выписки.
              </DialogDescription>
            </DialogHeader>

            <div className="space-y-3">
              <Label className="block space-y-1">
                <span className="text-sm">Сотрудник</span>
                <Select onValueChange={setEmployeeId} value={employeeId}>
                  <SelectTrigger>
                    <SelectValue placeholder="Выберите сотрудника" />
                  </SelectTrigger>
                  <SelectContent>
                    {employees.map((employee) => (
                      <SelectItem key={employee.id} value={employee.id}>
                        {employee.full_name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Label>

              <Label className="block space-y-1">
                <span className="text-sm">Сумма выплаты, ₽</span>
                <Input
                  inputMode="decimal"
                  onChange={(event) => setAmount(event.target.value)}
                  placeholder="0"
                  value={amount}
                />
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
                <span className="text-sm">Счёт-источник</span>
                <Select onValueChange={setWalletId} value={walletId}>
                  <SelectTrigger>
                    <SelectValue placeholder="Выберите счёт" />
                  </SelectTrigger>
                  <SelectContent>
                    {wallets.map((wallet) => (
                      <SelectItem key={wallet.id} value={wallet.id}>
                        {walletLabel(wallet)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                {isBank ? (
                  <span className="text-xs text-muted-foreground">
                    Банк: создастся черновик по реквизитам ИП + перевод на Сейф с резервом.
                  </span>
                ) : null}
              </Label>

              <Label className="block space-y-1">
                <span className="text-sm">Дата выплаты</span>
                <Input
                  onChange={(event) => setPayoutDate(event.target.value)}
                  type="date"
                  value={payoutDate}
                />
              </Label>

              <Label className="block space-y-1">
                <span className="text-sm">Комментарий (необязательно)</span>
                <Input
                  onChange={(event) => setNote(event.target.value)}
                  placeholder="Назначение выплаты"
                  value={note}
                />
              </Label>
            </div>

            <DialogFooter>
              <Button onClick={() => onOpenChange(false)} type="button" variant="outline">
                Отмена
              </Button>
              <Button disabled={!canCreate} onClick={() => createMutation.mutate()} type="button">
                {createMutation.isPending ? (
                  <LoaderCircle className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
                ) : null}
                {isBank ? "Создать черновик" : "Провести выплату"}
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
