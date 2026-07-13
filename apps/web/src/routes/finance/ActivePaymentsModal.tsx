import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeftRight,
  Banknote,
  Clock,
  Loader2,
  Plus,
  RefreshCw,
  Search,
  Send,
  ShieldCheck,
  Wallet,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { usePermissions } from "@/lib/permissions";
import { cn } from "@/lib/utils";
import { NewPaymentDialog } from "@/routes/dds/NewPaymentDialog";
import { navigateTo } from "@/router";

import { PayPayrollReserveDialog } from "./PayPayrollReserveDialog";
import { BUCKET_ORDER, getPayments, type PaymentRow } from "./payments-api";

const money = new Intl.NumberFormat("ru-RU", {
  style: "currency",
  currency: "RUB",
  maximumFractionDigits: 0,
});

// Оформление каждой корзины: иконка + семантический цвет (палитра проекта:
// border-{c}-200 bg-{c}-50 text-{c}-700). Классы записаны целиком — Tailwind JIT
// не собирает имена из шаблонов.
type BucketStyle = { icon: LucideIcon; chip: string; accent: string; pill: string };

const BUCKET_STYLE: Record<string, BucketStyle> = {
  to_review: {
    icon: Search,
    chip: "bg-amber-100 text-amber-700",
    accent: "bg-amber-400",
    pill: "border-amber-300 bg-amber-50 text-amber-800",
  },
  to_pay: {
    icon: Clock,
    chip: "bg-emerald-100 text-emerald-700",
    accent: "bg-emerald-500",
    pill: "border-emerald-300 bg-emerald-50 text-emerald-800",
  },
  bank_ready: {
    icon: Send,
    chip: "bg-sky-100 text-sky-700",
    accent: "bg-sky-500",
    pill: "border-sky-300 bg-sky-50 text-sky-800",
  },
  reserved_safe: {
    icon: ShieldCheck,
    chip: "bg-violet-100 text-violet-700",
    accent: "bg-violet-500",
    pill: "border-violet-300 bg-violet-50 text-violet-800",
  },
  reserved_kassa: {
    icon: Banknote,
    chip: "bg-teal-100 text-teal-700",
    accent: "bg-teal-500",
    pill: "border-teal-300 bg-teal-50 text-teal-800",
  },
};

// Короткие подписи для пилюль-фильтров (полные — в заголовках секций).
const PILL_LABEL: Record<string, string> = {
  to_review: "Требуют разбора",
  to_pay: "Отправлен в банк",
  bank_ready: "К отправке в банк",
  reserved_safe: "На Сейфе",
  reserved_kassa: "В кассе",
};

// Подпись способа оплаты — только когда осмысленна (нал/Сбер); Т-Банк по умолчанию.
function methodHint(row: PaymentRow): string | null {
  if (row.method === "cash") return "Наличными / Сейф";
  if (row.bank_channel === "sber") return "Сбербанк";
  return null;
}

