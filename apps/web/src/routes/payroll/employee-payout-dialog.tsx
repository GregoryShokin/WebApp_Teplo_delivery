import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle } from "lucide-react";
import { useEffect, useState } from "react";
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
  type EmployeePayoutKind,
} from "@/lib/api";

import { formatMoney } from "./runs";

function todayInput(): string {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

function Kpi({ label, value, tone }: { label: string; value: string; tone?: "rose" | "emerald" }) {
  const color = tone === "rose" ? "text-rose-700" : tone === "emerald" ? "text-emerald-700" : "";
  return (
    <div className="rounded-md bg-muted/50 p-2.5">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={`mt-0.5 text-lg font-medium tabular-nums ${color}`}>{value}</div>
    </div>
  );
}

export function EmployeePayoutDialog({
  open,
  onOpenChange,
  employeeId,
  employeeName,
  accrued,
  paid,
  debt,
  kind = "owner_salary",
  onSaved,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  employeeId: string | null;
  employeeName: string;
  accrued: number;
  paid: number;
  debt: number;
  kind?: EmployeePayoutKind;
  onSaved?: () => void;
}) {
  const queryClient = useQueryClient();
  const [amount, setAmount] = useState("");
  const [walletId, setWalletId] = useState("");
  const [payoutDate, setPayoutDate] = useState(todayInput());
  const [note, setNote] = useState("");

  const walletsQuery = useQuery({
    queryKey: ["cash-wallets"],
    queryFn: getCashWallets,
    enabled: open,
  });
  const wallets = walletsQuery.data ?? [];

  // Сброс формы при каждом открытии: сумма = текущий долг (но не отрицательная).
  useEffect(() => {
    if (open) {
      setAmount(debt > 0 ? String(debt) : "");
      setPayoutDate(todayInput());
      setNote("");
      setWalletId("");
    }
  }, [open, debt]);

  const mutation = useMutation({
    mutationFn: () =>
      createEmployeePayout({
        employee_id: employeeId ?? "",
        amount: Number(amount),
        wallet_id: walletId,
        payout_date: payoutDate,
        kind,
        note: note.trim() ? note.trim() : null,
      }),
    onSuccess: async () => {
      toast.success("Выплата проведена");
      await queryClient.invalidateQueries({ queryKey: ["payroll-admin-run-lines"] });
      await queryClient.invalidateQueries({ queryKey: ["payroll-admin-run"] });
      onSaved?.();
      onOpenChange(false);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось провести выплату"));
    },
  });

  const amountValue = Number(amount);
  const amountValid = Number.isFinite(amountValue) && amountValue > 0;
  const canSubmit =
    Boolean(employeeId) && amountValid && Boolean(walletId) && Boolean(payoutDate) && !mutation.isPending;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Включить в выплату</DialogTitle>
          <DialogDescription>
            {employeeName} — выплата ЗП «по востребованию» наличными или с Сейфа. Уменьшает
            накопленный долг и проводит расход в ДДС.
          </DialogDescription>
        </DialogHeader>

        <div className="grid grid-cols-3 gap-2">
          <Kpi label="Начислено" value={formatMoney(accrued)} />
          <Kpi label="Выплачено" value={formatMoney(paid)} tone="emerald" />
          <Kpi label="Долг" value={formatMoney(debt)} tone={debt > 0 ? "rose" : undefined} />
        </div>

        <div className="space-y-3">
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
