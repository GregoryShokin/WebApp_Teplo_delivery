import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, LoaderCircle, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
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
import { InfoHint } from "@/components/ui-app/InfoHint";
import { apiErrorMessage } from "@/lib/api";
import { cn } from "@/lib/utils";
import { formatDateTime, formatDdsMoney, isoDateDaysAgo, toIsoDate } from "@/routes/dds/shared";
import {
  getKassaOpenShift,
  getKassaShifts,
  postKassaShift,
  syncKassaShifts,
  type KassaOpenShift,
} from "@/routes/kassa/api";
import { ShiftDetailDialog } from "@/routes/kassa/ShiftDetailDialog";
import {
  PayoutCategoryBadge,
  ShiftPenaltyBadge,
  ShiftPostedBadge,
  ShiftUncollectedBadge,
} from "@/routes/kassa/shared";

export function ShiftCloseTab({
  canSync,
  canPost,
  canWaive,
}: {
  canSync: boolean;
  canPost: boolean;
  canWaive: boolean;
}) {
  const queryClient = useQueryClient();
  const [dateFrom, setDateFrom] = useState(isoDateDaysAgo(7));
  const [dateTo, setDateTo] = useState(toIsoDate(new Date()));
  const [selectedShiftId, setSelectedShiftId] = useState<string | null>(null);

  const shiftsQuery = useQuery({
    queryKey: ["kassa", "shifts", dateFrom, dateTo],
    queryFn: () => getKassaShifts({ date_from: dateFrom, date_to: dateTo }),
  });
  const shifts = shiftsQuery.data ?? [];

  // Текущая незакрытая смена: синк её не видит (тянет только закрытые), читаем из iiko
  // напрямую — чтобы инкассацию было видно днём, а не после вечернего закрытия смены.
  const openShiftQuery = useQuery({
    queryKey: ["kassa", "shift-open"],
    queryFn: getKassaOpenShift,
    retry: false,
  });

  // Смены, где наличка зависла в ящике: инкассацию забыли или инкассировали не всю.
  const uncollected = useMemo(
    () =>
      (shiftsQuery.data ?? []).filter((shift) => (shift.uncollected_status ?? "none") !== "none"),
    [shiftsQuery.data],
  );
  const uncollectedTotal = uncollected.reduce(
    (sum, shift) => sum + (shift.uncollected_cash ?? 0),
    0,
  );

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
        queryClient.invalidateQueries({ queryKey: ["kassa", "shift-open"] }),
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
      {uncollected.length > 0 ? (
        <div className="flex items-start gap-3 rounded-md border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800">
          <AlertTriangle size={18} aria-hidden="true" className="mt-0.5 shrink-0" />
          <div>
            <span className="font-medium">
              В кассе зависло {formatDdsMoney(uncollectedTotal)} по{" "}
              {uncollected.length === 1 ? "смене" : "сменам"}{" "}
              {uncollected.map((shift) => `№ ${shift.session_number ?? "—"}`).join(", ")}.
            </span>{" "}
            Инкассацию не проводили или инкассировали не всю наличку — деньги остались в ящике
            и в Главную кассу не доехали.
            <InfoHint tone="alert" label="Зависшая наличка в кассе">
              Считаем как остаток смены минус флоут, которым её открыли: из денежного ящика
              штатно уходят только инкассация в Главную кассу, ЗП курьеров и наличные Алисы,
              поэтому остаток выше стартового размена означает пропущенную инкассацию. Порог
              сигнала — настройка «Порог зависшей налички в кассе» (Настройки → Касса).
              Деньги не теряются: их инкассируют следующей сменой, но приход в ДДС встанет
              датой ТОЙ смены, а не этой.
            </InfoHint>
          </div>
        </div>
      ) : null}

      <OpenShiftCard
        shift={openShiftQuery.data ?? null}
        isLoading={openShiftQuery.isLoading}
        error={openShiftQuery.error}
      />

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
              <TableHead>Сигналы</TableHead>
              <TableHead>В ДДС</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {shifts.length === 0 ? (
              <TableRow>
                <TableCell colSpan={10} className="py-8 text-center text-sm text-muted-foreground">
                  {shiftsQuery.isLoading ? "Загрузка…" : "За период нет закрытых смен."}
                </TableCell>
              </TableRow>
            ) : (
              shifts.map((shift) => (
                <TableRow
                  key={shift.id}
                  className="cursor-pointer"
                  onClick={() => setSelectedShiftId(shift.id)}
                >
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
                  <TableCell
                    className={cn(
                      "text-right tabular-nums",
                      (shift.uncollected_status ?? "none") !== "none" && "font-medium text-amber-700",
                    )}
                  >
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
                    <div className="flex flex-col items-start gap-1">
                      <ShiftUncollectedBadge status={shift.uncollected_status} />
                      <ShiftPenaltyBadge status={shift.penalty_status} />
                    </div>
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
                        onClick={(event) => {
                          event.stopPropagation();
                          postMutation.mutate(shift.id);
                        }}
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

      <ShiftDetailDialog
        shiftId={selectedShiftId}
        canWaive={canWaive}
        onClose={() => setSelectedShiftId(null)}
      />
    </div>
  );
}

/** Текущая незакрытая смена: что уже продали, что уже вынули, сколько лежит в ящике.
 *
 * Витрина только смотрит: в ДДС смена попадёт вечером, при закрытии (иначе задвоение с
 * авто-проводкой). Ценность в другом — видно, инкассировали сегодня или ещё нет. */
function OpenShiftCard({
  shift,
  isLoading,
  error,
}: {
  shift: KassaOpenShift | null;
  isLoading: boolean;
  error: unknown;
}) {
  if (error) {
    // Молчать нельзя: пустое место читается как «смены нет», а на деле iiko недоступен.
    return (
      <Card>
        <CardContent className="flex items-start gap-2 p-4 text-sm text-muted-foreground">
          <AlertTriangle size={16} aria-hidden="true" className="mt-0.5 shrink-0" />
          <span>
            Текущую смену показать не удалось: {apiErrorMessage(error, "iiko не ответил")}. На
            закрытые смены ниже это не влияет.
          </span>
        </CardContent>
      </Card>
    );
  }
  if (isLoading) {
    return (
      <Card>
        <CardContent className="flex items-center gap-2 p-4 text-sm text-muted-foreground">
          <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
          Смотрим текущую смену в iiko…
        </CardContent>
      </Card>
    );
  }
  if (shift === null) {
    return null;
  }
  const collected = shift.collected_cash ?? 0;
  return (
    <Card className="border-sky-200 bg-sky-50/40">
      <CardContent className="space-y-4 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">Смена № {shift.session_number ?? "—"} идёт</span>
          <Badge className="border-sky-200 bg-sky-50 text-sky-700" variant="outline">
            Не закрыта
          </Badge>
          <span className="text-sm text-muted-foreground">
            {shift.open_date ? `открыта ${formatDateTime(shift.open_date)}` : null}
          </span>
          <InfoHint label="Текущая смена">
            Читается из iiko напрямую при открытии вкладки. В систему смена попадёт только
            после закрытия — тогда же наличный контур будет проведён в ДДС. Остаток в ящике
            считаем сами (iiko у открытой смены его не отдаёт): стартовый флоут + наличная
            выручка + внесения − изъятия.
          </InfoHint>
        </div>

        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <OpenShiftMetric label="Нал. выручка" value={shift.cash_sales} />
          <OpenShiftMetric label="Изъятия" value={shift.pay_out} />
          <OpenShiftMetric label="Из них инкассация" value={shift.collected_cash} />
          <OpenShiftMetric label="В ящике сейчас" value={shift.cash_in_drawer} />
        </div>

        {collected <= 0 ? (
          <div className="flex items-start gap-2 text-sm text-amber-800">
            <AlertTriangle size={16} aria-hidden="true" className="mt-0.5 shrink-0" />
            <span>
              Инкассации в этой смене ещё не было. Если наличка останется в ящике к закрытию —
              она не доедет в Главную кассу и завтра сюда придёт сигнал.
            </span>
          </div>
        ) : null}

        {shift.payouts.length > 0 ? (
          <div className="rounded-md border bg-background">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Изъятие</TableHead>
                  <TableHead>Комментарий</TableHead>
                  <TableHead className="text-right">Сумма</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {shift.payouts.map((payout, index) => (
                  <TableRow key={payout.iiko_payout_id ?? `${payout.account_id_iiko}-${index}`}>
                    <TableCell>
                      <PayoutCategoryBadge category={payout.category} />
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {payout.comment ?? payout.account_name ?? "—"}
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
      </CardContent>
    </Card>
  );
}

function OpenShiftMetric({ label, value }: { label: string; value: number | null }) {
  return (
    <div className="rounded-md border bg-background p-3">
      <div className="text-sm text-muted-foreground">{label}</div>
      <div className="mt-1 text-xl font-semibold tabular-nums">
        {value == null ? "—" : formatDdsMoney(value)}
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
