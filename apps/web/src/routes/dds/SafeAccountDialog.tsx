import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ArticleCombobox } from "@/components/ui-app/ArticleCombobox";
import {
  apiErrorMessage,
  cancelSafeAllocation,
  createSafeAllocation,
  getDdsArticles,
  getSafeAllocations,
  paySafeAllocation,
  reconcileSafe,
  withdrawSafeCash,
  type SafeAllocationRead,
  type SafeReconcileResult,
  type WalletRead,
} from "@/lib/api";
import { usePermissions } from "@/lib/permissions";
import { formatDdsMoney } from "@/routes/dds/shared";

const STATUS_LABEL: Record<SafeAllocationRead["status"], string> = {
  reserved: "Зарезервирован",
  partially_paid: "Частично оплачен",
  paid: "Оплачен",
  cancelled: "Отменён",
};

/**
 * Карточка подотчётного счёта «Сейф»: раскладка остатка (всего/свободно/резерв),
 * создание резервов под статью и подтверждение оплат (полных и частичных).
 */
export function SafeAccountDialog({
  wallet,
  open,
  onClose,
}: {
  wallet: WalletRead | null;
  open: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  const canAllocate = permissions.canPerformAction("finance.safe.allocate");
  const canConfirmPaid = permissions.canPerformAction("finance.safe.confirm_paid");

  const walletId = wallet?.id ?? null;
  const articlesQuery = useQuery({ queryKey: ["dds", "articles"], queryFn: getDdsArticles });
  const allocationsQuery = useQuery({
    queryKey: ["dds", "safe-allocations", walletId],
    queryFn: () => getSafeAllocations(walletId!),
    enabled: Boolean(walletId) && open,
  });

  const [amount, setAmount] = useState("");
  const [articleId, setArticleId] = useState("none");
  const [purpose, setPurpose] = useState("");
  const [payAmounts, setPayAmounts] = useState<Record<string, string>>({});
  const [withdrawAmount, setWithdrawAmount] = useState("");
  const [reconcileActual, setReconcileActual] = useState("");
  const [reconcileResult, setReconcileResult] = useState<SafeReconcileResult | null>(null);

  const invalidate = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["dds", "safe-allocations", walletId] }),
      queryClient.invalidateQueries({ queryKey: ["dds", "wallets"] }),
      queryClient.invalidateQueries({ queryKey: ["dds", "journal"] }),
    ]);
  };

  const resetForm = () => {
    setAmount("");
    setArticleId("none");
    setPurpose("");
  };

  const createMutation = useMutation({
    mutationFn: (payFull: boolean) =>
      createSafeAllocation(walletId!, {
        amount,
        article_id: articleId,
        purpose: purpose.trim() || null,
        pay_full: payFull,
      }),
    onSuccess: async (_data, payFull) => {
      await invalidate();
      toast.success(payFull ? "Оплачено с Сейфа" : "Резерв создан");
      resetForm();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось создать резерв")),
  });

  const payMutation = useMutation({
    mutationFn: ({ id, amt }: { id: string; amt: string }) => paySafeAllocation(id, amt),
    onSuccess: async () => {
      await invalidate();
      toast.success("Оплата проведена");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось провести оплату")),
  });

  const cancelMutation = useMutation({
    mutationFn: (id: string) => cancelSafeAllocation(id),
    onSuccess: async () => {
      await invalidate();
      toast.success("Резерв отменён");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось отменить резерв")),
  });

  const withdrawMutation = useMutation({
    mutationFn: () => withdrawSafeCash(walletId!, withdrawAmount),
    onSuccess: async () => {
      await invalidate();
      toast.success("Наличные сняты в кассу Черникова");
      setWithdrawAmount("");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось снять наличные")),
  });

  const reconcileMutation = useMutation({
    mutationFn: (applyAdjustment: boolean) =>
      reconcileSafe(walletId!, reconcileActual, applyAdjustment),
    onSuccess: async (result) => {
      setReconcileResult(result);
      if (result.adjusted) {
        await invalidate();
        toast.success("Остаток выровнен по карте");
      }
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сверить остаток")),
  });

  if (!wallet) {
    return null;
  }

  const articles = articlesQuery.data ?? [];
  const allocations = allocationsQuery.data ?? [];
  const busy =
    createMutation.isPending ||
    payMutation.isPending ||
    cancelMutation.isPending ||
    withdrawMutation.isPending ||
    reconcileMutation.isPending;
  const canSubmitCreate = Number(amount) > 0 && articleId !== "none";

  return (
    <Dialog open={open} onOpenChange={(value) => !value && onClose()}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{wallet.name}</DialogTitle>
          <DialogDescription>
            Подотчётный счёт: пополнения видны из выписки, оплаты подтверждаются вручную.
          </DialogDescription>
        </DialogHeader>

        <div className="grid grid-cols-3 gap-2 rounded-lg border bg-muted/20 p-3 text-center">
          <Stat label="Всего" value={wallet.balance} />
          <Stat label="Свободно" value={wallet.free_total} accent="emerald" />
          <Stat label="Зарезервировано" value={wallet.reserved_total} accent="amber" />
        </div>

        {canAllocate ? (
          <div className="grid gap-3 border-t pt-4">
            <Label className="text-base font-semibold">Новый резерв</Label>
            <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_140px]">
              <ArticleCombobox articles={articles} value={articleId} onChange={setArticleId} />
              <Input
                className="text-right tabular-nums"
                inputMode="decimal"
                placeholder="Сумма"
                value={amount}
                onChange={(event) => setAmount(event.target.value)}
              />
            </div>
            <Input
              placeholder="Назначение / получатель (необязательно)"
              value={purpose}
              onChange={(event) => setPurpose(event.target.value)}
            />
            <div className="flex flex-wrap gap-2">
              <Button disabled={busy || !canSubmitCreate} onClick={() => createMutation.mutate(false)}>
                Зарезервировать
              </Button>
              {canConfirmPaid ? (
                <Button
                  variant="outline"
                  disabled={busy || !canSubmitCreate}
                  onClick={() => createMutation.mutate(true)}
                  title="Оплатить сразу из свободных средств, без отдельного шага"
                >
                  Оплатить сразу
                </Button>
              ) : null}
            </div>
          </div>
        ) : null}

        <div className="grid gap-2 border-t pt-4">
          <Label className="text-base font-semibold">Резервы</Label>
          {allocationsQuery.isLoading ? (
            <div className="h-10 animate-pulse rounded bg-muted/60" />
          ) : allocations.length === 0 ? (
            <div className="text-sm text-muted-foreground">Активных резервов нет</div>
          ) : (
            allocations.map((allocation) => {
              const article = articles.find((item) => item.id === allocation.article_id);
              const payValue = payAmounts[allocation.id] ?? allocation.outstanding;
              const payable = allocation.status === "reserved" || allocation.status === "partially_paid";
              return (
                <div key={allocation.id} className="rounded-md border p-3 text-sm">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="font-medium">{article?.name ?? "Без статьи"}</span>
                        {allocation.source_draft_id ? (
                          // Авто-резерв: создан оплатой черновика выплаты на карту ИП
                          // (неофициальный поставщик) — деньги уже пришли на Сейф.
                          <Badge
                            className="border-amber-200 bg-amber-50 text-amber-700"
                            title="Создан автоматически при оплате банковской выплаты на карту ИП"
                          >
                            из банковской выплаты
                          </Badge>
                        ) : null}
                      </div>
                      {allocation.counterparty_name ? (
                        <div className="text-xs font-medium">{allocation.counterparty_name}</div>
                      ) : null}
                      {allocation.purpose ? (
                        <div className="text-xs text-muted-foreground">{allocation.purpose}</div>
                      ) : null}
                    </div>
                    <div className="text-right tabular-nums">
                      <div className="font-medium">{formatDdsMoney(allocation.outstanding)}</div>
                      <div className="text-xs text-muted-foreground">
                        из {formatDdsMoney(allocation.amount)} · {STATUS_LABEL[allocation.status]}
                      </div>
                    </div>
                  </div>
                  {canConfirmPaid && payable ? (
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      <Input
                        className="h-9 w-32 text-right tabular-nums"
                        inputMode="decimal"
                        value={payValue}
                        onChange={(event) =>
                          setPayAmounts((prev) => ({ ...prev, [allocation.id]: event.target.value }))
                        }
                      />
                      <Button
                        size="sm"
                        disabled={busy || Number(payValue) <= 0}
                        onClick={() => payMutation.mutate({ id: allocation.id, amt: payValue })}
                      >
                        Оплатить
                      </Button>
                      {canAllocate ? (
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={busy}
                          onClick={() => cancelMutation.mutate(allocation.id)}
                        >
                          Отменить
                        </Button>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              );
            })
          )}
        </div>

        {canConfirmPaid ? (
          <div className="grid gap-3 border-t pt-4">
            <Label className="text-base font-semibold">Снять наличные в кассу</Label>
            <div className="flex flex-wrap items-center gap-2">
              <Input
                className="h-9 w-40 text-right tabular-nums"
                inputMode="decimal"
                placeholder="Сумма"
                value={withdrawAmount}
                onChange={(event) => setWithdrawAmount(event.target.value)}
              />
              <Button
                size="sm"
                disabled={busy || Number(withdrawAmount) <= 0}
                onClick={() => withdrawMutation.mutate()}
              >
                Снять в кассу Черникова
              </Button>
            </div>
          </div>
        ) : null}

        {canConfirmPaid ? (
          <div className="grid gap-3 border-t pt-4">
            <Label className="text-base font-semibold">Сверка с картой</Label>
            <div className="flex flex-wrap items-center gap-2">
              <Input
                className="h-9 w-40 text-right tabular-nums"
                inputMode="decimal"
                placeholder="Остаток по карте"
                value={reconcileActual}
                onChange={(event) => setReconcileActual(event.target.value)}
              />
              <Button
                size="sm"
                variant="outline"
                disabled={busy || reconcileActual === ""}
                onClick={() => reconcileMutation.mutate(false)}
              >
                Сверить
              </Button>
            </div>
            {reconcileResult ? (
              <div className="rounded-md border bg-muted/20 p-3 text-sm">
                <div className="tabular-nums">
                  Учётный {formatDdsMoney(reconcileResult.accounted)} · по карте{" "}
                  {formatDdsMoney(reconcileResult.actual)}
                </div>
                {Number(reconcileResult.delta) === 0 ? (
                  <div className="mt-1 font-medium text-emerald-600">Расхождений нет ✓</div>
                ) : (
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <span className="font-medium text-amber-600">
                      Расхождение {formatDdsMoney(reconcileResult.delta)}
                    </span>
                    {!reconcileResult.adjusted ? (
                      <Button
                        size="sm"
                        disabled={busy}
                        onClick={() => reconcileMutation.mutate(true)}
                        title="Провести корректирующую проводку и выровнять учётный остаток"
                      >
                        Выровнять
                      </Button>
                    ) : (
                      <span className="text-emerald-600">выровнено ✓</span>
                    )}
                  </div>
                )}
              </div>
            ) : null}
          </div>
        ) : null}
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
  value: string | null;
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
