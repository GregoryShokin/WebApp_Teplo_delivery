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
import {
  getDdsArticles,
  getSafeAllocations,
  type SafeAllocationRead,
  type WalletRead,
} from "@/lib/api";
import { formatDdsMoney } from "@/routes/dds/shared";

const STATUS_LABEL: Record<SafeAllocationRead["status"], string> = {
  reserved: "зарезервирован",
  partially_paid: "частично выплачен",
  paid: "выплачен",
  cancelled: "отменён",
};

/** Информационная раскладка остатка Сейфа без денежных действий. */
export function SafeAccountDialog({
  wallet,
  open,
  onClose,
}: {
  wallet: WalletRead | null;
  open: boolean;
  onClose: () => void;
}) {
  const walletId = wallet?.id ?? null;
  const articlesQuery = useQuery({ queryKey: ["dds", "articles"], queryFn: getDdsArticles });
  const allocationsQuery = useQuery({
    queryKey: ["dds", "safe-allocations", walletId],
    queryFn: () => getSafeAllocations(walletId!),
    enabled: Boolean(walletId) && open,
  });

  if (!wallet) {
    return null;
  }

  const articles = articlesQuery.data ?? [];
  const allocations = allocationsQuery.data ?? [];

  return (
    <Dialog open={open} onOpenChange={(value) => !value && onClose()}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{wallet.name}</DialogTitle>
          <DialogDescription>
            Информация об остатке и действующих резервах на Сейфе.
          </DialogDescription>
        </DialogHeader>

        <div className="grid grid-cols-3 gap-2 rounded-lg border bg-muted/20 p-3 text-center">
          <Stat label="Всего" value={wallet.balance} />
          <Stat label="Свободно" value={wallet.free_total} accent="emerald" />
          <Stat label="Зарезервировано" value={wallet.reserved_total} accent="amber" />
        </div>

        <div className="grid gap-2 border-t pt-4">
          <Label className="text-base font-semibold">На что зарезервировано</Label>
          {allocationsQuery.isLoading ? (
            <div className="h-16 animate-pulse rounded bg-muted/60" />
          ) : allocationsQuery.isError ? (
            <div className="text-sm text-destructive">Не удалось загрузить резервы.</div>
          ) : allocations.length === 0 ? (
            <div className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
              Активных резервов нет — вся сумма свободна.
            </div>
          ) : (
            allocations.map((allocation) => {
              const article = articles.find((item) => item.id === allocation.article_id);
              return (
                <div key={allocation.id} className="rounded-md border p-3 text-sm">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="font-medium">{article?.name ?? "Без статьи"}</span>
                        {allocation.source_draft_id ? (
                          <Badge className="border-amber-200 bg-amber-50 text-amber-700">
                            из банковской выплаты
                          </Badge>
                        ) : null}
                      </div>
                      {allocation.counterparty_name ? (
                        <div className="mt-1 text-xs font-medium">
                          {allocation.counterparty_name}
                        </div>
                      ) : null}
                      {allocation.purpose ? (
                        <div className="mt-1 text-xs text-muted-foreground">
                          {allocation.purpose}
                        </div>
                      ) : null}
                    </div>
                    <div className="shrink-0 text-right tabular-nums">
                      <div className="font-medium text-amber-700">
                        {formatDdsMoney(allocation.outstanding)}
                      </div>
                      <div className="text-xs text-muted-foreground">
                        из {formatDdsMoney(allocation.amount)} · {STATUS_LABEL[allocation.status]}
                      </div>
                    </div>
                  </div>
                </div>
              );
            })
          )}
        </div>
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
