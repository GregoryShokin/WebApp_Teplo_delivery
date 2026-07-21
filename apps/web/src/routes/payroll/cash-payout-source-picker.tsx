import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo } from "react";

import { getRunFundingSources, type PayrollCashWalletCode } from "@/lib/api";
import { cn } from "@/lib/utils";
import { formatMoney } from "./runs";

export type PayrollCashChannelPerms = { safe: boolean; cash_tk: boolean };

const SOURCES: Array<{
  code: PayrollCashWalletCode;
  label: string;
  permission: keyof PayrollCashChannelPerms;
}> = [
  { code: "tk_chernikova", label: "Торговая касса", permission: "cash_tk" },
  { code: "cash_safe", label: "Сейф", permission: "safe" },
];

export function CashPayoutSourcePicker({
  amount,
  channelPerms,
  onCanSubmitChange,
  onChange,
  runId,
  value,
}: {
  amount: number;
  channelPerms: PayrollCashChannelPerms;
  onCanSubmitChange: (canSubmit: boolean) => void;
  onChange: (code: PayrollCashWalletCode) => void;
  runId: string;
  value: PayrollCashWalletCode | null;
}) {
  const fundingQuery = useQuery({
    queryKey: ["run-funding-sources", runId],
    queryFn: () => getRunFundingSources(runId),
  });
  const byCode = useMemo(
    () => new Map((fundingQuery.data?.cash_sources ?? []).map((source) => [source.code, source])),
    [fundingQuery.data?.cash_sources],
  );
  const selected = value ? byCode.get(value) : null;
  const selectedAvailable = Number(selected?.payroll_available ?? 0);
  const canSubmit = Boolean(
    value &&
    selected &&
    Number.isFinite(selectedAvailable) &&
    channelPerms[value === "cash_safe" ? "safe" : "cash_tk"] &&
    selectedAvailable + 0.001 >= amount,
  );

  useEffect(() => {
    onCanSubmitChange(canSubmit);
  }, [canSubmit, onCanSubmitChange]);

  useEffect(() => {
    if (value || !fundingQuery.data) return;
    const first = SOURCES.find(({ code, permission }) => {
      const source = byCode.get(code);
      const available = Number(source?.payroll_available ?? 0);
      return (
        channelPerms[permission] &&
        source &&
        Number.isFinite(available) &&
        available + 0.001 >= amount
      );
    });
    if (first) onChange(first.code);
  }, [amount, byCode, channelPerms, fundingQuery.data, onChange, value]);

  return (
    <div className="space-y-2">
      <div className="text-sm font-medium">Счёт выплаты</div>
      <div className="grid gap-2 sm:grid-cols-2">
        {SOURCES.map(({ code, label, permission }) => {
          const source = byCode.get(code);
          const permitted = channelPerms[permission];
          const available = Number(source?.payroll_available ?? 0);
          const sufficient = Boolean(
            source && Number.isFinite(available) && available + 0.001 >= amount,
          );
          const disabled = fundingQuery.isLoading || !permitted || !sufficient;
          return (
            <button
              className={cn(
                "rounded-md border p-3 text-left transition-colors",
                value === code ? "border-emerald-500 bg-emerald-50" : "border-border",
                disabled ? "cursor-not-allowed opacity-55" : "hover:bg-muted/50",
              )}
              disabled={disabled}
              key={code}
              onClick={() => onChange(code)}
              type="button"
            >
              <div className="text-sm font-medium">{source?.name || label}</div>
              <div className="mt-1 text-xs text-muted-foreground">
                {fundingQuery.isLoading
                  ? "Загрузка резерва…"
                  : !permitted
                    ? "Нет права на этот счёт"
                    : source
                      ? Number(source.reserved_for_run) > 0
                        ? `Резерв этой ведомости ${formatMoney(source.reserved_for_run)}`
                        : "Нет резерва этой ведомости"
                      : "Счёт недоступен"}
              </div>
              {source && Number(source.reserved_for_run) > available + 0.001 ? (
                <div className="mt-1 text-xs text-destructive">
                  Фактически доступно {formatMoney(available)}
                </div>
              ) : null}
              {source && permitted && !sufficient ? (
                <div className="mt-1 text-xs text-destructive">
                  Не хватает {formatMoney(Math.max(0, amount - available))}
                </div>
              ) : null}
            </button>
          );
        })}
      </div>
      {fundingQuery.isError ? (
        <p className="text-xs text-destructive">Не удалось получить остатки по счетам.</p>
      ) : null}
      {!fundingQuery.isLoading && !canSubmit ? (
        <p className="text-xs text-muted-foreground">
          Выберите счёт, на котором под эту ведомость зарезервировано не менее {formatMoney(amount)}
          .
        </p>
      ) : null}
    </div>
  );
}
