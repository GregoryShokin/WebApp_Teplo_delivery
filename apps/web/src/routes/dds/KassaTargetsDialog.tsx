import { useQuery } from "@tanstack/react-query";

import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { getDdsKassaTargets } from "@/lib/api";
import { formatDdsMoney } from "@/routes/dds/shared";

/** Информационная раскладка остатка Торговой кассы без денежных действий. */
export function KassaTargetsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const targetsQuery = useQuery({
    queryKey: ["dds", "kassa-targets"],
    queryFn: getDdsKassaTargets,
    enabled: open,
  });

  const data = targetsQuery.data;

  return (
    <Dialog open={open} onOpenChange={(value) => !value && onClose()}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Торговая касса</DialogTitle>
          <DialogDescription>
            Информация об остатке и действующих резервах в Торговой кассе.
          </DialogDescription>
        </DialogHeader>

        {targetsQuery.isLoading ? (
          <div className="h-24 animate-pulse rounded bg-muted/60" />
        ) : targetsQuery.isError || !data ? (
          <div className="text-sm text-destructive">Не удалось загрузить данные кассы.</div>
        ) : (
          <>
            <div className="grid grid-cols-3 gap-2 rounded-lg border bg-muted/20 p-3 text-center">
              <Stat label="Всего" value={data.balance} />
              <Stat label="Свободно" value={data.balance - data.targets_total} accent="emerald" />
              <Stat label="Зарезервировано" value={data.targets_total} accent="amber" />
            </div>

            <div className="grid gap-2 border-t pt-4">
              <Label className="text-base font-semibold">На что зарезервировано</Label>
              {data.targets.length === 0 ? (
                <div className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
                  Активных резервов нет — вся сумма свободна.
                </div>
              ) : (
                data.targets.map((target) => (
                  <div key={target.id} className="rounded-md border p-3 text-sm">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className="font-medium">{target.article_name ?? "Без статьи"}</span>
                          {target.from_bank_payout ? (
                            <Badge className="border-amber-200 bg-amber-50 text-amber-700">
                              из банковской выплаты
                            </Badge>
                          ) : null}
                          {target.is_payroll ? (
                            <Badge className="border-teal-200 bg-teal-50 text-teal-700">
                              зарплатная ведомость
                            </Badge>
                          ) : null}
                        </div>
                        {target.counterparty_name ? (
                          <div className="mt-1 text-xs font-medium">{target.counterparty_name}</div>
                        ) : null}
                        {target.purpose ? (
                          <div className="mt-1 text-xs text-muted-foreground">{target.purpose}</div>
                        ) : null}
                      </div>
                      <div className="shrink-0 text-right tabular-nums">
                        <div className="font-medium text-amber-700">
                          {formatDdsMoney(target.outstanding)}
                        </div>
                        <div className="text-xs text-muted-foreground">
                          из {formatDdsMoney(target.amount)}
                        </div>
                      </div>
                    </div>
                  </div>
                ))
              )}
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

function Stat({
  label,
  value,
  accent,
}: {
  label: string;
  value: number;
  accent?: "emerald" | "amber";
}) {
  const color =
    accent === "emerald"
      ? "text-emerald-600"
      : accent === "amber"
        ? "text-amber-600"
        : "text-foreground";
  return (
    <div>
      <div className="text-xs uppercase text-muted-foreground">{label}</div>
      <div className={`text-base font-semibold tabular-nums ${color}`}>{formatDdsMoney(value)}</div>
    </div>
  );
}
