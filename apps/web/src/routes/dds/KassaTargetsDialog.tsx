import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import {
  apiErrorMessage,
  cancelSafeAllocation,
  getDdsKassaTargets,
} from "@/lib/api";
import { usePermissions } from "@/lib/permissions";
import { formatDdsMoney } from "@/routes/dds/shared";

const KIND_LABEL: Record<string, string> = { advance: "Аванс", loan: "Заём" };

/**
 * Read-only диалог «Целевые в Торговой кассе» (вторая дверь с «Денег сегодня»):
 * целёвки в кассе и ожидающие разрешения БЕЗ кнопок выдачи — выдаёт только
 * администратор в модуле «Касса». Менеджеру с правом резервов Сейфа доступна
 * отмена целёвки (деньги остаются в кассе свободными, проводок нет).
 */
export function KassaTargetsDialog({
  open,
  onClose,
  onNavigate,
}: {
  open: boolean;
  onClose: () => void;
  onNavigate?: (path: string) => void;
}) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const canCancelTarget = permissions.canPerformAction("finance.safe.allocate");

  const targetsQuery = useQuery({
    queryKey: ["dds", "kassa-targets"],
    queryFn: getDdsKassaTargets,
    enabled: open,
  });

  const cancelMutation = useMutation({
    mutationFn: (id: string) => cancelSafeAllocation(id),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["dds"] }),
        queryClient.invalidateQueries({ queryKey: ["kassa"] }),
      ]);
      toast.success("Целёвка отменена — деньги остались в кассе свободными");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось отменить целёвку")),
  });

  const data = targetsQuery.data;

  return (
    <Dialog open={open} onOpenChange={(value) => !value && onClose()}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Целевые в Торговой кассе</DialogTitle>
          <DialogDescription>
            Деньги уже в кассе у администраторов: выдачу проводит администратор во вкладке
            «К выдаче» модуля «Касса».
          </DialogDescription>
        </DialogHeader>

        {targetsQuery.isLoading ? (
          <div className="h-16 animate-pulse rounded bg-muted/60" />
        ) : data ? (
          <>
            <div className="grid grid-cols-3 gap-2 rounded-lg border bg-muted/20 p-3 text-center">
              <Stat label="В кассе" value={data.balance} />
              <Stat label="Целевые" value={data.targets_total} accent="amber" />
              <Stat label="Свободно" value={data.balance - data.targets_total} accent="emerald" />
            </div>

            <div className="grid gap-2">
              <Label className="text-base font-semibold">Целёвки в кассе</Label>
              {data.targets.length === 0 ? (
                <div className="text-sm text-muted-foreground">
                  Целёвок в кассе нет — их передают из карточки Сейфа («Передать в кассу»).
                </div>
              ) : (
                data.targets.map((target) => (
                  <div key={target.id} className="rounded-md border p-3 text-sm">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className="font-medium">
                            {target.article_name ?? "Без статьи"}
                          </span>
                          {target.from_bank_payout ? (
                            <Badge className="border-amber-200 bg-amber-50 text-amber-700">
                              из банковской выплаты
                            </Badge>
                          ) : null}
                        </div>
                        {target.counterparty_name ? (
                          <div className="text-xs font-medium">{target.counterparty_name}</div>
                        ) : null}
                        {target.purpose ? (
                          <div className="text-xs text-muted-foreground">{target.purpose}</div>
                        ) : null}
                      </div>
                      <div className="flex items-center gap-2">
                        <div className="text-right tabular-nums">
                          <div className="font-medium">{formatDdsMoney(target.outstanding)}</div>
                          <div className="text-xs text-muted-foreground">
                            из {formatDdsMoney(target.amount)}
                          </div>
                        </div>
                        {canCancelTarget ? (
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={cancelMutation.isPending}
                            onClick={() => cancelMutation.mutate(target.id)}
                            title="Снять целёвку: деньги останутся в кассе свободными наличными, проводок не будет"
                          >
                            Отменить
                          </Button>
                        ) : null}
                      </div>
                    </div>
                  </div>
                ))
              )}
            </div>

            <div className="grid gap-2">
              <Label className="text-base font-semibold">Ожидающие разрешения</Label>
              {data.permissions.length === 0 ? (
                <div className="text-sm text-muted-foreground">
                  Разрешений на авансы и займы нет.
                </div>
              ) : (
                data.permissions.map((permission) => (
                  <div key={permission.id} className="rounded-md border p-3 text-sm">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className="font-medium">{permission.employee_name}</span>
                          <Badge
                            variant={permission.kind === "loan" ? "destructive" : "secondary"}
                          >
                            {KIND_LABEL[permission.kind] ?? permission.kind}
                          </Badge>
                        </div>
                        <div className="text-xs text-muted-foreground">
                          {permission.created_by_label
                            ? `оформил(а) ${permission.created_by_label}`
                            : "уйдёт из наличных кассы после «Выплачено»"}
                        </div>
                      </div>
                      <div className="text-right font-medium tabular-nums">
                        {formatDdsMoney(permission.amount)}
                      </div>
                    </div>
                  </div>
                ))
              )}
            </div>

            {onNavigate ? (
              <Button
                variant="outline"
                className="justify-center"
                onClick={() => {
                  onClose();
                  onNavigate("/kassa");
                }}
              >
                Открыть модуль «Касса» — вкладка «К выдаче»
                <ArrowRight size={14} aria-hidden="true" />
              </Button>
            ) : null}
          </>
        ) : (
          <div className="text-sm text-muted-foreground">Не удалось загрузить данные.</div>
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
      <div className={`text-base font-semibold tabular-nums ${color}`}>
        {formatDdsMoney(value)}
      </div>
    </div>
  );
}