export function ActivePaymentsModal({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const permissions = usePermissions();
  // Кнопка «Создать» — только при праве хотя бы одного маршрута окна (иначе пустое окно).
  const canCreate = permissions.canPerformAction("payments.create");
  const [createOpen, setCreateOpen] = useState(false);
  const [sendingId, setSendingId] = useState<string | null>(null);
  const [payRow, setPayRow] = useState<PaymentRow | null>(null);
  const [payrollRow, setPayrollRow] = useState<PaymentRow | null>(null);
  const [search, setSearch] = useState("");
  const [bucketFilter, setBucketFilter] = useState<string>("all");

  async function refetchAll() {
    await refetch();
    await queryClient.invalidateQueries({ queryKey: ["finance-payments"] });
  }

  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["finance-payments", "active"],
    queryFn: () => getPayments("active"),
    enabled: open,
  });

  const items = data?.items ?? [];
  const buckets = data?.buckets ?? [];
  const totalSum = items.reduce((acc, row) => acc + row.amount, 0);

  const q = search.trim().toLowerCase();
  const visible = useMemo(() => {
    return items.filter((row) => {
      if (bucketFilter !== "all" && row.bucket !== bucketFilter) return false;
      if (!q) return true;
      return [row.title, row.counterparty_name, row.article_name]
        .filter(Boolean)
        .some((v) => (v as string).toLowerCase().includes(q));
    });
  }, [items, bucketFilter, q]);

  async function sendToBank(row: PaymentRow) {
    setSendingId(row.id);
    try {
      await api.post(`/payment-page/intakes/${row.ref_id}/send-to-bank`, {});
      toast.success("Счёт отправлен в банк");
      await refetch();
      await queryClient.invalidateQueries({ queryKey: ["finance-payments"] });
    } catch (error) {
      const detail =
        (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Не удалось отправить в банк";
      toast.error(detail);
    } finally {
      setSendingId(null);
    }
  }

  function goReview() {
    onOpenChange(false);
    navigateTo("/payment-page");
  }

  function rowAction(row: PaymentRow) {
    if (row.can_send_to_bank) {
      return (
        <Button
          size="sm"
          variant="outline"
          className="h-8"
          onClick={() => sendToBank(row)}
          disabled={sendingId === row.id}
        >
          {sendingId === row.id ? (
            <Loader2 className="animate-spin" size={14} />
          ) : (
            <>
              <Send size={14} className="mr-1.5" /> В банк
            </>
          )}
        </Button>
      );
    }
    if (row.bucket === "to_review") {
      return (
        <Button size="sm" variant="outline" className="h-8" onClick={goReview}>
          Разобрать
        </Button>
      );
    }
    // Резерв-контейнер выплаты ЗП: открывает список сотрудников ведомости (раскладка пула).
    if (row.kind === "payroll_reserve" && row.can_pay) {
      return (
        <Button size="sm" variant="outline" className="h-8" onClick={() => setPayrollRow(row)}>
          Выплатить ЗП
        </Button>
      );
    }
    if (row.can_pay) {
      return (
        <Button size="sm" variant="outline" className="h-8" onClick={() => setPayRow(row)}>
          {row.bucket === "reserved_kassa" ? "Выдать" : "Выплатить"}
        </Button>
      );
    }
    return null;
  }

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="flex max-h-[86vh] max-w-2xl flex-col gap-0 overflow-hidden p-0">
          <DialogHeader className="shrink-0 space-y-0 border-b py-4 pl-6 pr-14">
            <DialogTitle className="text-lg">Активные платежи</DialogTitle>
            <DialogDescription className="mt-0.5">
              {isLoading
                ? "Загрузка…"
                : `${items.length} шт · ${money.format(totalSum)} — ждут оплаты, отправки или выдачи`}
            </DialogDescription>
          </DialogHeader>

          {/* Панель фильтров — не скроллится вместе со списком */}
          <div className="shrink-0 space-y-2.5 border-b px-6 py-3">
            <div className="flex items-center gap-2">
              <div className="relative flex-1">
                <Search
                  className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground"
                  size={15}
                />
                <Input
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="Поиск по получателю или статье…"
                  className="h-9 pl-8"
                />
              </div>
              <Button
                size="icon"
                variant="ghost"
                className="h-9 w-9 shrink-0"
                onClick={() => refetch()}
                disabled={isFetching}
                title="Обновить"
              >
                <RefreshCw className={cn("h-4 w-4", isFetching && "animate-spin")} />
              </Button>
              {canCreate ? (
                <Button size="sm" className="h-9 shrink-0" onClick={() => setCreateOpen(true)}>
                  <Plus size={16} className="mr-1" /> Создать
                </Button>
              ) : null}
            </div>

            <div className="flex gap-1.5 overflow-x-auto pb-0.5">
              <FilterPill
                active={bucketFilter === "all"}
                onClick={() => setBucketFilter("all")}
                label="Все"
                count={items.length}
              />
              {BUCKET_ORDER.map((key) => {
                const meta = buckets.find((b) => b.key === key);
                if (!meta || meta.count === 0) return null;
                return (
                  <FilterPill
                    key={key}
                    active={bucketFilter === key}
                    activeClass={BUCKET_STYLE[key].pill}
                    onClick={() => setBucketFilter(key)}
                    label={PILL_LABEL[key]}
                    count={meta.count}
                  />
                );
              })}
            </div>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto [max-height:calc(86vh-11rem)]">
            <div className="space-y-6 px-6 py-5">
              {isLoading ? (
                <div className="flex items-center justify-center py-16 text-muted-foreground">
                  <Loader2 className="mr-2 animate-spin" size={18} /> Загрузка…
                </div>
              ) : visible.length === 0 ? (
                <div className="flex flex-col items-center gap-3 py-16 text-center">
                  <div className="flex h-12 w-12 items-center justify-center rounded-full bg-muted">
                    <Wallet className="text-muted-foreground" size={22} />
                  </div>
                  <div className="text-sm text-muted-foreground">
                    {q || bucketFilter !== "all"
                      ? "Ничего не найдено по фильтру."
                      : "Активных платежей нет — всё оплачено."}
                  </div>
                  {!q && bucketFilter === "all" && canCreate ? (
                    <Button size="sm" variant="outline" onClick={() => setCreateOpen(true)}>
                      <Plus size={16} className="mr-1" /> Создать платёж
                    </Button>
                  ) : null}
                </div>
              ) : (
                BUCKET_ORDER.map((key) => {
                  const rows = visible.filter((row) => row.bucket === key);
                  if (rows.length === 0) return null;
                  const meta = buckets.find((b) => b.key === key);
                  const style = BUCKET_STYLE[key];
                  const Icon = style.icon;
                  const sum = rows.reduce((acc, row) => acc + row.amount, 0);
                  return (
                    <section key={key}>
                      <header className="mb-2 flex items-center gap-2.5">
                        <span
                          className={cn(
                            "flex h-7 w-7 items-center justify-center rounded-full",
                            style.chip,
                          )}
                        >
                          <Icon size={15} />
                        </span>
                        <h3 className="text-sm font-semibold">{meta?.label ?? key}</h3>
                        <span className="text-xs text-muted-foreground">· {rows.length}</span>
                        <span className="ml-auto text-xs font-medium text-muted-foreground tabular-nums">
                          {money.format(sum)}
                        </span>
                      </header>
                      <div className="overflow-hidden rounded-lg border">
                        {rows.map((row, index) => {
                          const hint = methodHint(row);
                          return (
                            <div
                              key={row.id}
                              className={cn(
                                "flex items-center gap-3 px-3.5 py-2.5",
                                index > 0 && "border-t",
                              )}
                            >
                              <span
                                className={cn("h-8 w-1 shrink-0 rounded-full", style.accent)}
                                aria-hidden="true"
                              />
                              <div className="min-w-0 flex-1">
                                <div className="truncate text-sm font-medium">{row.title}</div>
                                <div className="mt-0.5 truncate text-xs text-muted-foreground">
                                  {row.article_name ?? "Без статьи"}
                                  {hint ? ` · ${hint}` : ""}
                                  {row.amount_paid && row.amount_paid > 0
                                    ? ` · выплачено ${money.format(row.amount_paid)}`
                                    : ""}
                                </div>
                              </div>
                              <div className="shrink-0 text-right text-sm font-semibold tabular-nums">
                                {money.format(row.amount)}
                              </div>
                              <div className="w-[92px] shrink-0 text-right">{rowAction(row)}</div>
                            </div>
                          );
                        })}
                      </div>
                    </section>
                  );
                })
              )}
            </div>
          </div>
        </DialogContent>
      </Dialog>

      <NewPaymentDialog
        open={createOpen}
        onOpenChange={(next) => {
          setCreateOpen(next);
          if (!next) {
            refetch();
          }
        }}
        presetArticleCode={null}
      />

      <PayReserveDialog
        row={payRow}
        onOpenChange={(next) => {
          if (!next) setPayRow(null);
        }}
        onPaid={refetchAll}
      />

      <PayPayrollReserveDialog
        row={payrollRow}
        onOpenChange={(next) => {
          if (!next) setPayrollRow(null);
        }}
        onPaid={refetchAll}
      />
    </>
  );
}

