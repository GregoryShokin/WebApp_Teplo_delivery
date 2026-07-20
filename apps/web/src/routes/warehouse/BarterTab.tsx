import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, LoaderCircle } from "lucide-react";
import { toast } from "sonner";

import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { apiErrorMessage, getDdsBankOperations, getDdsWallets } from "@/lib/api";
import { cn } from "@/lib/utils";

import {
  createDraft,
  getBarterDetail,
  getBarterSuggestions,
  settleBarter,
  type BarterInvoice,
} from "../counterparties/api";
import { formatRub } from "../counterparties/shared";
import { CreateInvoiceDialog, num } from "./CreateInvoiceDialog";
import {
  createBarterReturn,
  getBarterPartners,
  getLoanReturnable,
  getOpenLoans,
  getWarehouseInvoices,
  markInvoiceAsLoan,
  settleLoanWithMoney,
  type BarterPartner,
  type OpenLoan,
} from "./api";

// Допуск перевозврата товаром — синхронно с бэком (RETURN_QTY_TOLERANCE): довесок при
// фасовке принимаем, явный перебор отклоняем.
const RETURN_QTY_TOLERANCE = 0.1;

/** Вкладка «Бартер»: партнёры с балансом → карточка с накладными → окно гашения. */
export function BarterPartnersTab({ canOperate }: { canOperate: boolean }) {
  const [openPartner, setOpenPartner] = useState<BarterPartner | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const queryClient = useQueryClient();
  const partnersQuery = useQuery({
    queryKey: ["wh", "barter-partners"],
    queryFn: getBarterPartners,
  });
  const partners = partnersQuery.data ?? [];

  return (
    <div className="space-y-3">
      {canOperate ? (
        <div className="flex justify-end">
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            + Оформить займ
          </Button>
          <CreateInvoiceDialog
            open={createOpen}
            onOpenChange={setCreateOpen}
            barter
            onCreated={() =>
              void queryClient.invalidateQueries({ queryKey: ["wh", "barter-partners"] })
            }
          />
        </div>
      ) : null}
      <div className="overflow-x-auto rounded-md border">
        <table className="w-full min-w-[760px] text-sm">
          <thead>
            <tr className="border-b text-left text-xs uppercase tracking-wide text-muted-foreground">
              <th className="px-3 py-2 font-medium">Партнёр</th>
              <th className="px-3 py-2 text-right font-medium">Нам должны</th>
              <th className="px-3 py-2 text-right font-medium">Мы должны</th>
              <th className="px-3 py-2 text-right font-medium">Нетто</th>
              <th className="px-3 py-2 font-medium">Открыто</th>
              <th className="px-3 py-2 font-medium">Движение</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody>
            {partners.map((p) => (
              <tr
                key={p.counterparty_id}
                className="cursor-pointer border-b last:border-b-0 hover:bg-muted/40"
                onClick={() => setOpenPartner(p)}
              >
                <td className="px-3 py-2.5 font-medium">{p.name}</td>
                <td className="px-3 py-2.5 text-right tabular-nums">
                  {p.we_are_owed > 0 ? (
                    <span className="font-medium text-emerald-700">{formatRub(p.we_are_owed)}</span>
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                </td>
                <td className="px-3 py-2.5 text-right tabular-nums">
                  {p.we_owe > 0 ? (
                    <span className="font-medium text-amber-700">{formatRub(p.we_owe)}</span>
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                </td>
                <td
                  className={cn(
                    "px-3 py-2.5 text-right font-medium tabular-nums",
                    p.net > 0 && "text-emerald-700",
                    p.net < 0 && "text-amber-700",
                    p.net === 0 && "text-muted-foreground",
                  )}
                >
                  {p.net > 0 ? "+" : ""}
                  {formatRub(p.net)}
                </td>
                <td className="px-3 py-2.5">
                  {p.open_loans > 0 ? (
                    <span className="rounded-full bg-emerald-50 px-2.5 py-0.5 text-xs font-medium text-emerald-700">
                      займы: {p.open_loans}
                    </span>
                  ) : p.has_mutual && (p.we_are_owed > 0 || p.we_owe > 0) ? (
                    <span className="rounded-full bg-violet-50 px-2.5 py-0.5 text-xs font-medium text-violet-700">
                      взаимные поставки
                    </span>
                  ) : (
                    <span className="rounded-full bg-muted px-2.5 py-0.5 text-xs text-muted-foreground">
                      рассчитаны
                    </span>
                  )}
                </td>
                <td className="px-3 py-2.5 text-xs text-muted-foreground">
                  {p.last_activity ?? "—"}
                </td>
                <td className="px-2 py-2.5 text-muted-foreground">
                  <ChevronRight size={15} aria-hidden="true" />
                </td>
              </tr>
            ))}
            {!partnersQuery.isLoading && partners.length === 0 ? (
              <tr>
                <td colSpan={7} className="px-3 py-8 text-center text-sm text-muted-foreground">
                  Бартерных партнёров пока нет — оформите первый займ.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
      <p className="text-xs text-muted-foreground">
        Строка — весь бартер с партнёром: займы по остатку (кг × цена выдачи) плюс открытые
        взаимные поставки. Клик открывает карточку с накладными.
      </p>

      <PartnerBarterCard
        partner={openPartner}
        canOperate={canOperate}
        onClose={() => setOpenPartner(null)}
      />
    </div>
  );
}

/** Уровень 2: карточка партнёра — займы и взаимные поставки, клик по строке → гашение. */
function PartnerBarterCard({
  partner,
  canOperate,
  onClose,
}: {
  partner: BarterPartner | null;
  canOperate: boolean;
  onClose: () => void;
}) {
  const cpId = partner?.counterparty_id ?? "";
  const [settleLoan, setSettleLoan] = useState<OpenLoan | null>(null);
  const queryClient = useQueryClient();

  const loansQuery = useQuery({
    queryKey: ["wh", "loans", cpId],
    queryFn: () => getOpenLoans(cpId),
    enabled: !!partner,
  });
  const detailQuery = useQuery({
    queryKey: ["cp", "barter", cpId],
    queryFn: () => getBarterDetail(cpId),
    enabled: !!partner,
  });
  const suggestionsQuery = useQuery({
    queryKey: ["cp", "barter-suggest", cpId],
    queryFn: () => getBarterSuggestions(cpId),
    enabled: !!partner && canOperate,
  });
  // Возвраты по займам: показываем со статусом зеркала iiko (накладные рождаются у нас,
  // iiko только принимает — модель владельца 18.07).
  const invoicesQuery = useQuery({
    queryKey: ["wh", "cp-invoices", cpId],
    queryFn: () => getWarehouseInvoices({ counterparty_id: cpId }),
    enabled: !!partner,
  });
  const returns = (invoicesQuery.data ?? []).filter((i) => i.barter_role === "return");

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["wh", "barter-partners"] });
    void queryClient.invalidateQueries({ queryKey: ["wh", "loans", cpId] });
    void queryClient.invalidateQueries({ queryKey: ["cp", "barter", cpId] });
    void queryClient.invalidateQueries({ queryKey: ["cp", "barter-suggest", cpId] });
    void queryClient.invalidateQueries({ queryKey: ["wh", "cp-invoices", cpId] });
    void queryClient.invalidateQueries({ queryKey: ["wh", "returnable"] });
  };

  const settleMutation = useMutation({
    mutationFn: (s: { payable_ids: string[]; receivable_ids: string[] }) =>
      settleBarter(cpId, s),
    onSuccess: () => {
      toast.success("Зачёт проведён");
      refresh();
    },
    onError: (e) => toast.error(apiErrorMessage(e, "Не удалось провести зачёт")),
  });
  // Мостик для унаследованных строк: признать поставку явным займом → штатное гашение.
  const markLoanMutation = useMutation({
    mutationFn: (invoiceId: string) => markInvoiceAsLoan(invoiceId),
    onSuccess: () => {
      toast.success("Накладная признана займом — гашение доступно в списке займов");
      refresh();
    },
    onError: (e) => toast.error(apiErrorMessage(e, "Не удалось признать займом")),
  });

  const loans = loansQuery.data ?? [];
  const detail = detailQuery.data;
  const suggestions = suggestionsQuery.data ?? [];

  return (
    <>
      <Dialog open={!!partner} onOpenChange={(v) => !v && onClose()}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>{partner?.name}</DialogTitle>
          </DialogHeader>
          {partner ? (
            <div className="space-y-4">
              <div className="flex flex-wrap gap-x-6 gap-y-2 rounded-md bg-muted/50 px-4 py-2.5 text-sm">
                <div>
                  <div className="text-[11px] uppercase tracking-wide text-muted-foreground">
                    Нам должны
                  </div>
                  <div className="font-semibold tabular-nums text-emerald-700">
                    {partner.we_are_owed > 0 ? formatRub(partner.we_are_owed) : "—"}
                  </div>
                </div>
                <div>
                  <div className="text-[11px] uppercase tracking-wide text-muted-foreground">
                    Мы должны
                  </div>
                  <div className="font-semibold tabular-nums text-amber-700">
                    {partner.we_owe > 0 ? formatRub(partner.we_owe) : "—"}
                  </div>
                </div>
                <div>
                  <div className="text-[11px] uppercase tracking-wide text-muted-foreground">
                    Нетто
                  </div>
                  <div className="font-semibold tabular-nums">
                    {partner.net > 0 ? "+" : ""}
                    {formatRub(partner.net)}
                  </div>
                </div>
              </div>

              {loans.length > 0 ? (
                <div className="space-y-2">
                  <div className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                    Займы
                  </div>
                  {loans.map((ln) => (
                    <button
                      key={ln.id}
                      type="button"
                      className="grid w-full grid-cols-[auto_1fr_auto_auto] items-center gap-3 rounded-md border px-3 py-2.5 text-left text-sm hover:bg-muted/40"
                      onClick={() => canOperate && setSettleLoan(ln)}
                    >
                      <span
                        className={cn(
                          "rounded-full px-2.5 py-0.5 text-xs font-medium",
                          ln.we_lend
                            ? "bg-emerald-50 text-emerald-700"
                            : "bg-amber-50 text-amber-700",
                        )}
                      >
                        {ln.we_lend ? "→ мы выдали" : "← нам выдали"}
                      </span>
                      <span className="min-w-0">
                        <span className="block truncate font-medium">
                          №{ln.number ?? "—"} ·{" "}
                          {ln.issued_at ? ln.issued_at.slice(0, 10) : "без даты"}
                        </span>
                        <span className="block text-xs text-muted-foreground">
                          {ln.status === "partially_returned"
                            ? `погашено ${formatRub(ln.returned)} из ${formatRub(ln.amount)}`
                            : `заём на ${formatRub(ln.amount)}`}
                        </span>
                      </span>
                      <span className="text-right">
                        <span className="block font-semibold tabular-nums">
                          {formatRub(ln.remaining)}
                        </span>
                        <span className="block text-[11px] text-muted-foreground">остаток</span>
                      </span>
                      <ChevronRight size={15} className="text-muted-foreground" aria-hidden="true" />
                    </button>
                  ))}
                </div>
              ) : null}

              {returns.length > 0 ? (
                <div className="space-y-2">
                  <div className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                    Возвраты
                  </div>
                  {returns.map((r) => (
                    <div
                      key={r.id}
                      className="grid grid-cols-[auto_1fr_auto] items-center gap-3 rounded-md border px-3 py-2 text-sm"
                    >
                      <span className="rounded-full bg-muted px-2.5 py-0.5 text-xs font-medium text-muted-foreground">
                        возврат
                      </span>
                      <span className="min-w-0">
                        <span className="block truncate font-medium">
                          №{r.number ?? "—"} · {r.invoice_date ?? "без даты"}
                        </span>
                        <span className="block text-xs text-muted-foreground">
                          {r.iiko_push_status === "pushed"
                            ? "отправлен в iiko"
                            : r.iiko_push_status === "failed"
                              ? "не ушёл в iiko — переотправьте из деталей накладной"
                              : "ещё не в iiko"}
                        </span>
                      </span>
                      <span className="text-right font-semibold tabular-nums">
                        {formatRub(r.amount)}
                      </span>
                    </div>
                  ))}
                </div>
              ) : null}

              {suggestions.length > 0 && canOperate ? (
                <div className="space-y-2">
                  {suggestions.map((s, i) => (
                    <div
                      key={i}
                      className="flex flex-wrap items-center justify-between gap-2 rounded-md bg-violet-50 px-3 py-2 text-sm text-violet-900"
                    >
                      <span>
                        Совпадение на <b className="tabular-nums">{formatRub(s.amount)}</b>
                        {s.confident ? " — номенклатура сходится" : " — проверьте состав"}
                      </span>
                      <Button
                        size="sm"
                        variant="secondary"
                        disabled={settleMutation.isPending}
                        onClick={() =>
                          settleMutation.mutate({
                            payable_ids: s.payable_ids,
                            receivable_ids: s.receivable_ids,
                          })
                        }
                      >
                        Зачесть
                      </Button>
                    </div>
                  ))}
                </div>
              ) : null}

              {detail && (detail.open_payables.length > 0 || detail.open_receivables.length > 0) ? (
                <div className="space-y-2">
                  <div className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                    Взаимные поставки (зачёт равными суммами)
                  </div>
                  {detail.open_payables.map((inv) => (
                    <MutualRow
                      key={inv.id}
                      inv={inv}
                      kind="payable"
                      onMarkLoan={
                        canOperate ? () => markLoanMutation.mutate(inv.id) : undefined
                      }
                      marking={markLoanMutation.isPending}
                    />
                  ))}
                  {detail.open_receivables.map((inv) => (
                    <MutualRow
                      key={inv.id}
                      inv={inv}
                      kind="receivable"
                      onMarkLoan={
                        canOperate ? () => markLoanMutation.mutate(inv.id) : undefined
                      }
                      marking={markLoanMutation.isPending}
                    />
                  ))}
                </div>
              ) : null}
            </div>
          ) : null}
        </DialogContent>
      </Dialog>

      <SettleLoanDialog
        key={settleLoan?.id ?? "none"}
        loan={settleLoan}
        partnerName={partner?.name ?? ""}
        onClose={() => setSettleLoan(null)}
        onDone={() => {
          setSettleLoan(null);
          refresh();
        }}
      />
    </>
  );
}

function MutualRow({
  inv,
  kind,
  onMarkLoan,
  marking,
}: {
  inv: BarterInvoice;
  kind: "payable" | "receivable";
  onMarkLoan?: () => void;
  marking?: boolean;
}) {
  return (
    <div className="grid grid-cols-[auto_1fr_auto_auto] items-center gap-3 rounded-md border px-3 py-2 text-sm">
      <span
        className={cn(
          "rounded-full px-2.5 py-0.5 text-xs font-medium",
          kind === "payable" ? "bg-amber-50 text-amber-700" : "bg-emerald-50 text-emerald-700",
        )}
      >
        {kind === "payable" ? "приход" : "расход"}
      </span>
      <span className="min-w-0">
        <span className="block truncate font-medium">
          №{inv.number ?? "—"} · {inv.invoice_date ?? "без даты"}
        </span>
        <span className="block truncate text-xs text-muted-foreground">
          {inv.products.filter(Boolean).slice(0, 4).join(", ") || "состав не указан"}
        </span>
      </span>
      <span className="text-right font-semibold tabular-nums">{formatRub(inv.amount)}</span>
      {onMarkLoan ? (
        <Button size="sm" variant="outline" disabled={marking} onClick={onMarkLoan}>
          Это заём
        </Button>
      ) : (
        <span />
      )}
    </div>
  );
}

/** «ГГГГ-ММ-ДДTчч:мм» по ЛОКАЛЬНОМУ времени — для datetime-local и производной даты. */
function localDateTimeInput(): string {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T${pad(
    now.getHours(),
  )}:${pad(now.getMinutes())}`;
}

/** Уровень 3: окно гашения займа — товаром (по цене выдачи) или деньгами. */
function SettleLoanDialog({
  loan,
  partnerName,
  onClose,
  onDone,
}: {
  loan: OpenLoan | null;
  partnerName: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const loanId = loan?.id ?? "";
  const [mode, setMode] = useState<"goods" | "money">("goods");
  const [qtyByLine, setQtyByLine] = useState<Record<string, string>>({});
  const [moneyByLine, setMoneyByLine] = useState<Record<string, { qty: string; price: string }>>(
    {},
  );
  const [moneyAmount, setMoneyAmount] = useState("");
  // Каналы разные по направлению долга: нам должны — деньги ПРИХОДЯТ (наличный приход или
  // привязка входящей операции выписки); мы должны — деньги УХОДЯТ (наличная выдача или
  // черновик платежа в банк). Входящая операция чужой выписки наш долг погасить не может.
  const [moneyChannel, setMoneyChannel] = useState<"wallet" | "bank" | "draft">("wallet");
  const [walletId, setWalletId] = useState("");
  const [bankOperationId, setBankOperationId] = useState("");
  const [draftChannel, setDraftChannel] = useState<"bank_draft" | "bank_draft_sber">("bank_draft");
  // Недовоз: спрашиваем про хвост НЕ в форме, а при подтверждении возврата — иначе вопрос
  // висит на экране ещё до того, как оператор ввёл килограммы.
  const [askRemainder, setAskRemainder] = useState(false);
  // Дата события — задаёт оператор (гашение бывает задним числом). Дефолт — сегодня по
  // МЕСТНОМУ времени: toISOString() дал бы UTC и поздним вечером уехал бы на вчера.
  const [happenedAt, setHappenedAt] = useState(() => localDateTimeInput());

  const returnableQuery = useQuery({
    queryKey: ["wh", "returnable", loanId],
    queryFn: () => getLoanReturnable(loanId),
    enabled: !!loan,
  });
  const info = returnableQuery.data;
  const walletsQuery = useQuery({
    queryKey: ["dds", "wallets"],
    queryFn: getDdsWallets,
    enabled: !!loan && mode === "money",
  });
  const opsQuery = useQuery({
    queryKey: ["wh", "barter-ops", info?.we_lend],
    // Только НЕРАЗОБРАННЫЕ операции: уже классифицированную (разнесённую по статьям или
    // привязанную) предлагать нельзя — её деньги уже учтены в другом месте.
    queryFn: () => getDdsBankOperations({ limit: 200, classification_status: "needs_review" }),
    enabled: !!loan && mode === "money" && moneyChannel === "bank" && !!info,
  });
  const bankOps = useMemo(
    () =>
      // Канал доступен только когда НАМ должны: контрагент платит → входящая операция.
      (opsQuery.data?.items ?? []).filter(
        (op) => op.direction === "in" && !op.transfer_group_id,
      ),
    [opsQuery.data],
  );

  const goodsLines = useMemo(() => {
    if (!info) return [];
    return info.lines.map((l) => {
      const entered = num(qtyByLine[l.id] ?? "");
      // Довесок при фасовке принимаем (до 100 г сверх выданного), явный перебор — НЕ молча
      // обрезаем, а показываем ошибкой: 3,5 кг вместо 3 — это опечатка оператора.
      const excess = entered > l.remaining_qty + RETURN_QTY_TOLERANCE + 1e-9;
      const qty = Math.min(entered, l.remaining_qty + RETURN_QTY_TOLERANCE);
      // Сумма долга от лишних граммов не растёт: закрываем ровно остаток строки.
      const amount = Math.min(
        Math.round(qty * l.price * 100) / 100,
        Math.round(l.remaining_qty * l.price * 100) / 100,
      );
      return {
        ...l,
        raw: qtyByLine[l.id] ?? "",
        qty,
        excess,
        amount,
      };
    });
  }, [info, qtyByLine]);
  const overReturnLine = goodsLines.find((l) => l.excess);
  const moneyLines = useMemo(() => {
    if (!info) return [];
    return info.lines.map((l) => {
      const entry = moneyByLine[l.id] ?? { qty: "", price: "" };
      const qty = Math.min(num(entry.qty), l.remaining_qty);
      const price = num(entry.price);
      return {
        ...l,
        rawQty: entry.qty,
        rawPrice: entry.price,
        qty,
        unitPrice: price,
        money: Math.round(qty * price * 100) / 100,
        credited: Math.round(qty * l.price * 100) / 100,
      };
    });
  }, [info, moneyByLine]);

  const goodsTotal = goodsLines.reduce((s, l) => s + l.amount, 0);
  // Сколько долга останется после этого возврата: показываем выбор только когда есть хвост.
  const goodsShortfall = info ? Math.round((info.remaining - goodsTotal) * 100) / 100 : 0;
  const goodsQty = goodsLines.reduce((s, l) => s + l.qty, 0);
  const money = info
    ? info.we_lend
      ? moneyLines.reduce((s, l) => s + l.money, 0)
      : Math.min(num(moneyAmount), info.remaining)
    : 0;
  // Тем же предикатом, что и payload: строка без цены на бэк не уйдёт — и в «зачёт долга»
  // её показывать нельзя, иначе экран завышает фактическое гашение.
  const moneyCredited = moneyLines
    .filter((l) => l.qty > 0 && l.unitPrice > 0)
    .reduce((s, l) => s + l.credited, 0);

  const mutation = useMutation({
    mutationFn: async (writeOff: boolean): Promise<void> => {
      if (mode === "goods") {
        await createBarterReturn({
          loan_id: loanId,
          issued_at: happenedAt,
          write_off_remainder: writeOff,
          returns: goodsLines
            .filter((l) => l.qty > 0)
            .map((l) => ({ loan_line_item_id: l.id, quantity: l.qty, amount: l.amount })),
        });
        return;
      }
      if (moneyChannel === "draft") {
        // Мы должны и платим безналом: не привязка чужой операции, а СВОЙ платёж — черновик
        // в банк (подпись в банке, гашение придёт выпиской), как у обычной накладной.
        await createDraft([loanId], draftChannel);
        return;
      }
      await settleLoanWithMoney(loanId, {
        operation_date: happenedAt.slice(0, 10),
        wallet_id: moneyChannel === "wallet" ? walletId : null,
        bank_operation_id: moneyChannel === "bank" ? bankOperationId : null,
        lines: info?.we_lend
          ? moneyLines
              .filter((l) => l.qty > 0 && l.unitPrice > 0)
              .map((l) => ({
                loan_line_item_id: l.id,
                quantity: l.qty,
                unit_price: l.unitPrice,
              }))
          : null,
        amount: info?.we_lend ? null : money,
      });
    },
    onSuccess: () => {
      toast.success(
        mode === "goods"
          ? "Возврат проведён"
          : moneyChannel === "draft"
            ? "Черновик отправлен в банк — подпишите платёж в банке"
            : "Гашение деньгами проведено",
      );
      setQtyByLine({});
      setAskRemainder(false);
      setMoneyByLine({});
      setMoneyAmount("");
      onDone();
    },
    onError: (e) => toast.error(apiErrorMessage(e, "Не удалось провести гашение")),
  });

  const channelReady =
    moneyChannel === "wallet"
      ? !!walletId
      : moneyChannel === "bank"
        ? !!bankOperationId
        : true; // черновик: сумма и получатель берутся из самой накладной займа
  // Черновик выписывается на остаток накладной целиком — поле суммы для него не нужно.
  const amountReady = moneyChannel === "draft" ? (info?.remaining ?? 0) > 0 : money > 0;
  const canSubmit =
    !!loan &&
    !!happenedAt &&
    !mutation.isPending &&
    (mode === "goods" ? goodsTotal > 0 && !overReturnLine : amountReady && channelReady);

  return (
    <Dialog open={!!loan} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Гашение займа №{loan?.number ?? "—"}</DialogTitle>
        </DialogHeader>
        {loan && info ? (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
              <span
                className={cn(
                  "rounded-full px-2.5 py-0.5 text-xs font-medium",
                  info.we_lend ? "bg-emerald-50 text-emerald-700" : "bg-amber-50 text-amber-700",
                )}
              >
                {info.we_lend ? "→ нам должны" : "← мы должны"}
              </span>
              <span>{partnerName}</span>
              <span>· остаток {formatRub(info.remaining)}</span>
            </div>

            <div className="flex flex-wrap items-end justify-between gap-3">
              <div className="inline-flex w-fit overflow-hidden rounded-md border text-xs">
                <button
                  type="button"
                  className={cn("px-3 py-1.5", mode === "goods" && "bg-muted font-medium")}
                  onClick={() => setMode("goods")}
                >
                  Товаром
                </button>
                <button
                  type="button"
                  className={cn("px-3 py-1.5", mode === "money" && "bg-muted font-medium")}
                  onClick={() => setMode("money")}
                >
                  Деньгами
                </button>
              </div>
              <div className="grid gap-1.5">
                <Label className="text-xs">Дата и время</Label>
                <Input
                  type="datetime-local"
                  className="h-8 w-[210px]"
                  value={happenedAt}
                  onChange={(e) => setHappenedAt(e.target.value)}
                />
              </div>
            </div>

            {mode === "goods" ? (
              <div className="space-y-2">
                <div className="grid grid-cols-[1fr_90px_84px_90px] gap-2 px-2 text-xs text-muted-foreground">
                  <span>Товар</span>
                  <span className="text-right">Осталось</span>
                  <span className="text-right">Вернуть</span>
                  <span className="text-right">Сумма</span>
                </div>
                {goodsLines.map((l) => (
                  <div
                    key={l.id}
                    className="grid grid-cols-[1fr_90px_84px_90px] items-center gap-2 rounded-md border p-2 text-sm"
                  >
                    <span className="min-w-0">
                      <span className="block truncate">{l.name}</span>
                      <span className="block text-[11px] text-muted-foreground">
                        по цене выдачи {formatRub(l.price)}
                      </span>
                    </span>
                    <span className="text-right tabular-nums text-muted-foreground">
                      {l.remaining_qty} {l.unit ?? ""}
                    </span>
                    <Input
                      type="number"
                      min={0}
                      max={l.remaining_qty}
                      value={l.raw}
                      onChange={(e) =>
                        setQtyByLine((p) => ({ ...p, [l.id]: e.target.value }))
                      }
                    />
                    <span className="text-right font-medium tabular-nums">
                      {formatRub(l.amount)}
                    </span>
                  </div>
                ))}
                <div className="flex items-center justify-between rounded-md bg-muted/50 px-3 py-2 text-sm">
                  <span>
                    Возврат: <b className="tabular-nums">{goodsQty} кг · {formatRub(goodsTotal)}</b>
                  </span>
                  <span className="text-xs text-muted-foreground">
                    склад принимает по цене выдачи
                  </span>
                </div>
                {overReturnLine ? (
                  <p className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
                    По «{overReturnLine.name}» вернуть можно не больше{" "}
                    {overReturnLine.remaining_qty} кг (+{RETURN_QTY_TOLERANCE} кг довеска) —
                    проверьте количество.
                  </p>
                ) : null}
              </div>
            ) : (
              <div className="space-y-3">
                {info.we_lend ? (
                  <div className="space-y-2">
                    <p className="text-xs text-muted-foreground">
                      Партнёр платит по текущей цене; в зачёт долга идёт кг × цена выдачи.
                    </p>
                    <div className="grid grid-cols-[1fr_74px_80px_92px_84px] gap-2 px-2 text-xs text-muted-foreground">
                      <span>Товар</span>
                      <span className="text-right">Осталось</span>
                      <span className="text-right">Кг</span>
                      <span className="text-right">Цена сейчас</span>
                      <span className="text-right">Деньги</span>
                    </div>
                    {moneyLines.map((l) => (
                      <div
                        key={l.id}
                        className="grid grid-cols-[1fr_74px_80px_92px_84px] items-center gap-2 rounded-md border p-2 text-sm"
                      >
                        <span className="min-w-0">
                          <span className="block truncate">{l.name}</span>
                          <span className="block text-[11px] text-muted-foreground">
                            выдано по {formatRub(l.price)}
                            {l.last_purchase_price != null
                              ? ` · закуп ~${formatRub(l.last_purchase_price)}`
                              : ""}
                          </span>
                        </span>
                        <span className="text-right tabular-nums text-muted-foreground">
                          {l.remaining_qty}
                        </span>
                        <Input
                          type="number"
                          min={0}
                          max={l.remaining_qty}
                          value={l.rawQty}
                          onChange={(e) =>
                            setMoneyByLine((p) => ({
                              ...p,
                              [l.id]: { qty: e.target.value, price: p[l.id]?.price ?? "" },
                            }))
                          }
                        />
                        <Input
                          type="number"
                          min={0}
                          placeholder={
                            l.last_purchase_price != null ? String(l.last_purchase_price) : ""
                          }
                          value={l.rawPrice}
                          onChange={(e) =>
                            setMoneyByLine((p) => ({
                              ...p,
                              [l.id]: { qty: p[l.id]?.qty ?? "", price: e.target.value },
                            }))
                          }
                        />
                        <span className="text-right font-medium tabular-nums">
                          {formatRub(l.money)}
                        </span>
                      </div>
                    ))}
                    <div className="flex flex-wrap items-center justify-between gap-2 rounded-md bg-muted/50 px-3 py-2 text-sm">
                      <span>
                        Деньгами: <b className="tabular-nums">{formatRub(money)}</b>
                      </span>
                      <span className="text-xs text-muted-foreground">
                        в зачёт долга {formatRub(moneyCredited)}
                      </span>
                    </div>
                  </div>
                ) : moneyChannel === "draft" ? null : (
                  <div className="grid gap-2 sm:max-w-xs">
                    <Label>Сумма оплаты, ₽</Label>
                    <Input
                      type="number"
                      min={0}
                      max={info.remaining}
                      value={moneyAmount}
                      onChange={(e) => setMoneyAmount(e.target.value)}
                    />
                    <p className="text-xs text-muted-foreground">
                      «Как обычная поставка» — по сумме накладной займа; из{" "}
                      {formatRub(info.remaining)} остатка.
                    </p>
                  </div>
                )}

                <div className="grid gap-3 sm:grid-cols-2">
                  <div className="grid gap-2">
                    <Label>Канал денег</Label>
                    <div className="inline-flex w-fit overflow-hidden rounded-md border text-xs">
                      <button
                        type="button"
                        className={cn(
                          "px-3 py-1.5",
                          moneyChannel === "wallet" && "bg-muted font-medium",
                        )}
                        onClick={() => setMoneyChannel("wallet")}
                      >
                        Наличные / кошелёк
                      </button>
                      {info.we_lend ? (
                        <button
                          type="button"
                          className={cn(
                            "px-3 py-1.5",
                            moneyChannel === "bank" && "bg-muted font-medium",
                          )}
                          onClick={() => setMoneyChannel("bank")}
                        >
                          Банк-операция
                        </button>
                      ) : (
                        <button
                          type="button"
                          className={cn(
                            "px-3 py-1.5",
                            moneyChannel === "draft" && "bg-muted font-medium",
                          )}
                          onClick={() => setMoneyChannel("draft")}
                        >
                          Черновик в банк
                        </button>
                      )}
                    </div>
                  </div>
                  {moneyChannel === "wallet" ? (
                    <div className="grid gap-2">
                      <Label>Кошелёк</Label>
                      <Select value={walletId} onValueChange={setWalletId}>
                        <SelectTrigger>
                          <SelectValue
                            placeholder={
                              info.we_lend ? "Куда пришли деньги" : "Откуда ушли деньги"
                            }
                          />
                        </SelectTrigger>
                        <SelectContent>
                          {(walletsQuery.data ?? [])
                            // Банковский кошелёк здесь запрещён (бэк тоже откажет): приход
                            // придёт ещё и выпиской — ДДС задвоится. Для банка — канал «Банк-операция».
                            .filter((w) => w.status === "active" && w.type !== "bank")
                            .map((w) => (
                              <SelectItem key={w.id} value={w.id}>
                                {w.name}
                              </SelectItem>
                            ))}
                        </SelectContent>
                      </Select>
                    </div>
                  ) : moneyChannel === "bank" ? (
                    <div className="grid gap-2">
                      <Label>Входящая операция</Label>
                      <Select value={bankOperationId} onValueChange={setBankOperationId}>
                        <SelectTrigger>
                          <SelectValue placeholder="Выберите операцию" />
                        </SelectTrigger>
                        <SelectContent>
                          {bankOps.map((op) => (
                            <SelectItem key={op.id} value={op.id}>
                              {op.operation_date} · {formatRub(Number(op.amount))} ·{" "}
                              {op.counterparty_name_raw ?? op.payment_purpose ?? "—"}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                      <p className="text-xs text-muted-foreground">
                        Только неразобранные приходы — уже разнесённые операции здесь не
                        предлагаются.
                      </p>
                    </div>
                  ) : (
                    <div className="grid gap-2">
                      <Label>Банк-плательщик</Label>
                      <Select
                        value={draftChannel}
                        onValueChange={(v) =>
                          setDraftChannel(v as "bank_draft" | "bank_draft_sber")
                        }
                      >
                        <SelectTrigger>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="bank_draft">Т-Банк</SelectItem>
                          <SelectItem value="bank_draft_sber">Сбербанк</SelectItem>
                        </SelectContent>
                      </Select>
                      <p className="text-xs text-muted-foreground">
                        Черновик на остаток {formatRub(info.remaining)} — подпись в банке,
                        гашение придёт выпиской.
                      </p>
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        ) : null}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Отмена
          </Button>
          <Button
            disabled={!canSubmit}
            onClick={() => {
              // Недовоз — спрашиваем, что с хвостом, ПЕРЕД проведением; иначе сразу проводим.
              if (mode === "goods" && goodsShortfall > 0) {
                setAskRemainder(true);
                return;
              }
              mutation.mutate(false);
            }}
          >
            {mutation.isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : null}
            {mode === "goods"
              ? "Провести возврат"
              : moneyChannel === "draft"
                ? "Отправить в банк"
                : info?.we_lend
                  ? "Принять деньги"
                  : "Оплатить заём"}
          </Button>
        </DialogFooter>
      </DialogContent>

      <AlertDialog open={askRemainder} onOpenChange={(open) => !open && setAskRemainder(false)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Что с остатком долга?</AlertDialogTitle>
            <AlertDialogDescription>
              Возвращаете {goodsQty} кг на {formatRub(goodsTotal)} — это меньше выданного.
              Непокрытыми останутся {formatRub(goodsShortfall)}.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Отмена</AlertDialogCancel>
            <Button variant="outline" onClick={() => mutation.mutate(false)}>
              Оставить долгом
            </Button>
            <Button onClick={() => mutation.mutate(true)}>Списать остаток</Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Dialog>
  );
}
