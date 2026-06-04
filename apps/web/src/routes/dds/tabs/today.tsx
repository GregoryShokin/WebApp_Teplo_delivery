import { useQuery } from "@tanstack/react-query";
import { AlertCircle, ArrowRight, Landmark, Wallet } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { getDdsBankOperations, getDdsOwnerReview, getDdsWallets } from "@/lib/api";
import {
  BankSyncButton,
  formatDdsMoney,
  isoDateDaysAgo,
  toIsoDate,
} from "@/routes/dds/shared";

export function TodayTab({ onNavigate }: { onNavigate: (path: string) => void }) {
  const walletsQuery = useQuery({ queryKey: ["dds", "wallets"], queryFn: getDdsWallets });
  const ownerReviewQuery = useQuery({
    queryKey: ["dds", "owner-review", "today"],
    queryFn: () => getDdsOwnerReview({ limit: 1, offset: 0 }),
  });
  const recentOperationsQuery = useQuery({
    queryKey: ["dds", "operations", "recent"],
    queryFn: () =>
      getDdsBankOperations({
        from: isoDateDaysAgo(7),
        to: toIsoDate(new Date()),
        limit: 1,
        offset: 0,
      }),
  });

  const wallets = walletsQuery.data ?? [];

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-lg font-semibold tracking-normal">Деньги сегодня</h2>
          <p className="text-sm text-muted-foreground">
            Балансы кошельков и быстрые сигналы по банковским операциям.
          </p>
        </div>
        <BankSyncButton />
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        {walletsQuery.isLoading
          ? Array.from({ length: 5 }).map((_, index) => (
              <Card key={index}>
                <CardContent className="grid gap-3 p-4">
                  <div className="h-4 w-28 rounded bg-muted" />
                  <div className="h-8 w-32 rounded bg-muted" />
                  <div className="h-3 w-24 rounded bg-muted" />
                </CardContent>
              </Card>
            ))
          : wallets.map((wallet) => (
              <Card key={wallet.id}>
                <CardContent className="grid gap-3 p-4">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="truncate text-sm font-medium">{wallet.name}</div>
                      <div className="text-xs text-muted-foreground">{wallet.code}</div>
                    </div>
                    <Badge variant="outline">{wallet.type}</Badge>
                  </div>
                  <div>
                    <div className="text-2xl font-semibold tabular-nums">
                      {formatDdsMoney(wallet.balance)}
                    </div>
                    <div className="text-xs text-muted-foreground">{wallet.currency}</div>
                  </div>
                  <div className="text-xs text-muted-foreground">
                    Начальный баланс {formatDdsMoney(wallet.opening_balance)}
                  </div>
                </CardContent>
              </Card>
            ))}
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        <button
          className="rounded-lg border bg-card p-4 text-left transition-colors hover:bg-muted/50"
          onClick={() => onNavigate("/dds/owner-review")}
          type="button"
        >
          <div className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <span className="flex h-10 w-10 items-center justify-center rounded-md bg-orange-50 text-orange-700">
                <AlertCircle size={18} aria-hidden="true" />
              </span>
              <div>
                <div className="text-sm text-muted-foreground">Pending owner review</div>
                <div className="text-2xl font-semibold tabular-nums">
                  {ownerReviewQuery.data?.total ?? "—"}
                </div>
              </div>
            </div>
            <ArrowRight size={18} aria-hidden="true" />
          </div>
        </button>

        <button
          className="rounded-lg border bg-card p-4 text-left transition-colors hover:bg-muted/50"
          onClick={() => onNavigate("/dds/operations")}
          type="button"
        >
          <div className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <span className="flex h-10 w-10 items-center justify-center rounded-md bg-sky-50 text-sky-700">
                <Landmark size={18} aria-hidden="true" />
              </span>
              <div>
                <div className="text-sm text-muted-foreground">Операций за последние 7 дней</div>
                <div className="text-2xl font-semibold tabular-nums">
                  {recentOperationsQuery.data?.total ?? "—"}
                </div>
              </div>
            </div>
            <ArrowRight size={18} aria-hidden="true" />
          </div>
        </button>
      </div>

      {!walletsQuery.isLoading && wallets.length === 0 ? (
        <div className="flex items-center gap-2 rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
          <Wallet size={16} aria-hidden="true" />
          Кошельки пока не найдены.
        </div>
      ) : null}
    </div>
  );
}
