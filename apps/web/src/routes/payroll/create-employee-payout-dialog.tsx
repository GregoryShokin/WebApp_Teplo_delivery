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
  createEmployeePayout,
  getCashWallets,
  getDdsArticles,
  getEmployees,
} from "@/lib/api";

function todayInput(): string {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

// Дефолтная статья выплаты — «Зарплата собственника», иначе первая зарплатная, иначе пусто.
const SALARY_ARTICLE_HINT = "зарплат";

export function CreateEmployeePayoutDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [employeeId, setEmployeeId] = useState("");
  const [amount, setAmount] = useState("");
  const [walletId, setWalletId] = useState("");
  const [articleId, setArticleId] = useState("");
  const [payoutDate, setPayoutDate] = useState(todayInput());
  const [note, setNote] = useState("");

  const employeesQuery = useQuery({
    queryKey: ["fab-payout-employees"],
    queryFn: () => getEmployees({ status: "active" }),
    enabled: open,
  });
  const walletsQuery = useQuery({
    queryKey: ["cash-wallets"],
    queryFn: getCashWallets,
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
  const wallets = walletsQuery.data ?? [];
  const articles = useMemo(
    () =>
      (articlesQuery.data ?? []).filter(
        (article) => article.movement_type === "outflow" && article.is_active,
      ),
    [articlesQuery.data],
  );

  useEffect(() => {
    if (open) {
      setEmployeeId("");
      setAmount("");
      setWalletId("");
      setPayoutDate(todayInput());
      setNote("");
    }
  }, [open]);

  // Автоподстановка зарплатной статьи по умолчанию, когда статьи загрузились.
  useEffect(() => {
    if (!open || articleId || articles.length === 0) {
      return;
    }
    const salary =
      articles.find((a) => a.code === "zarplata_sobstvennika") ??
      articles.find((a) => a.name.toLowerCase().includes(SALARY_ARTICLE_HINT));
    if (salary) {
      setArticleId(salary.id);
    }
  }, [open, articles, articleId]);

  const mutation = useMutation({
    mutationFn: () =>
      createEmployeePayout({
        employee_id: employeeId,
        amount: Number(amount),
        wallet_id: walletId,
        payout_date: payoutDate,
        kind: "salary",
        article_id: articleId || null,
        note: note.trim() ? note.trim() : null,
      }),
    onSuccess: async () => {
      toast.success("Выплата проведена");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["dds"] }),
        queryClient.invalidateQueries({ queryKey: ["cashflow"] }),
      ]);
      onOpenChange(false);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось провести выплату"));
    },
  });

  const amountValue = Number(amount);
  const amountValid = Number.isFinite(amountValue) && amountValue > 0;
  const canSubmit =
    Boolean(employeeId) &&
    amountValid &&
    Boolean(walletId) &&
    Boolean(articleId) &&
    Boolean(payoutDate) &&
    !mutation.isPending;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Создать выплату сотруднику</DialogTitle>
          <DialogDescription>
            Разовая выплата наличными или с Сейфа: проводит расход в ДДС по выбранной статье.
            Банковская выплата (через ИП и Сейф) появится позже.
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
                    {wallet.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
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
          <Button disabled={!canSubmit} onClick={() => mutation.mutate()} type="button">
            {mutation.isPending ? (
              <LoaderCircle className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
            ) : null}
            Провести выплату
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
