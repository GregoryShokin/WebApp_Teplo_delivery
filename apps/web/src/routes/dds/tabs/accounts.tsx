import { useState } from "react";

import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { AccountSelect, sortAccounts, useAccountOptions } from "@/components/ui-app/AccountSelect";
import type { WalletRead } from "@/lib/api";
import { formatDdsMoney } from "@/routes/dds/shared";

function accountGroupLabel(wallet: WalletRead): string {
  if (wallet.bank_code === "tbank") return "Тинькофф";
  if (wallet.bank_code === "sber") return "Сбербанк";
  return "Наличные";
}

export function AccountsTab() {
  const [selected, setSelected] = useState<string | null>(null);
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
              {accounts.map((account) => (
                <div className="flex items-center justify-between gap-3 py-2 text-sm" key={account.id}>
                  <div className="min-w-0">
                    <div className="truncate font-medium">{account.name}</div>
                    <div className="text-xs text-muted-foreground">{accountGroupLabel(account)}</div>
                  </div>
                  <div className="tabular-nums">{formatDdsMoney(account.balance)}</div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
