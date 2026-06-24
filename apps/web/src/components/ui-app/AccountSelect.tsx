import { useQuery } from "@tanstack/react-query";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { getDdsWallets, type WalletRead } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Единый источник списка счетов (кошельков ДДС) для всего приложения.
 *
 * Все выпадающие списки выбора счёта должны брать данные отсюда: react-query
 * ключ ["dds", "wallets"] дедуплицирует запрос, поэтому источник один — эндпоинт
 * /dds/wallets. Так выбор счёта в любом модуле (депозиты курьеров, оплаты и т.д.)
 * всегда показывает один и тот же актуальный набор счетов.
 */
export function useAccountOptions() {
  return useQuery({ queryKey: ["dds", "wallets"], queryFn: getDdsWallets });
}

/**
 * Накопительные/фондовые счета — НЕ источники выплат: с них напрямую никогда ничего не
 * выплачивается (только внутренние переводы). Скрываем их в селекторах выбора счёта-источника
 * выплаты (авансы/займы и т.п.) через `filter={isPayoutWallet}`; в общем просмотре счетов ДДС
 * они остаются видимыми.
 */
export const NON_PAYOUT_WALLET_CODES = ["tbank_kopilka", "guarant_fund", "tbank_nakopit"];

export const isPayoutWallet = (wallet: WalletRead): boolean =>
  !NON_PAYOUT_WALLET_CODES.includes(wallet.code);

const GROUP_RANK: Record<string, number> = { tbank: 0, sber: 1 };

/** Банки сверху (Тинькофф, затем Сбер), наличные и прочее — ниже; внутри по имени. */
export function sortAccounts(wallets: WalletRead[]): WalletRead[] {
  const rank = (wallet: WalletRead) =>
    wallet.bank_code && wallet.bank_code in GROUP_RANK ? GROUP_RANK[wallet.bank_code] : 2;
  return [...wallets].sort(
    (left, right) => rank(left) - rank(right) || left.name.localeCompare(right.name, "ru"),
  );
}

/**
 * Переиспользуемый выпадающий список со всеми счетами. По умолчанию показывает
 * все активные счета; `filter` позволяет сузить набор под конкретный сценарий
 * (например, только наличные для выдачи депозита курьеру).
 */
export function AccountSelect({
  className,
  disabled = false,
  filter,
  onChange,
  placeholder = "Выберите счёт",
  value,
}: {
  className?: string;
  disabled?: boolean;
  filter?: (wallet: WalletRead) => boolean;
  onChange: (walletId: string) => void;
  placeholder?: string;
  value: string | null | undefined;
}) {
  const { data, isLoading } = useAccountOptions();
  const accounts = sortAccounts(
    (data ?? [])
      .filter((wallet) => wallet.status === "active")
      .filter(filter ?? (() => true)),
  );

  return (
    <Select value={value ?? undefined} onValueChange={onChange} disabled={disabled || isLoading}>
      <SelectTrigger className={cn(className)}>
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent>
        {accounts.map((account) => (
          <SelectItem key={account.id} value={account.id}>
            {account.name}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
