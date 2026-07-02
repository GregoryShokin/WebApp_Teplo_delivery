import { useMutation, useQueryClient } from "@tanstack/react-query";
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
import { apiErrorMessage, includeOnDemandPayout } from "@/lib/api";

import { formatMoney } from "./runs";

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
  runId,
  employeeId,
  employeeName,
  accrued,
  paid,
  remaining,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  runId: string;
  employeeId: string | null;
  employeeName: string;
  accrued: number;
  paid: number;
  remaining: number;
  onSaved?: () => void;
}) {
  const queryClient = useQueryClient();
  const [amount, setAmount] = useState("");
  const [note, setNote] = useState("");

  // Сброс при открытии: сумма = текущий остаток (но не отрицательная).
  useEffect(() => {
    if (open) {
      setAmount(remaining > 0 ? String(remaining) : "");
      setNote("");
    }
  }, [open, remaining]);

  const mutation = useMutation({
    mutationFn: () =>
      includeOnDemandPayout(runId, {
        employee_id: employeeId ?? "",
        amount: Number(amount),
        note: note.trim() ? note.trim() : null,
      }),
    onSuccess: async () => {
      toast.success("Сумма включена в выплату ведомости");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["payroll-admin-run-lines"] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-admin-run"] }),
      ]);
      onSaved?.();
      onOpenChange(false);
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось включить в выплату"));
    },
  });

  const amountValue = Number(amount);
  const amountValid = Number.isFinite(amountValue) && amountValue > 0;
  const canSubmit = Boolean(employeeId) && amountValid && !mutation.isPending;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Включить в выплату</DialogTitle>
          <DialogDescription>
            {employeeName} — сумма попадёт в «К выплате» этой ведомости и оплатится вместе с ней
            (тем счётом, которым платится вся ведомость). Остаток уменьшится сразу.
          </DialogDescription>
        </DialogHeader>

        <div className="grid grid-cols-3 gap-2">
          <Kpi label="Начислено" value={formatMoney(accrued)} />
          <Kpi label="Выплачено" value={formatMoney(paid)} tone="emerald" />
          <Kpi
            label="Остаток"
            value={formatMoney(remaining)}
            tone={remaining > 0 ? "rose" : undefined}
          />
        </div>

        <div className="space-y-3">
          <Label className="block space-y-1">
            <span className="text-sm">Сумма к выплате, ₽</span>
            <Input
              inputMode="decimal"
              onChange={(event) => setAmount(event.target.value)}
              placeholder="0"
              value={amount}
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
            Включить в выплату
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
