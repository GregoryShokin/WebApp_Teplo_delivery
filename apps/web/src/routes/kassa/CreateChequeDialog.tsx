import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
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
import { apiErrorMessage } from "@/lib/api";
import { formatDdsMoney, formatDateTime, toDateTimeLocalInput } from "@/routes/dds/shared";
import {
  createCheque,
  getCardTransactions,
  getKassaCounterparties,
  getKassaExpenseArticles,
  type ChequeLinePayload,
} from "@/routes/kassa/api";
import { CardTierBadge } from "@/routes/kassa/shared";

type DraftLine = { key: string; name: string; quantity: string; price: string };

let lineSeq = 0;
function emptyLine(): DraftLine {
  lineSeq += 1;
  return { key: `l${lineSeq}`, name: "", quantity: "1", price: "" };
}

type CreateChequeDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: () => void;
};

export function CreateChequeDialog({ open, onOpenChange, onCreated }: CreateChequeDialogProps) {
  const queryClient = useQueryClient();
  const [counterpartyId, setCounterpartyId] = useState("");
  const [articleId, setArticleId] = useState("");
  const [issuedAt, setIssuedAt] = useState(() => toDateTimeLocalInput(new Date()));
  const [selectedOps, setSelectedOps] = useState<Record<string, number>>({});
  const [cashAmount, setCashAmount] = useState("");
  const [trackNomenclature, setTrackNomenclature] = useState(false);
  const [lines, setLines] = useState<DraftLine[]>([emptyLine()]);
  const [comment, setComment] = useState("");

  const counterpartiesQuery = useQuery({
    queryKey: ["kassa", "counterparties"],
    queryFn: () => getKassaCounterparties(),
    enabled: open,
  });
  const articlesQuery = useQuery({
    queryKey: ["kassa", "expense-articles"],
    queryFn: () => getKassaExpenseArticles(),
    enabled: open,
  });
  const cardTxQuery = useQuery({
    queryKey: ["kassa", "card-transactions", issuedAt],
    queryFn: () => getCardTransactions({ issued_at: issuedAt }),
    enabled: open && Boolean(issuedAt),
  });

  const cardTotal = useMemo(
    () => Object.values(selectedOps).reduce((sum, amount) => sum + amount, 0),
    [selectedOps],
  );
  const linesTotal = useMemo(
    () =>
      lines.reduce(
        (sum, line) => sum + (Number(line.quantity) || 0) * (Number(line.price) || 0),
        0,
      ),
    [lines],
  );
  const cash = Number(cashAmount) || 0;
  const paidTotal = cardTotal + cash;

  function reset() {
    setCounterpartyId("");
    setArticleId("");
    setIssuedAt(toDateTimeLocalInput(new Date()));
    setSelectedOps({});
    setCashAmount("");
    setTrackNomenclature(false);
    setLines([emptyLine()]);
    setComment("");
  }

  const createMutation = useMutation({
    mutationFn: () => {
      const bankParts = Object.keys(selectedOps).map((id) => ({ bank_operation_id: id }));
      const payloadLines: ChequeLinePayload[] | undefined = trackNomenclature
        ? lines
            .filter((line) => line.name.trim())
            .map((line) => ({
              name: line.name.trim(),
              quantity: Number(line.quantity) || 0,
              price: Number(line.price) || 0,
            }))
        : undefined;
      return createCheque({
        counterparty_id: counterpartyId,
        article_id: articleId,
        issued_at: issuedAt,
        bank_parts: bankParts,
        cash_amount: cash > 0 ? cash : null,
        track_nomenclature: trackNomenclature,
        lines: payloadLines,
        comment: comment.trim() || null,
      });
    },
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["kassa", "cheques"] }),
        queryClient.invalidateQueries({ queryKey: ["dds", "cashflow"] }),
        queryClient.invalidateQueries({ queryKey: ["dds", "wallets"] }),
      ]);
      toast.success("Чек создан и проведён в ДДС");
      reset();
      onCreated();
      onOpenChange(false);
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось создать чек")),
  });

  const nomenclatureOk = !trackNomenclature || Math.abs(linesTotal - paidTotal) < 0.01;
  const canSave =
    Boolean(counterpartyId) &&
    Boolean(articleId) &&
    Boolean(issuedAt) &&
    paidTotal > 0 &&
    nomenclatureOk &&
    !createMutation.isPending;

  function toggleOp(id: string, amount: number) {
    setSelectedOps((prev) => {
      const next = { ...prev };
      if (next[id] != null) {
        delete next[id];
      } else {
        next[id] = amount;
      }
      return next;
    });
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[88vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Новый чек (оплата картой)</DialogTitle>
          <DialogDescription>
            Уже оплаченная покупка по бизнес-карте. Подберите операции из выписки T-Bank; при
            необходимости добавьте наличную часть. Чек сразу проводится в ДДС.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="grid gap-2">
              <Label>Контрагент / магазин</Label>
              <Select value={counterpartyId} onValueChange={setCounterpartyId}>
                <SelectTrigger>
                  <SelectValue placeholder="Выберите контрагента" />
                </SelectTrigger>
                <SelectContent>
                  {(counterpartiesQuery.data ?? []).map((cp) => (
                    <SelectItem key={cp.id} value={cp.id}>
                      {cp.name}
                      {cp.inn ? ` · ${cp.inn}` : ""}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="grid gap-2">
              <Label>Статья ДДС</Label>
              <Select value={articleId} onValueChange={setArticleId}>
                <SelectTrigger>
                  <SelectValue placeholder="Выберите статью расхода" />
                </SelectTrigger>
                <SelectContent>
                  {(articlesQuery.data ?? []).map((article) => (
                    <SelectItem key={article.id} value={article.id}>
                      {article.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="grid gap-2">
              <Label>Дата и время чека</Label>
              <Input
                type="datetime-local"
                value={issuedAt}
                onChange={(event) => setIssuedAt(event.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <Label>Оплачено наличными, ₽</Label>
              <Input
                inputMode="decimal"
                placeholder="0"
                value={cashAmount}
                onChange={(event) => setCashAmount(event.target.value)}
              />
            </div>
          </div>

          <div className="space-y-2">
            <Label>Оплачено с карты (операции T-Bank)</Label>
            <div className="max-h-60 space-y-1 overflow-y-auto rounded-md border p-2">
              {cardTxQuery.isLoading ? (
                <div className="px-2 py-3 text-sm text-muted-foreground">Загрузка операций…</div>
              ) : (cardTxQuery.data ?? []).length === 0 ? (
                <div className="px-2 py-3 text-sm text-muted-foreground">
                  Нет карточных операций рядом с датой чека.
                </div>
              ) : (
                (cardTxQuery.data ?? []).map((op) => (
                  <label
                    key={op.bank_operation_id}
                    className="flex cursor-pointer items-center gap-3 rounded-md px-2 py-2 hover:bg-muted/50"
                  >
                    <Checkbox
                      checked={selectedOps[op.bank_operation_id] != null}
                      onChange={() => toggleOp(op.bank_operation_id, op.amount)}
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 text-sm">
                        <span className="font-medium tabular-nums">{formatDdsMoney(op.amount)}</span>
                        <CardTierBadge tier={op.tier} />
                      </div>
                      <div className="truncate text-xs text-muted-foreground">
                        {formatDateTime(op.posted_at ?? op.operation_date)}
                        {op.purpose ? ` · ${op.purpose}` : ""}
                      </div>
                    </div>
                  </label>
                ))
              )}
            </div>
          </div>

          <label className="flex items-center gap-2 text-sm">
            <Checkbox
              checked={trackNomenclature}
              onChange={(event) => setTrackNomenclature(event.target.checked)}
            />
            Вести складской учёт (номенклатура)
          </label>

          {trackNomenclature ? (
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Label>Позиции</Label>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => setLines((prev) => [...prev, emptyLine()])}
                >
                  <Plus size={14} aria-hidden="true" />
                  Строка
                </Button>
              </div>
              {lines.map((line) => (
                <div key={line.key} className="grid grid-cols-[1fr_64px_88px_auto] items-center gap-2">
                  <Input
                    placeholder="Наименование"
                    value={line.name}
                    onChange={(event) =>
                      setLines((prev) =>
                        prev.map((l) =>
                          l.key === line.key ? { ...l, name: event.target.value } : l,
                        ),
                      )
                    }
                  />
                  <Input
                    inputMode="decimal"
                    placeholder="Кол-во"
                    value={line.quantity}
                    onChange={(event) =>
                      setLines((prev) =>
                        prev.map((l) =>
                          l.key === line.key ? { ...l, quantity: event.target.value } : l,
                        ),
                      )
                    }
                  />
                  <Input
                    inputMode="decimal"
                    placeholder="Цена"
                    value={line.price}
                    onChange={(event) =>
                      setLines((prev) =>
                        prev.map((l) =>
                          l.key === line.key ? { ...l, price: event.target.value } : l,
                        ),
                      )
                    }
                  />
                  <Button
                    size="icon"
                    variant="ghost"
                    onClick={() =>
                      setLines((prev) =>
                        prev.length > 1 ? prev.filter((l) => l.key !== line.key) : prev,
                      )
                    }
                  >
                    <Trash2 size={14} aria-hidden="true" />
                  </Button>
                </div>
              ))}
              {!nomenclatureOk ? (
                <p className="text-xs text-destructive">
                  Сумма позиций {formatDdsMoney(linesTotal)} не совпадает с оплатой{" "}
                  {formatDdsMoney(paidTotal)}.
                </p>
              ) : null}
            </div>
          ) : null}

          <div className="grid gap-2">
            <Label>Комментарий</Label>
            <Input value={comment} onChange={(event) => setComment(event.target.value)} />
          </div>

          <div className="flex flex-wrap items-center gap-4 rounded-md bg-muted/50 p-2 text-sm tabular-nums">
            <span>
              Карта: <span className="font-medium">{formatDdsMoney(cardTotal)}</span>
            </span>
            <span>
              Наличные: <span className="font-medium">{formatDdsMoney(cash)}</span>
            </span>
            <span>
              Итого: <span className="font-medium">{formatDdsMoney(paidTotal)}</span>
            </span>
          </div>
        </div>

        <DialogFooter>
          <Button disabled={!canSave} onClick={() => createMutation.mutate()}>
            {createMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : null}
            Создать чек
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
