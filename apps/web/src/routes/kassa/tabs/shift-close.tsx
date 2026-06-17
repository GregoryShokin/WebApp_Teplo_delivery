import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
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
import { formatDateTime, formatDdsMoney, isoDateDaysAgo, toIsoDate } from "@/routes/dds/shared";
import { getKassaShifts, postKassaShift, syncKassaShifts } from "@/routes/kassa/api";
import { ShiftPostedBadge } from "@/routes/kassa/shared";

export function ShiftCloseTab({ canSync, canPost }: { canSync: boolean; canPost: boolean }) {
  const queryClient = useQueryClient();
  const [dateFrom, setDateFrom] = useState(isoDateDaysAgo(7));
  const [dateTo, setDateTo] = useState(toIsoDate(new Date()));

  const shiftsQuery = useQuery({
    queryKey: ["kassa", "shifts", dateFrom, dateTo],
    queryFn: () => getKassaShifts({ date_from: dateFrom, date_to: dateTo }),
  });
  const shifts = shiftsQuery.data ?? [];

  const totals = useMemo(
    () =>
      (shiftsQuery.data ?? []).reduce(
        (acc, shift) => ({
          salesCash: acc.salesCash + (shift.cash_sales ?? 0),
          payOut: acc.payOut + (shift.pay_out ?? 0),
          diff: acc.diff + (shift.real_cash_diff ?? 0),
        }),
        { salesCash: 0, payOut: 0, diff: 0 },
      ),
    [shiftsQuery.data],
  );

  const syncMutation = useMutation({
    mutationFn: () => syncKassaShifts({ date_from: dateFrom, date_to: dateTo }),
    onSuccess: async (report) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["kassa", "shifts"] }),
        queryClient.invalidateQueries({ queryKey: ["dds", "cashflow"] }),
        queryClient.invalidateQueries({ queryKey: ["dds", "wallets"] }),
      ]);
      toast.success(`Смены загружены: ${report.fetched}, проведено ${report.posted}`);
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось загрузить смены")),
  });

  const postMutation = useMutation({
    mutationFn: (id: string) => postKassaShift(id),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["kassa", "shifts"] }),
        queryClient.invalidateQueries({ queryKey: ["dds", "cashflow"] }),
        queryClient.invalidateQueries({ queryKey: ["dds", "wallets"] }),
      ]);
      toast.success("Смена проведена в ДДС");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось провести смену")),
  });

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end gap-3">
        <div className="grid gap-1.5">
          <Label htmlFor="ks-from">С</Label>
          <Input
            id="ks-from"
            type="date"
            value={dateFrom}
            onChange={(event) => setDateFrom(event.target.value)}
            className="w-40"
          />
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="ks-to">По</Label>
          <Input
            id="ks-to"
            type="date"
            value={dateTo}
            onChange={(event) => setDateTo(event.target.value)}
            className="w-40"
          />
        </div>
        {canSync ? (
          <Button
            variant="outline"
            onClick={() => syncMutation.mutate()}
            disabled={syncMutation.isPending}
          >
            {syncMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : (
              <RefreshCw size={16} aria-hidden="true" />
            )}
            Загрузить из iiko
          </Button>
        ) : null}
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <MetricCard label="Наличная выручка" value={totals.salesCash} />
        <MetricCard label="Изъятия / инкассация" value={totals.payOut} />
        <MetricCard label="Расхождение кассы" value={totals.diff} warnNonZero />
      </div>

      <div className="rounded-lg border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Закрыта</TableHead>
              <TableHead>Смена</TableHead>
              <TableHead className="text-right">Нал. выручка</TableHead>
              <TableHead className="text-right">Безнал</TableHead>
              <TableHead className="text-right">Изъятия</TableHead>
              <TableHead className="text-right">Остаток</TableHead>
              <TableHead className="text-right">Расхожд.</TableHead>
              <TableHead>В ДДС</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {shifts.length === 0 ? (
              <TableRow>
                <TableCell colSpan={9} className="py-8 text-center text-sm text-muted-foreground">
                  {shiftsQuery.isLoading ? "Загрузка…" : "За период нет закрытых смен."}
                </TableCell>
              </TableRow>
            ) : (
              shifts.map((shift) => (
                <TableRow key={shift.id}>
                  <TableCell>{formatDateTime(shift.close_date)}</TableCell>
                  <TableCell className="font-medium">№ {shift.session_number ?? "—"}</TableCell>
                  <TableCell className="text-right tabular-nums">
                    {formatDdsMoney(shift.cash_sales)}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {formatDdsMoney(shift.sales_card)}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {formatDdsMoney(shift.pay_out)}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {formatDdsMoney(shift.cash_remain)}
                  </TableCell>
                  <TableCell
                    className={cn(
                      "text-right tabular-nums",
                      (shift.real_cash_diff ?? 0) !== 0 && "font-medium text-amber-700",
                    )}
                  >
                    {formatDdsMoney(shift.real_cash_diff)}
                  </TableCell>
                  <TableCell>
                    <ShiftPostedBadge posted={shift.posted} />
                  </TableCell>
                  <TableCell className="text-right">
                    {canPost && !shift.posted ? (
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={postMutation.isPending}
                        onClick={() => postMutation.mutate(shift.id)}
                      >
                        Провести
                      </Button>
                    ) : null}
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}

function MetricCard({
  label,
  value,
  warnNonZero,
}: {
  label: string;
  value: number;
  warnNonZero?: boolean;
}) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="text-sm text-muted-foreground">{label}</div>
        <div
          className={cn(
            "mt-1 text-2xl font-semibold tabular-nums",
            warnNonZero && value !== 0 && "text-amber-700",
          )}
        >
          {formatDdsMoney(value)}
        </div>
      </CardContent>
    </Card>
  );
}
