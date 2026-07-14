import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowLeftRight, LoaderCircle } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  apiErrorMessage,
  correctPayrollPayoutWallet,
  getPayrollPayoutCashflows,
  type CashWallet,
  type PayrollPayoutCashflow,
} from "@/lib/api";

import { formatDate, formatMoney } from "./runs";

const CORRECTION_WALLET_CODES = new Set(["cash_safe", "tk_chernikova"]);

type PayrollPayoutWalletCorrectionButtonProps = {
  runId: string;
  wallets: CashWallet[];
  onCorrected: () => Promise<unknown>;
};

export function PayrollPayoutWalletCorrectionButton({
  runId,
  wallets,
  onCorrected,
}: PayrollPayoutWalletCorrectionButtonProps) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [targetWalletCode, setTargetWalletCode] = useState("");
  const [reason, setReason] = useState("");

  const cashflowsQuery = useQuery({
    queryKey: ["payroll-payout-cashflows", runId],
    queryFn: () => getPayrollPayoutCashflows(runId),
    enabled: open,
  });
  const cashflows = useMemo(() => cashflowsQuery.data ?? [], [cashflowsQuery.data]);
  const selectedRows = useMemo(
    () => cashflows.filter((row) => selectedIds.includes(row.id)),
    [cashflows, selectedIds],
  );
  const sourceWalletId = selectedRows[0]?.wallet_id ?? null;
  const sourceWalletName = selectedRows[0]?.wallet_name ?? null;
  const selectedTotal = selectedRows.reduce((total, row) => total + Number(row.amount), 0);
  const targetWallets = wallets.filter(
    (wallet) =>
      CORRECTION_WALLET_CODES.has(wallet.code) &&
      (sourceWalletId === null || wallet.id !== sourceWalletId),
  );

  const reset = () => {
    setSelectedIds([]);
    setTargetWalletCode("");
    setReason("");
  };

  const mutation = useMutation({
    mutationFn: () =>
      correctPayrollPayoutWallet(runId, {
        transaction_ids: selectedIds,
        target_wallet_code: targetWalletCode,
        reason: reason.trim(),
      }),
    onSuccess: async (result) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["payroll-payout-cashflows", runId] }),
        queryClient.invalidateQueries({ queryKey: ["finance-payments"] }),
        queryClient.invalidateQueries({ queryKey: ["dds"] }),
        onCorrected(),
      ]);
      toast.success(
        `${formatMoney(Number(result.total_amount))} перенесено на «${result.target_wallet_name}»`,
      );
      setOpen(false);
      reset();
    },
    onError: (error) => {
      toast.error(apiErrorMessage(error, "Не удалось исправить счёт выплаты"));
    },
  });

  const toggleRow = (row: PayrollPayoutCashflow, checked: boolean) => {
    setTargetWalletCode("");
    setSelectedIds((current) => {
      if (!checked) {
        return current.filter((id) => id !== row.id);
      }
      if (sourceWalletId !== null && row.wallet_id !== sourceWalletId) {
        return [row.id];
      }
      return [...current, row.id];
    });
  };

  const canSubmit =
    selectedIds.length > 0 &&
    targetWalletCode !== "" &&
    reason.trim().length >= 3 &&
    !mutation.isPending;

  return (
    <>
      <Button onClick={() => setOpen(true)} size="sm" type="button" variant="outline">
        <ArrowLeftRight size={15} aria-hidden="true" />
        Исправить счёт выплаты
      </Button>

      <Dialog
        open={open}
        onOpenChange={(nextOpen) => {
          if (mutation.isPending) {
            return;
          }
          setOpen(nextOpen);
          if (!nextOpen) {
            reset();
          }
        }}
      >
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle>Исправить счёт фактической выплаты</DialogTitle>
            <DialogDescription>
              Выберите ошибочные проводки и счёт, с которого деньги были выданы фактически. Выплаты
              сотрудникам не изменятся. Исключённые проводки вернутся в ДДС на правильном счёте, а
              ошибочный зарплатный резерв будет пересчитан.
            </DialogDescription>
          </DialogHeader>

          {cashflowsQuery.isLoading ? (
            <div className="flex min-h-32 items-center justify-center text-muted-foreground">
              <LoaderCircle className="animate-spin" size={20} aria-hidden="true" />
            </div>
          ) : cashflowsQuery.isError ? (
            <div className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
              Не удалось загрузить проводки выплаты.
            </div>
          ) : cashflows.length === 0 ? (
            <div className="rounded-md border bg-muted/30 p-4 text-sm text-muted-foreground">
              У этой ведомости ещё нет проводок фактической выплаты.
            </div>
          ) : (
            <div className="max-h-[360px] overflow-y-auto rounded-md border">
              {cashflows.map((row) => {
                const checked = selectedIds.includes(row.id);
                const anotherSourceSelected =
                  sourceWalletId !== null && row.wallet_id !== sourceWalletId;
                return (
                  <label
                    className="flex cursor-pointer items-start gap-3 border-b p-3 last:border-b-0"
                    key={row.id}
                  >
                    <Checkbox
                      checked={checked}
                      className="mt-0.5"
                      onChange={(event) => toggleRow(row, event.target.checked)}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="flex flex-wrap items-center gap-2">
                        <span className="font-medium">{row.wallet_name}</span>
                        <Badge
                          variant={row.quality_status === "excluded" ? "destructive" : "secondary"}
                        >
                          {row.quality_status === "excluded" ? "Исключена" : "Действует"}
                        </Badge>
                        {anotherSourceSelected ? (
                          <span className="text-xs text-muted-foreground">
                            выбор переключит исходный счёт
                          </span>
                        ) : null}
                      </span>
                      <span className="mt-1 block text-xs text-muted-foreground">
                        {formatDate(row.operation_date)} · {row.article_name ?? "Без статьи"}
                        {row.purpose ? ` · ${row.purpose}` : ""}
                      </span>
                    </span>
                    <span className="font-medium tabular-nums">
                      {formatMoney(Number(row.amount))}
                    </span>
                  </label>
                );
              })}
            </div>
          )}

          {selectedRows.length > 0 ? (
            <div className="rounded-md border bg-muted/30 p-3 text-sm">
              Выбрано с «{sourceWalletName}»: <strong>{formatMoney(selectedTotal)}</strong>
            </div>
          ) : null}

          <div className="grid gap-2">
            <Label htmlFor="payroll-correction-target">Фактический счёт выдачи</Label>
            <select
              className="h-9 w-full rounded-md border border-input bg-background px-2 text-sm disabled:opacity-50"
              disabled={selectedIds.length === 0 || mutation.isPending}
              id="payroll-correction-target"
              onChange={(event) => setTargetWalletCode(event.target.value)}
              value={targetWalletCode}
            >
              <option value="">— выберите счёт —</option>
              {targetWallets.map((wallet) => (
                <option key={wallet.id} value={wallet.code}>
                  {wallet.name}
                </option>
              ))}
            </select>
          </div>

          <div className="grid gap-2">
            <Label htmlFor="payroll-correction-reason">Причина исправления</Label>
            <Textarea
              disabled={mutation.isPending}
              id="payroll-correction-reason"
              onChange={(event) => setReason(event.target.value)}
              placeholder="Например: менеджер выбрал Сейф вместо торговой кассы"
              value={reason}
            />
          </div>

          <div className="flex items-start gap-2 rounded-md border border-amber-400/50 bg-amber-50 p-3 text-xs text-amber-950 dark:bg-amber-950/20 dark:text-amber-100">
            <AlertTriangle className="mt-0.5 shrink-0" size={16} aria-hidden="true" />
            Корректировка записывается в аудит. Она не отменяет зарплату и не меняет суммы,
            полученные сотрудниками.
          </div>

          <DialogFooter>
            <Button
              disabled={mutation.isPending}
              onClick={() => {
                setOpen(false);
                reset();
              }}
              type="button"
              variant="outline"
            >
              Отмена
            </Button>
            <Button disabled={!canSubmit} onClick={() => mutation.mutate()} type="button">
              {mutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : (
                <ArrowLeftRight size={16} aria-hidden="true" />
              )}
              Перенести проводки
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
