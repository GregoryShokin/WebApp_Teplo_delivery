import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { HandCoins } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { apiErrorMessage } from "@/lib/api";
import { formatRub } from "@/routes/counterparties/shared";
import {
  cancelKassaAdvancePermission,
  disburseKassaAdvancePermission,
  getKassaPending,
  payKassaTarget,
  type KassaAdvancePermission,
  type KassaTarget,
} from "@/routes/kassa/api";

const KIND_LABEL: Record<KassaAdvancePermission["kind"], string> = {
  advance: "Аванс",
  loan: "Заём",
};

/**
 * Вкладка «К выдаче»: целёвки, переданные в кассу (частичная выдача допустима),
 * и разрешения на авансы/займы (только вся сумма). Выдаёт администратор кассы.
 */
export function KassaPendingTab() {
  const queryClient = useQueryClient();
  const pendingQuery = useQuery({ queryKey: ["kassa", "pending"], queryFn: getKassaPending });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["kassa"] });
    void queryClient.invalidateQueries({ queryKey: ["dds"] });
  };

  const payTargetMutation = useMutation({
    mutationFn: ({ id, amount }: { id: string; amount: number }) => payKassaTarget(id, amount),
    onSuccess: () => {
      toast.success("Выдано — запись в кассовом журнале");
      invalidate();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось выдать")),
  });

  const disburseMutation = useMutation({
    mutationFn: (id: string) => disburseKassaAdvancePermission(id),
    onSuccess: () => {
      toast.success("Выплачено — удержание пойдёт с даты выдачи");
      invalidate();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось выплатить")),
  });

  const cancelMutation = useMutation({
    mutationFn: (id: string) => cancelKassaAdvancePermission(id),
    onSuccess: () => {
      toast.success("Разрешение отменено — создатель увидит отметку");
      invalidate();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось отменить")),
  });

  const pending = pendingQuery.data;
  const busy =
    payTargetMutation.isPending || disburseMutation.isPending || cancelMutation.isPending;

  if (pendingQuery.isLoading) {
    return <div className="h-24 animate-pulse rounded-lg bg-muted/60" />;
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="pt-5">
          <div className="text-sm text-muted-foreground">Наличные администраторов</div>
          <div className="mt-1 text-2xl font-semibold tabular-nums">
            {pending ? formatRub(pending.balance) : "—"}
          </div>
          {pending ? (
            <div className="mt-0.5 text-xs text-muted-foreground">
              в кассе {formatRub(pending.balance)} · из них целевые{" "}
              <span className="font-medium text-amber-600">
                {formatRub(pending.targets_total)}
              </span>
            </div>
          ) : null}
        </CardContent>
      </Card>

      {pending && pending.targets.length === 0 && pending.permissions.length === 0 ? (
        <div className="rounded-lg border border-dashed p-6 text-sm text-muted-foreground">
          К выдаче ничего нет: целёвки передаются из Сейфа («Передать в кассу»), разрешения
          на авансы и займы приходят со страницы «Авансы и займы».
        </div>
      ) : null}

      {pending && pending.targets.length > 0 ? (
        <div className="grid gap-2">
          <Label className="text-base font-semibold">Целевые выплаты</Label>
          {pending.targets.map((target) => (
            <TargetCard
              key={target.id}
              target={target}
              balance={pending.balance}
              busy={busy}
              onPay={(amount) => payTargetMutation.mutate({ id: target.id, amount })}
            />
          ))}
        </div>
      ) : null}

      {pending && pending.permissions.length > 0 ? (
        <div className="grid gap-2">
          <Label className="text-base font-semibold">Разрешения на авансы и займы</Label>
          {pending.permissions.map((permission) => (
            <div key={permission.id} className="rounded-md border p-3 text-sm">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="font-medium">{permission.employee_name}</span>
                    <Badge variant={permission.kind === "loan" ? "destructive" : "secondary"}>
                      {KIND_LABEL[permission.kind]}
                    </Badge>
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {[
                      permission.created_by_label
                        ? `оформил(а) ${permission.created_by_label}`
                        : null,
                      permission.comment,
                    ]
                      .filter(Boolean)
                      .join(" · ") || "Выдаётся только вся сумма"}
                  </div>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    size="sm"
                    disabled={busy}
                    onClick={() => disburseMutation.mutate(permission.id)}
                  >
                    <HandCoins size={14} aria-hidden="true" />
                    Выплачено {formatRub(permission.amount)}
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={busy}
                    onClick={() => cancelMutation.mutate(permission.id)}
                  >
                    Отменить
                  </Button>
                </div>
              </div>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

/** Карточка целёвки: «Выдано» раскрывает поле суммы, предзаполненное остатком. */
function TargetCard({
  target,
  balance,
  busy,
  onPay,
}: {
  target: KassaTarget;
  balance: number;
  busy: boolean;
  onPay: (amount: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const [amount, setAmount] = useState("");

  const amountNumber = Number(amount.replace(",", "."));
  const validAmount = Number.isFinite(amountNumber) && amountNumber > 0;
  const overOutstanding = validAmount && amountNumber > target.outstanding + 0.005;
  const overBalance = validAmount && amountNumber > balance;

  return (
    <div className="rounded-md border p-3 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="font-medium">{target.article_name ?? "Без статьи"}</span>
            {target.from_bank_payout ? (
              <Badge
                className="border-amber-200 bg-amber-50 text-amber-700"
                title="Создана автоматически при оплате банковской выплаты на карту ИП"
              >
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
        <div className="text-right tabular-nums">
          <div className="font-medium">{formatRub(target.outstanding)}</div>
          {target.amount_paid > 0 ? (
            <div className="text-xs text-muted-foreground">
              из {formatRub(target.amount)} · выдано {formatRub(target.amount_paid)}
            </div>
          ) : (
            <div className="text-xs text-muted-foreground">из {formatRub(target.amount)}</div>
          )}
        </div>
      </div>

      {open ? (
        <div className="mt-2 grid gap-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Input
              className="h-9 w-36 text-right tabular-nums"
              inputMode="decimal"
              value={amount}
              onChange={(event) => setAmount(event.target.value)}
            />
            <Button
              size="sm"
              disabled={busy || !validAmount || overOutstanding}
              onClick={() => onPay(amountNumber)}
            >
              Подтвердить выдачу
            </Button>
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => setOpen(false)}>
              Отмена
            </Button>
          </div>
          {overOutstanding ? (
            <p className="text-xs font-medium text-destructive">
              Больше остатка целёвки ({formatRub(target.outstanding)}) выдать нельзя.
            </p>
          ) : overBalance ? (
            <p className="text-xs font-medium text-amber-600">
              Сумма больше учётного остатка кассы ({formatRub(balance)}) — выдача пройдёт, но
              остаток уйдёт в минус. Проверьте сумму.
            </p>
          ) : (
            <p className="text-xs text-muted-foreground">
              Частичная выдача допустима — остаток целёвки останется в списке.
            </p>
          )}
        </div>
      ) : (
        <div className="mt-2">
          <Button
            size="sm"
            disabled={busy}
            onClick={() => {
              setAmount(String(target.outstanding));
              setOpen(true);
            }}
          >
            <HandCoins size={14} aria-hidden="true" />
            Выдано
          </Button>
        </div>
      )}
    </div>
  );
}
