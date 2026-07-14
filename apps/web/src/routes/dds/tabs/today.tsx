import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertCircle, ArrowRight, Banknote, Coins, Lock } from "lucide-react";

import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { usePermissions } from "@/lib/permissions";
import { getDdsBankOperations, getDdsOwnerReview, getDdsWallets, type WalletRead } from "@/lib/api";
import { KassaTargetsDialog } from "@/routes/dds/KassaTargetsDialog";
import { SafeAccountDialog } from "@/routes/dds/SafeAccountDialog";
import { BankSyncButton, formatDdsMoney } from "@/routes/dds/shared";

type WalletGroupKey = "tbank" | "sber" | "cash" | "reserve";

const isBankWallet = (wallet: WalletRead) =>
  wallet.type === "bank" || wallet.type === "bank_account";

const GROUP_CONFIG: Array<{
  key: WalletGroupKey;
  label: string;
  description: string;
  icon: typeof Banknote;
  accent: string;
  badgeClass: string;
  matches: (wallet: WalletRead) => boolean;
}> = [
  {
    key: "tbank",
    label: "Тинькофф",
    description: "Счета в Т-Банке",
    icon: Banknote,
    accent: "from-sky-50 to-sky-100/60",
    badgeClass: "border-sky-200 bg-sky-50 text-sky-700",
    matches: (wallet) => isBankWallet(wallet) && wallet.bank_code === "tbank",
  },
  {
    key: "sber",
    label: "Сбербанк",
    description: "Счета в Сбербанке",
    icon: Banknote,
    accent: "from-green-50 to-green-100/60",
    badgeClass: "border-green-200 bg-green-50 text-green-700",
    matches: (wallet) => isBankWallet(wallet) && wallet.bank_code === "sber",
  },
  {
    key: "cash",
    label: "Кассы",
    description: "Наличные у администраторов и в сейфе",
    icon: Coins,
    accent: "from-amber-50 to-amber-100/60",
    badgeClass: "border-amber-200 bg-amber-50 text-amber-700",
    matches: (wallet) =>
      wallet.type === "cash" ||
      wallet.type === "cash_safe" ||
      wallet.type === "cash_register" ||
      wallet.type === "store_cash",
  },
  {
    key: "reserve",
    label: "Резервы",
    description: "Обязательства перед сотрудниками и фонды",
    icon: Lock,
    accent: "from-slate-50 to-slate-100/60",
    badgeClass: "border-slate-200 bg-slate-100 text-slate-700",
    matches: (wallet) =>
      wallet.type === "reserve" || wallet.type === "fund" || wallet.type === "deposit",
  },
];

const TYPE_LABELS: Record<string, string> = {
  bank: "Банк",
  bank_account: "Банк",
  cash: "Касса",
  cash_safe: "Сейф",
  cash_register: "Касса",
  store_cash: "Торговая касса",
  reserve: "Резерв",
  fund: "Фонд",
  deposit: "Депозит",
};

