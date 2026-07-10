import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Loader2, Plus, Trash2 } from "lucide-react";
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
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  apiErrorMessage,
  createInternalTransfer,
  getNewPaymentContext,
  type NewPaymentExpenseLine,
} from "@/lib/api";
import { cn } from "@/lib/utils";

type TargetRow = { key: string; articleId: string; amount: string; purpose: string };

const money = new Intl.NumberFormat("ru-RU", {
  style: "currency",
  currency: "RUB",
  maximumFractionDigits: 0,
});

let rowSeq = 0;
const newRow = (): TargetRow => ({
  key: `t${rowSeq++}`,
  articleId: "",
  amount: "",
  purpose: "",
});

const num = (v: string) => Number(v.replace(",", ".").replace(/\s/g, ""));

export function InternalTransferDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [sourceId, setSourceId] = useState("");
  const [destId, setDestId] = useState("");
  const [mode, setMode] = useState<"plain" | "targeted">("plain");
  const [amount, setAmount] = useState("");
  const [purpose, setPurpose] = useState("");
  const [rows, setRows] = useState<TargetRow[]>([newRow()]);

  const contextQuery = useQuery({
    queryKey: ["new-payment", "context"],
    queryFn: getNewPaymentContext,
    enabled: open,
  });

  const cashWallets = useMemo(
    () => (contextQuery.data?.wallets ?? []).filter((w) => w.kind === "cash"),
    [contextQuery.data],
  );
  const expenseArticles = useMemo(
    () => (contextQuery.data?.articles ?? []).filter((a) => a.flow === "expense"),
    [contextQuery.data],
  );

  // Сброс при открытии/закрытии.
  useEffect(() => {
    if (!open) return;
    setSourceId("");
    setDestId("");
    setMode("plain");
    setAmount("");
    setPurpose("");
    setRows([newRow()]);
  }, [open]);

  // Источник и получатель не могут совпадать: при конфликте сбрасываем получателя.
  useEffect(() => {
    if (destId && destId === sourceId) setDestId("");
  }, [sourceId, destId]);

  const destOptions = cashWallets.filter((w) => w.id !== sourceId);
  const targetTotal = rows.reduce((acc, r) => acc + (num(r.amount) || 0), 0);

  const plainValid = mode === "plain" && sourceId && destId && num(amount) > 0;
  const targetedValid =
    mode === "targeted" &&
    sourceId &&
    destId &&
    rows.length > 0 &&
    rows.every((r) => r.articleId && num(r.amount) > 0);
  const canSubmit = Boolean(plainValid || targetedValid);

  function updateRow(key: string, patch: Partial<TargetRow>) {
    setRows((prev) => prev.map((r) => (r.key === key ? { ...r, ...patch } : r)));
  }

  const createMutation = useMutation({
    mutationFn: async () => {
      if (mode === "plain") {
        return createInternalTransfer({
          source_wallet_id: sourceId,
          dest_wallet_id: destId,
          mode: "plain",
          amount: num(amount),
          purpose: purpose.trim() || null,
        });
      }
      const lines: NewPaymentExpenseLine[] = rows.map((r) => ({
        article_id: r.articleId,
        amount: num(r.amount),
        purpose: r.purpose.trim(),
      }));
      return createInternalTransfer({
        source_wallet_id: sourceId,
        dest_wallet_id: destId,
        mode: "targeted",
        lines,
      });
    },
    onSuccess: async (res) => {
      await queryClient.invalidateQueries({ queryKey: ["finance-payments"] });
      await queryClient.invalidateQueries({ queryKey: ["dds"] });
      toast.success(
        res.reserves > 0
          ? `Переведено ${money.format(res.amount)}, резервов: ${res.reserves}`
          : `Переведено ${money.format(res.amount)}`,
      );
      onOpenChange(false);
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось выполнить перевод")),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Внутренний перевод</DialogTitle>
          <DialogDescription>
            Перемещение наличных между Сейфом и Кассой. На банковские счета переводить нельзя.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {/* Откуда → Куда */}
          <div className="flex items-end gap-2">
            <Label className="flex-1 space-y-1">
              <span className="text-sm">Откуда</span>
              <Select value={sourceId} onValueChange={setSourceId}>
                <SelectTrigger>
                  <SelectValue placeholder="Счёт-источник" />
                </SelectTrigger>
                <SelectContent>
                  {cashWallets.map((w) => (
                    <SelectItem key={w.id} value={w.id}>
                      {w.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Label>
            <ArrowRight className="mb-2.5 shrink-0 text-muted-foreground" size={18} />
            <Label className="flex-1 space-y-1">
              <span className="text-sm">Куда</span>
              <Select value={destId} onValueChange={setDestId} disabled={!sourceId}>
                <SelectTrigger>
                  <SelectValue placeholder="Счёт-получатель" />
                </SelectTrigger>
                <SelectContent>
                  {destOptions.map((w) => (
                    <SelectItem key={w.id} value={w.id}>
                      {w.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Label>
          </div>

          {/* Режим */}
          <div className="grid grid-cols-2 gap-2">
            <button
              type="button"
              onClick={() => setMode("plain")}
              className={cn(
                "rounded-md border p-2.5 text-left text-sm transition-colors",
                mode === "plain" ? "border-primary bg-accent" : "hover:bg-muted",
              )}
            >
              <div className="font-medium">Обычный</div>
              <div className="text-xs text-muted-foreground">Просто переместить деньги</div>
            </button>
            <button
              type="button"
              onClick={() => setMode("targeted")}
              className={cn(
                "rounded-md border p-2.5 text-left text-sm transition-colors",
                mode === "targeted" ? "border-primary bg-accent" : "hover:bg-muted",
              )}
            >
              <div className="font-medium">Целевой</div>
              <div className="text-xs text-muted-foreground">Зарезервировать под цели</div>
            </button>
          </div>

          {mode === "plain" ? (
            <div className="space-y-3">
              <Label className="space-y-1">
                <span className="text-sm">Сумма</span>
                <Input
                  inputMode="decimal"
                  value={amount}
                  onChange={(e) => setAmount(e.target.value)}
                  placeholder="0"
                  className="tabular-nums"
                />
              </Label>
              <Label className="space-y-1">
                <span className="text-sm">Назначение (необязательно)</span>
                <Input
                  value={purpose}
                  onChange={(e) => setPurpose(e.target.value)}
                  placeholder="Например: разменять в кассу"
                />
              </Label>
            </div>
          ) : (
            <div className="space-y-2">
              <div className="text-sm text-muted-foreground">
                Каждая цель станет резервом на счёте-получателе (появится в «Платежах»).
              </div>
              {rows.map((row) => (
                <div key={row.key} className="flex items-start gap-2">
                  <div className="flex-1 space-y-1.5">
                    <Select
                      value={row.articleId}
                      onValueChange={(v) => updateRow(row.key, { articleId: v })}
                    >
                      <SelectTrigger className="h-9">
                        <SelectValue placeholder="Статья" />
                      </SelectTrigger>
                      <SelectContent>
                        {expenseArticles.map((a) => (
                          <SelectItem key={a.id} value={a.id}>
                            {a.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <Input
                      value={row.purpose}
                      onChange={(e) => updateRow(row.key, { purpose: e.target.value })}
                      placeholder="Назначение (необязательно)"
                      className="h-9"
                    />
                  </div>
                  <Input
                    inputMode="decimal"
                    value={row.amount}
                    onChange={(e) => updateRow(row.key, { amount: e.target.value })}
                    placeholder="0"
                    className="h-9 w-28 tabular-nums"
                  />
                  <Button
                    size="icon"
                    variant="ghost"
                    className="h-9 w-9 shrink-0"
                    disabled={rows.length === 1}
                    onClick={() => setRows((prev) => prev.filter((r) => r.key !== row.key))}
                  >
                    <Trash2 size={15} />
                  </Button>
                </div>
              ))}
              <div className="flex items-center justify-between">
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => setRows((prev) => [...prev, newRow()])}
                >
                  <Plus size={15} className="mr-1" /> Добавить цель
                </Button>
                <span className="text-sm text-muted-foreground">
                  Итого: <span className="font-semibold text-foreground">{money.format(targetTotal)}</span>
                </span>
              </div>
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={createMutation.isPending}>
            Отмена
          </Button>
          <Button onClick={() => createMutation.mutate()} disabled={!canSubmit || createMutation.isPending}>
            {createMutation.isPending ? <Loader2 className="mr-1.5 animate-spin" size={15} /> : null}
            Перевести
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
