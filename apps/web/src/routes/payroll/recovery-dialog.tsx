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
import { Switch } from "@/components/ui/switch";
import {
  apiErrorMessage,
  getEmployeeRecoveries,
  setPayrollRecoveryOverrides,
  type RecoveryLine,
} from "@/lib/api";

import { formatDate, formatMoney } from "./runs";

function clampAmount(value: number, max: number): number {
  if (Number.isNaN(value) || value < 0) {
    return 0;
  }
  return value > max ? max : Math.round(value);
}

function Kpi({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "rose" | "emerald";
}) {
  const color =
    tone === "rose" ? "text-rose-700" : tone === "emerald" ? "text-emerald-700" : "";
  return (
    <div className="rounded-md bg-muted/50 p-2.5">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={`mt-0.5 text-lg font-medium tabular-nums ${color}`}>{value}</div>
    </div>
  );
}

export function RecoveryDialog({
  runId,
  employeeId,
  open,
  onOpenChange,
  onSaved,
}: {
  runId: string;
  employeeId: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSaved?: () => void;
}) {
  const queryClient = useQueryClient();
  const detailQuery = useQuery({
    queryKey: ["employee-recoveries", runId, employeeId],
    queryFn: () => getEmployeeRecoveries(runId, employeeId as string),
    enabled: open && Boolean(employeeId),
  });
  const detail = detailQuery.data;

  const [amounts, setAmounts] = useState<Record<string, number>>({});

  useEffect(() => {
    if (!detail) {
      return;
    }
    const next: Record<string, number> = {};
    for (const item of detail.items) {
      next[item.advance_id] = Math.round(item.current_recovery);
    }
    setAmounts(next);
  }, [detail]);

  const saveMutation = useMutation({
    mutationFn: () =>
      setPayrollRecoveryOverrides(
        runId,
        (detail?.items ?? []).map((item) => ({
          advance_id: item.advance_id,
          amount: amounts[item.advance_id] ?? 0,
        })),
      ),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["payroll-admin-run", runId] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-admin-run-lines", runId] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-run", runId] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-run-lines", runId] }),
        queryClient.invalidateQueries({ queryKey: ["payroll-advances", "all"] }),
      ]);
      toast.success("Удержания обновлены");
      onSaved?.();
      onOpenChange(false);
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить удержания")),
  });

  const totalHold = detail
    ? detail.items.reduce((sum, item) => sum + (amounts[item.advance_id] ?? 0), 0)
    : 0;
  const net = detail ? Math.max(0, detail.accrued - totalHold) : 0;

  const setAmount = (item: RecoveryLine, value: number) => {
    setAmounts((prev) => ({
      ...prev,
      [item.advance_id]: clampAmount(value, item.max_amount),
    }));
  };

  const loans = detail?.items.filter((item) => item.kind === "loan") ?? [];
  const advances = detail?.items.filter((item) => item.kind !== "loan") ?? [];

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Удержания{detail ? ` — ${detail.employee_name}` : ""}</DialogTitle>
          <DialogDescription>
            {detail
              ? `${detail.role ?? ""} · выплата ${formatDate(detail.payroll_date)}`
              : "Загрузка…"}
          </DialogDescription>
        </DialogHeader>

        {detailQuery.isLoading ? (
          <div className="flex justify-center py-10">
            <LoaderCircle className="animate-spin text-muted-foreground" size={20} aria-hidden="true" />
          </div>
        ) : detail ? (
          <div className="space-y-4">
            <div className="grid grid-cols-3 gap-2">
              <Kpi label="Начислено" value={formatMoney(detail.accrued)} />
              <Kpi label="Удержание" value={`−${formatMoney(totalHold)}`} tone="rose" />
              <Kpi label="К выплате" value={formatMoney(net)} tone="emerald" />
            </div>

            {loans.length > 0 ? (
              <div className="space-y-2">
                <div className="text-xs text-muted-foreground">Займы</div>
                {loans.map((item) => {
                  const current = amounts[item.advance_id] ?? 0;
                  const left = item.max_amount - current;
                  return (
                    <div key={item.advance_id} className="rounded-md border p-3">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div>
                          <div className="text-sm font-medium">
                            Заём · выдан {formatDate(item.issued_on)}
                          </div>
                          <div className="text-xs text-muted-foreground">
                            остаток {formatMoney(item.outstanding)}
                            {left <= 0
                              ? " · закроется"
                              : ` · останется ${formatMoney(left)}`}
                          </div>
                        </div>
                        <div className="flex items-center gap-2">
                          <Input
                            type="number"
                            min={0}
                            max={item.max_amount}
                            step={500}
                            value={String(current)}
                            onChange={(event) => setAmount(item, Number(event.target.value))}
                            className="w-28 text-right tabular-nums"
                            aria-label="Сумма удержания займа"
                          />
                          <span className="text-muted-foreground">₽</span>
                        </div>
                      </div>
                      <div className="mt-2 flex flex-wrap gap-2">
                        <Button
                          onClick={() => setAmount(item, item.default_installment)}
                          size="sm"
                          type="button"
                          variant="outline"
                        >
                          Доля {formatMoney(item.default_installment)}
                        </Button>
                        <Button
                          onClick={() => setAmount(item, item.max_amount)}
                          size="sm"
                          type="button"
                          variant="outline"
                        >
                          Остаток {formatMoney(item.max_amount)}
                        </Button>
                        <Button
                          onClick={() => setAmount(item, 0)}
                          size="sm"
                          type="button"
                          variant="outline"
                        >
                          Отложить
                        </Button>
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : null}

            {advances.length > 0 ? (
              <div className="space-y-2">
                <div className="text-xs text-muted-foreground">Авансы</div>
                {advances.map((item) => {
                  const on = (amounts[item.advance_id] ?? 0) > 0;
                  return (
                    <div
                      key={item.advance_id}
                      className="flex items-center justify-between rounded-md border p-3"
                    >
                      <div>
                        <div className="text-sm font-medium">Аванс · {formatDate(item.issued_on)}</div>
                        <div className="text-xs text-muted-foreground">
                          {formatMoney(item.outstanding)} · разово
                        </div>
                      </div>
                      <div className="flex items-center gap-2">
                        <span className="text-sm tabular-nums">
                          {on ? formatMoney(amounts[item.advance_id] ?? 0) : "—"}
                        </span>
                        <Switch
                          checked={on}
                          onCheckedChange={(value) => setAmount(item, value ? item.max_amount : 0)}
                        />
                        <span className="w-16 text-xs text-muted-foreground">
                          {on ? "Удержать" : "Отложить"}
                        </span>
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : null}

            {loans.length === 0 && advances.length === 0 ? (
              <div className="rounded-md border bg-muted/40 p-4 text-sm text-muted-foreground">
                Нет активных авансов или займов к удержанию в этом периоде.
              </div>
            ) : null}
          </div>
        ) : (
          <div className="py-6 text-sm text-muted-foreground">
            Не удалось загрузить детализацию удержаний.
          </div>
        )}

        <DialogFooter>
          <Button
            disabled={saveMutation.isPending}
            onClick={() => onOpenChange(false)}
            type="button"
            variant="outline"
          >
            Отмена
          </Button>
          <Button
            disabled={saveMutation.isPending || !detail || detail.items.length === 0}
            onClick={() => saveMutation.mutate()}
            type="button"
          >
            {saveMutation.isPending ? (
              <LoaderCircle className="mr-1 animate-spin" size={15} aria-hidden="true" />
            ) : null}
            Сохранить
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