export function TodayTab({ onNavigate }: { onNavigate: (path: string) => void }) {
  const permissions = usePermissions();
  const canOpenOwnerReview = permissions.hasAnyPermission([
    "finance.owner_review.prepare",
    "finance.cashflow.classify",
  ]);
  // Вторые двери: карточка Сейфа и read-only «Целевые в Торговой кассе».
  const [safeWallet, setSafeWallet] = useState<WalletRead | null>(null);
  const [kassaTargetsOpen, setKassaTargetsOpen] = useState(false);

  // Сейф → его карточка (вторая дверь, путь через «Счета» остаётся);
  // Торговая касса → read-only «Целевые в Торговой кассе» (выдаёт админ в Кассе).
  const handleWalletClick = (wallet: WalletRead) => {
    if (wallet.type === "cash_safe") {
      setSafeWallet(wallet);
    } else if (wallet.pending_payout_count !== null) {
      setKassaTargetsOpen(true);
    }
  };
  const walletsQuery = useQuery({ queryKey: ["dds", "wallets"], queryFn: getDdsWallets });
  const ownerReviewQuery = useQuery({
    queryKey: ["dds", "owner-review", "today"],
    queryFn: () => getDdsOwnerReview({ limit: 1, offset: 0 }),
    enabled: canOpenOwnerReview,
  });
  const lastOperationQuery = useQuery({
    queryKey: ["dds", "operations", "latest"],
    queryFn: () => getDdsBankOperations({ limit: 1, offset: 0 }),
  });

  const wallets = walletsQuery.data ?? [];

  const grandTotal = useMemo(
    () => wallets.reduce((sum, w) => sum + numeric(w.balance), 0),
    [wallets],
  );

  const groups = useMemo(() => {
    const used = new Set<string>();
    const buckets = GROUP_CONFIG.map((group) => {
      const items = wallets.filter((w) => {
        if (used.has(w.id)) return false;
        if (!group.matches(w)) return false;
        used.add(w.id);
        return true;
      });
      const total = items.reduce((sum, w) => sum + numeric(w.balance), 0);
      return { ...group, items, total };
    });
    const orphans = wallets.filter((w) => !used.has(w.id));
    return { buckets, orphans };
  }, [wallets]);

  const lastImport = lastOperationQuery.data?.items?.[0];
  const lastImportLabel = lastImport
    ? `Последняя операция в банке: ${formatDate(lastImport.operation_date)}`
    : "Импорт ещё не запускался";

  const pendingReview = canOpenOwnerReview ? (ownerReviewQuery.data?.total ?? 0) : 0;

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 className="text-lg font-semibold tracking-normal">Деньги сегодня</h2>
          <p className="text-sm text-muted-foreground">{lastImportLabel}</p>
        </div>
        <BankSyncButton />
      </div>

      <Card className="border-emerald-100 bg-gradient-to-br from-emerald-50 to-emerald-100/40">
        <CardContent className="grid gap-3 p-5 sm:flex sm:items-end sm:justify-between">
          <div>
            <div className="text-sm font-medium text-emerald-900/80">Всего на руках</div>
            <div className="mt-1 text-4xl font-semibold tabular-nums text-emerald-950">
              {formatDdsMoney(grandTotal)}
            </div>
            <div className="mt-2 text-xs text-emerald-900/70">
              По {wallets.length} {pluralize(wallets.length, "кошельку", "кошелькам", "кошелькам")}{" "}
              — все счета и кассы вместе.
            </div>
          </div>
          {pendingReview > 0 ? (
            <button
              type="button"
              onClick={() => onNavigate("/dds/owner-review")}
              className="inline-flex items-center gap-2 self-start rounded-md border border-orange-200 bg-orange-50 px-3 py-2 text-sm text-orange-800 transition hover:bg-orange-100 sm:self-end"
            >
              <AlertCircle size={16} aria-hidden="true" />
              Требует разбора: <span className="font-semibold tabular-nums">{pendingReview}</span>
              <ArrowRight size={14} aria-hidden="true" />
            </button>
          ) : null}
        </CardContent>
      </Card>

      <div className="grid gap-3 lg:grid-cols-3">
        {groups.buckets
          .filter((group) => walletsQuery.isLoading || group.items.length > 0)
          .map((group) => (
            <GroupCard
              key={group.key}
              group={group}
              loading={walletsQuery.isLoading}
              onWalletClick={handleWalletClick}
            />
          ))}
      </div>

      {groups.orphans.length > 0 ? (
        <Card>
          <CardContent className="grid gap-2 p-4">
            <div className="text-sm font-medium">Прочее</div>
            {groups.orphans.map((wallet) => (
              <WalletRow key={wallet.id} wallet={wallet} onClick={handleWalletClick} />
            ))}
          </CardContent>
        </Card>
      ) : null}

      {!walletsQuery.isLoading && wallets.length === 0 ? (
        <div className="rounded-lg border border-dashed p-6 text-sm text-muted-foreground">
          Кошельки пока не найдены. Откройте «Доступы» и подключите банки или добавьте кассу.
        </div>
      ) : null}

      <SafeAccountDialog
        wallet={safeWallet}
        open={Boolean(safeWallet)}
        onClose={() => setSafeWallet(null)}
      />

      <KassaTargetsDialog open={kassaTargetsOpen} onClose={() => setKassaTargetsOpen(false)} />
    </div>
  );
}

