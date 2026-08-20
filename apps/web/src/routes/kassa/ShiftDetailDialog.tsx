import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, LoaderCircle } from "lucide-react";
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { apiErrorMessage } from "@/lib/api";
import { cn } from "@/lib/utils";
import { formatDateTime, formatDdsMoney } from "@/routes/dds/shared";
import { getKassaShift, waiveKassaShiftPenalty } from "@/routes/kassa/api";
import {
  PayoutCategoryBadge,
  ShiftPenaltyBadge,
  ShiftPostedBadge,
  ShiftFloatBadge,
} from "@/routes/kassa/shared";

type ShiftDetailDialogProps = {
  shiftId: string | null;
  canWaive: boolean;
  onClose: () => void;
};

function formatPercent(value: number | null | undefined): string {
  if (value == null) {
    return "—";
  }
  return `${value.toLocaleString("ru-RU", { maximumFractionDigits: 2 })} %`;
}

export function ShiftDetailDialog({ shiftId, canWaive, onClose }: ShiftDetailDialogProps) {
  const queryClient = useQueryClient();
  const open = shiftId !== null;

  const shiftQuery = useQuery({
    queryKey: ["kassa", "shift", shiftId],
    queryFn: () => getKassaShift(shiftId as string),
    enabled: open,
  });
  const shift = shiftQuery.data;

  const waiveMutation = useMutation({
    mutationFn: () => waiveKassaShiftPenalty(shiftId as string),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["kassa", "shift", shiftId] }),
        queryClient.invalidateQueries({ queryKey: ["kassa", "shifts"] }),
      ]);
      toast.success("Авто-штраф отменён");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось отменить штраф")),
  });

  const activePenalties = shift?.penalties.filter((penalty) => penalty.status === "active") ?? [];
  const hasActivePenalty = activePenalties.length > 0;

  return (
    <Dialog open={open} onOpenChange={(next) => (!next ? onClose() : undefined)}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>
            Смена № {shift?.session_number ?? "—"}
            {shift ? (
              <span className="ml-2 inline-flex gap-2 align-middle">
                <ShiftPostedBadge posted={shift.posted} />
                <ShiftFloatBadge status={shift.float_status} />
              </span>
            ) : null}
          </DialogTitle>
          <DialogDescription>
            {shift?.open_date ? `Открыта ${formatDateTime(shift.open_date)}` : "Детали смены"}
            {shift?.close_date ? ` · закрыта ${formatDateTime(shift.close_date)}` : ""}
          </DialogDescription>
        </DialogHeader>

        {shiftQuery.isLoading || !shift ? (
          <div className="flex items-center justify-center py-10 text-sm text-muted-foreground">
            <LoaderCircle className="mr-2 animate-spin" size={16} aria-hidden="true" /> Загрузка…
          </div>
        ) : (
          <div className="space-y-5">
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              <Metric label="Наличная выручка" value={formatDdsMoney(shift.cash_sales)} />
              <Metric label="Вся выручка смены" value={formatDdsMoney(shift.total_sales)} />
              <Metric label="Безнал" value={formatDdsMoney(shift.sales_card)} />
              <Metric label="Изъятия / инкассация" value={formatDdsMoney(shift.pay_out)} />
              <Metric label="Остаток в кассе" value={formatDdsMoney(shift.cash_remain)} />
              <Metric
                label="Сверх нормы размена"
                value={formatDdsMoney(shift.cash_over_norm)}
                warn={(shift.float_status ?? "ok") !== "ok"}
              />
              <Metric
                label="Расхождение кассы"
                value={formatDdsMoney(shift.real_cash_diff)}
                warn={(shift.real_cash_diff ?? 0) > 0}
              />
            </div>

            {shift.float_status === "missing" || shift.float_status === "partial" ? (
              <div className="flex items-start gap-3 rounded-md border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800">
                <AlertTriangle size={18} aria-hidden="true" className="mt-0.5 shrink-0" />
                <div>
                  <span className="font-medium">
                    {shift.float_status === "missing"
                      ? "Инкассацию в этой смене не проводили."
                      : "Инкассировали не всю наличку."}
                  </span>{" "}
                  В ящике осталось {formatDdsMoney(shift.cash_over_norm)} сверх нормы размена (
                  {formatDdsMoney(shift.cash_float_norm)}) — при пороге{" "}
                  {formatDdsMoney(shift.cash_float_threshold)}. Деньги доедут в Главную кассу
                  следующей инкассацией, и приход в ДДС встанет датой ТОЙ смены.
                </div>
              </div>
            ) : null}

            {shift.float_status === "short" ? (
              <div className="flex items-start gap-3 rounded-md border border-sky-200 bg-sky-50 p-4 text-sm text-sky-900">
                <AlertTriangle size={18} aria-hidden="true" className="mt-0.5 shrink-0" />
                <div>
                  <span className="font-medium">Размена в кассе не хватает.</span> Смена закрылась
                  с {formatDdsMoney(shift.cash_remain)} при минимуме{" "}
                  {formatDdsMoney(shift.cash_float_min)} — с этой суммой касса откроется на
                  следующий день, и сдачу давать будет нечем. Смотрите изъятия смены: наличку
                  выбрали инкассацией, курьерами или партнёром, а на размен не оставили.
                </div>
              </div>
            ) : null}

            <div className="rounded-lg border p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="text-sm font-medium">Недостача и штраф</div>
                <ShiftPenaltyBadge status={shift.penalty_status} />
              </div>
              <div className="mt-3 grid grid-cols-2 gap-3 text-sm sm:grid-cols-3">
                <Metric label="Недостача, % выручки" value={formatPercent(shift.shortage_pct_of_revenue)} />
                <Metric label="Порог" value={formatPercent(shift.shortage_threshold_pct)} />
                <Metric label="Порог в рублях" value={formatDdsMoney(shift.shortage_threshold_amount)} />
              </div>
              {shift.penalty_review_reason ? (
                <p className="mt-3 rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-800">
                  {shift.penalty_review_reason}
                </p>
              ) : null}

              {shift.penalties.length > 0 ? (
                <div className="mt-4 rounded-md border">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Кассир</TableHead>
                        <TableHead className="text-right">Штраф</TableHead>
                        <TableHead>Статус</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {shift.penalties.map((penalty) => (
                        <TableRow key={penalty.id}>
                          <TableCell className="font-medium">{penalty.employee_full_name}</TableCell>
                          <TableCell className="text-right tabular-nums">
                            {formatDdsMoney(penalty.amount)}
                          </TableCell>
                          <TableCell>
                            <span
                              className={cn(
                                "text-sm",
                                penalty.status === "waived"
                                  ? "text-muted-foreground line-through"
                                  : "text-foreground",
                              )}
                            >
                              {penalty.status === "waived" ? "Отменён" : "Действует"}
                            </span>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              ) : (
                <p className="mt-3 text-sm text-muted-foreground">
                  Штрафов по этой смене нет.
                </p>
              )}
            </div>

            {shift.payouts.length > 0 ? (
              <div className="rounded-lg border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Изъятие</TableHead>
                      <TableHead>Счёт</TableHead>
                      <TableHead className="text-right">Сумма</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {shift.payouts.map((payout) => (
                      <TableRow key={payout.id}>
                        <TableCell>
                          <PayoutCategoryBadge category={payout.category} />
                        </TableCell>
                        <TableCell className="text-sm text-muted-foreground">
                          {payout.account_name ?? "—"}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {formatDdsMoney(payout.amount)}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            ) : null}
          </div>
        )}

        <DialogFooter className="gap-2">
          {canWaive && hasActivePenalty ? (
            <Button
              variant="destructive"
              disabled={waiveMutation.isPending}
              onClick={() => waiveMutation.mutate()}
            >
              {waiveMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : null}
              Отменить штраф
            </Button>
          ) : null}
          <Button variant="outline" onClick={onClose}>
            Закрыть
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Metric({
  label,
  value,
  warn,
}: {
  label: string;
  value: string;
  warn?: boolean;
}) {
  return (
    <div>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={cn("mt-0.5 text-sm font-semibold tabular-nums", warn && "text-amber-700")}>
        {value}
      </div>
    </div>
  );
}