// Диалог выплаты резерва с кастомной суммой. Сейф → /dds/allocations/{id}/pay,
// Касса → /kassa/targets/{id}/payout. Обе ручки допускают частичную выплату
// (сумма ≤ остатка); дефолт — весь остаток.
function PayReserveDialog({
  row,
  onOpenChange,
  onPaid,
}: {
  row: PaymentRow | null;
  onOpenChange: (open: boolean) => void;
  onPaid: () => void | Promise<void>;
}) {
  const [amount, setAmount] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [confirmWriteOff, setConfirmWriteOff] = useState(false);
  const [writingOff, setWritingOff] = useState(false);
  const [moving, setMoving] = useState(false);

  const paid = row?.amount_paid ?? 0;
  const remaining = row ? row.amount - paid : 0;
  const isKassa = row?.bucket === "reserved_kassa";
  const where = isKassa ? "в кассе" : "на Сейфе";

  useEffect(() => {
    if (row) setAmount(String(remaining));
    setConfirmWriteOff(false);
    // сбрасываем сумму/подтверждение при смене резерва
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [row?.id]);

  const value = Number(amount.replace(",", ".").replace(/\s/g, ""));
  const valid = value > 0 && value <= remaining + 0.01;

  async function submit() {
    if (!row || !valid) return;
    setSubmitting(true);
    try {
      const url = isKassa
        ? `/kassa/targets/${row.ref_id}/payout`
        : `/dds/allocations/${row.ref_id}/pay`;
      await api.post(url, { amount: value });
      toast.success(isKassa ? "Выдано из кассы" : "Выплачено с Сейфа");
      await onPaid();
      onOpenChange(false);
    } catch (error) {
      const detail =
        (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Не удалось провести выплату";
      toast.error(detail);
    } finally {
      setSubmitting(false);
    }
  }

  // Списать остаток: снять резерв, неоплаченный остаток освобождается (деньги
  // остаются на счёте свободными). Та же ручка для Сейфа и Кассы.
  async function writeOff() {
    if (!row) return;
    setWritingOff(true);
    try {
      await api.post(`/dds/allocations/${row.ref_id}/cancel`, {});
      toast.success(`Остаток списан — ${money.format(remaining)} освобождено ${where}`);
      await onPaid();
      onOpenChange(false);
    } catch (error) {
      const detail =
        (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Не удалось списать остаток";
      toast.error(detail);
    } finally {
      setWritingOff(false);
    }
  }

  // Переместить резерв между Сейфом и Кассой (весь остаток). Одна ручка обе стороны.
  async function move() {
    if (!row) return;
    const toLocation = isKassa ? "safe" : "kassa";
    setMoving(true);
    try {
      await api.post(`/dds/allocations/${row.ref_id}/move`, { to_location: toLocation });
      toast.success(isKassa ? "Резерв возвращён на Сейф" : "Резерв передан в Кассу");
      await onPaid();
      onOpenChange(false);
    } catch (error) {
      const detail =
        (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Не удалось переместить резерв";
      toast.error(detail);
    } finally {
      setMoving(false);
    }
  }

  const busy = submitting || writingOff || moving;

  return (
    <Dialog open={row !== null} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{isKassa ? "Выдать из кассы" : "Выплатить с Сейфа"}</DialogTitle>
          <DialogDescription>
            {row?.title}
            {row?.article_name ? ` · ${row.article_name}` : ""}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="flex justify-between rounded-md bg-muted/50 px-3 py-2 text-sm">
            <span className="text-muted-foreground">Резерв</span>
            <span className="font-medium tabular-nums">{money.format(row?.amount ?? 0)}</span>
          </div>
          {paid > 0 ? (
            <div className="flex justify-between px-3 text-sm">
              <span className="text-muted-foreground">Уже выплачено</span>
              <span className="tabular-nums">{money.format(paid)}</span>
            </div>
          ) : null}
          <div className="flex justify-between px-3 text-sm">
            <span className="text-muted-foreground">Остаток</span>
            <span className="font-medium tabular-nums">{money.format(remaining)}</span>
          </div>

          <div className="space-y-1.5">
            <div className="flex items-center justify-between">
              <label className="text-sm font-medium" htmlFor="pay-amount">
                Сумма выплаты
              </label>
              <button
                type="button"
                className="text-xs text-primary hover:underline"
                onClick={() => setAmount(String(remaining))}
              >
                Весь остаток
              </button>
            </div>
            <Input
              id="pay-amount"
              inputMode="decimal"
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              className="tabular-nums"
            />
            {amount && !valid ? (
              <p className="text-xs text-rose-600">
                Сумма должна быть больше 0 и не больше остатка {money.format(remaining)}.
              </p>
            ) : null}
          </div>
        </div>

        <div className="flex items-center justify-between gap-2 rounded-md border p-3">
          <span className="text-xs text-muted-foreground">
            {isKassa ? "Вернуть резерв на карту Сейф" : "Передать резерв в кассу для выдачи"}
          </span>
          <Button
            size="sm"
            variant="outline"
            className="h-8 shrink-0"
            onClick={move}
            disabled={busy}
          >
            {moving ? (
              <Loader2 className="animate-spin" size={14} />
            ) : (
              <>
                <ArrowLeftRight size={14} className="mr-1.5" />
                {isKassa ? "Вернуть на Сейф" : "Передать в Кассу"}
              </>
            )}
          </Button>
        </div>

        <div className="rounded-md border border-dashed p-3">
          {confirmWriteOff ? (
            <div className="space-y-2">
              <p className="text-sm">
                Списать остаток {money.format(remaining)}? Резерв снимется, деньги останутся
                свободными {where} — платёж не проводится.
              </p>
              <div className="flex gap-2">
                <Button
                  size="sm"
                  variant="destructive"
                  onClick={writeOff}
                  disabled={writingOff}
                >
                  {writingOff ? <Loader2 className="mr-1.5 animate-spin" size={14} /> : null}
                  Списать {money.format(remaining)}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => setConfirmWriteOff(false)}
                  disabled={writingOff}
                >
                  Отмена
                </Button>
              </div>
            </div>
          ) : (
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs text-muted-foreground">
                Не платить — снять резерв, освободить остаток.
              </span>
              <button
                type="button"
                className="shrink-0 text-sm font-medium text-rose-600 hover:underline"
                onClick={() => setConfirmWriteOff(true)}
              >
                Списать остаток
              </button>
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={busy}>
            Отмена
          </Button>
          <Button onClick={submit} disabled={!valid || busy}>
            {submitting ? (
              <Loader2 className="mr-1.5 animate-spin" size={15} />
            ) : null}
            {isKassa ? "Выдать" : "Выплатить"} {valid ? money.format(value) : ""}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function FilterPill({
  active,
  activeClass,
  onClick,
  label,
  count,
}: {
  active: boolean;
  activeClass?: string;
  onClick: () => void;
  label: string;
  count: number;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "flex shrink-0 items-center gap-1 whitespace-nowrap rounded-full border px-2.5 py-1 text-xs font-medium transition-colors",
        active
          ? (activeClass ?? "border-primary bg-primary text-primary-foreground")
          : "border-input bg-background text-muted-foreground hover:bg-muted",
      )}
    >
      {label}
      <span className={cn("tabular-nums", active ? "opacity-80" : "text-foreground/70")}>
        {count}
      </span>
    </button>
  );
}