function GroupCard({
  group,
  loading,
  onWalletClick,
}: {
  group: (typeof GROUP_CONFIG)[number] & { items: WalletRead[]; total: number };
  loading: boolean;
  onWalletClick: (wallet: WalletRead) => void;
}) {
  const Icon = group.icon;
  return (
    <Card className={cn("overflow-hidden border bg-gradient-to-br", group.accent)}>
      <CardContent className="grid gap-3 p-4">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <span className="flex h-8 w-8 items-center justify-center rounded-md border bg-background/80">
              <Icon size={16} aria-hidden="true" />
            </span>
            <div>
              <div className="text-sm font-medium">{group.label}</div>
              <div className="text-xs text-muted-foreground">{group.description}</div>
            </div>
          </div>
        </div>
        <div className="text-2xl font-semibold tabular-nums">{formatDdsMoney(group.total)}</div>
        <div className="grid gap-1 border-t pt-3">
          {loading ? (
            <div className="h-10 animate-pulse rounded bg-muted/60" />
          ) : group.items.length > 0 ? (
            group.items.map((wallet) => (
              <WalletRow key={wallet.id} wallet={wallet} onClick={onWalletClick} />
            ))
          ) : (
            <div className="text-xs text-muted-foreground">Нет кошельков в группе.</div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function WalletRow({
  wallet,
  onClick,
}: {
  wallet: WalletRead;
  onClick?: (wallet: WalletRead) => void;
}) {
  // Кликабельны Сейф (карточка резервов) и Торговая касса (целевые «К выдаче»).
  const isSafe = wallet.type === "cash_safe";
  const hasTargets = wallet.reserved_total !== null;
  const clickable = Boolean(onClick) && (isSafe || wallet.pending_payout_count !== null);
  return (
    <div
      className={cn(
        "flex items-center justify-between gap-3 text-sm",
        clickable && "-mx-2 cursor-pointer rounded px-2 py-1 transition hover:bg-background/70",
      )}
      onClick={clickable ? () => onClick?.(wallet) : undefined}
      role={clickable ? "button" : undefined}
      tabIndex={clickable ? 0 : undefined}
      onKeyDown={
        clickable
          ? (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onClick?.(wallet);
              }
            }
          : undefined
      }
    >
      <div className="min-w-0">
        <div className="truncate font-medium">{wallet.name}</div>
        <div className="text-xs text-muted-foreground">
          {TYPE_LABELS[wallet.type] ?? wallet.type}
        </div>
      </div>
      <div className="text-right">
        <div className="tabular-nums font-medium">{formatDdsMoney(wallet.balance)}</div>
        {hasTargets ? (
          // Сейф: «свободно X · целевые Y»; Торговая касса: + «к выдаче N».
          <div className="text-xs tabular-nums text-muted-foreground">
            свободно {formatDdsMoney(wallet.free_total)} ·{" "}
            <span className={numeric(wallet.reserved_total) > 0 ? "text-amber-600" : undefined}>
              целевые {formatDdsMoney(wallet.reserved_total)}
            </span>
            {wallet.pending_payout_count !== null && wallet.pending_payout_count > 0 ? (
              <> · к выдаче {wallet.pending_payout_count}</>
            ) : null}
          </div>
        ) : numeric(wallet.opening_balance) > 0 ? (
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
            нач. {formatDdsMoney(wallet.opening_balance)}
          </div>
        ) : null}
      </div>
    </div>
  );
}

function numeric(value: string | number | null | undefined) {
  if (value == null) return 0;
  const n = typeof value === "string" ? Number(value) : value;
  return Number.isFinite(n) ? n : 0;
}

function formatDate(iso: string) {
  const [y, m, d] = iso.split("-");
  return `${d}.${m}.${y}`;
}

function pluralize(n: number, one: string, few: string, many: string) {
  const mod100 = n % 100;
  const mod10 = n % 10;
  if (mod100 >= 11 && mod100 <= 14) return many;
  if (mod10 === 1) return one;
  if (mod10 >= 2 && mod10 <= 4) return few;
  return many;
}
