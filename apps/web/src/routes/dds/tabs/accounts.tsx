import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { AccountSelect, sortAccounts, useAccountOptions } from "@/components/ui-app/AccountSelect";
import type { WalletRead } from "@/lib/api";
import { SafeAccountDialog } from "@/routes/dds/SafeAccountDialog";
import { formatDdsMoney } from "@/routes/dds/shared";

function accountGroupLabel(wallet: WalletRead): string {
  if (wallet.bank_code === "tbank") return "Тинькофф";
  if (wallet.bank_code === "sber") return "Сбербанк";
  return "Наличные";
}

// 1 целевой, 2/5/11 целевых — резервы Сейфа под конкретных получателей.
function targetedReservesLabel(count: number): string {
  return count % 10 === 1 && count % 100 !== 11 ? "целевой" : "целевых";
}

export function AccountsTab() {
  const [selected, setSelected] = useState<string | null>(null);
  const [safeWallet, setSafeWallet] = useState<WalletRead | null>(null);
  const { data, isLoading } = useAccountOptions();
  const accounts = sortAccounts(data ?? []);
  const picked = accounts.find((account) => account.id === selected) ?? null;

  return (
    <div className="space-y-5">
      <Card>
        <CardHeader className="pb-3">
          <h3 className="text-base font-semibold">Выбор счёта</h3>
          <p className="text-sm text-muted-foreground">
            Единый список счетов. Этот же список используется во всех выборах счёта в приложении.
          </p>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="max-w-sm">
            <AccountSelect value={selected} onChange={setSelected} />
          </div>
          {picked ? (
            <div className="text-sm text-muted-foreground">
              Выбран: <span className="font-medium text-foreground">{picked.name}</span> · остаток{" "}
              <span className="tabular-nums">{formatDdsMoney(picked.balance)}</span>
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between gap-2">
            <h3 className="text-base font-semibold">Все счета</h3>
            <span className="text-sm text-muted-foreground">{accounts.length}</span>
          </div>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <div className="h-10 animate-pulse rounded bg-muted/60" />
          ) : (
            <div className="divide-y">
              {accounts.map((account) => {
                // Именно Сейф: у Торговой кассы теперь тоже есть reserved_total
                // (целёвки), но её карточка — read-only диалог на «Деньгах сегодня».
                const isSafe = account.type === "cash_safe";
                return (
                  <div
                    className={`flex items-center justify-between gap-3 py-2 text-sm ${
                      isSafe ? "-mx-2 cursor-pointer rounded px-2 hover:bg-muted/40" : ""
                    }`}
                    key={account.id}
                    onClick={isSafe ? () => setSafeWallet(account) : undefined}
                    role={isSafe ? "button" : undefined}
                    tabIndex={isSafe ? 0 : undefined}
                    onKeyDown={
                      isSafe
                        ? (event) => {
                            if (event.key === "Enter" || event.key === " ") {
                              event.preventDefault();
                              setSafeWallet(account);
                            }
                          }
                        : undefined
                    }
                  >
                    <div className="min-w-0">
                      <div className="flex min-w-0 items-center gap-2">
                        <div className="truncate font-medium">{account.name}</div>
                        {isSafe && (account.active_allocations ?? 0) > 0 ? (
                          <Badge className="shrink-0 border-amber-200 bg-amber-50 text-amber-700 tabular-nums">
                            {account.active_allocations}{" "}
                            {targetedReservesLabel(account.active_allocations ?? 0)} ·{" "}
                            {formatDdsMoney(account.reserved_total)}
                          </Badge>
                        ) : null}
                      </div>
                      <div className="text-xs text-muted-foreground">
                        {accountGroupLabel(account)}
                        {isSafe ? " · подотчётный, нажмите для деталей" : ""}
                      </div>
                    </div>
                    <div className="text-right">
                      <div className="tabular-nums">{formatDdsMoney(account.balance)}</div>
                      {isSafe ? (
                        <div className="text-xs tabular-nums text-muted-foreground">
                          свободно {formatDdsMoney(account.free_total)} · резерв{" "}
                          {formatDdsMoney(account.reserved_total)}
                        </div>
                      ) : null}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>

      <SafeAccountDialog
        wallet={safeWallet}
        open={Boolean(safeWallet)}
        onClose={() => setSafeWallet(null)}
      />
    </div>
  );
}
