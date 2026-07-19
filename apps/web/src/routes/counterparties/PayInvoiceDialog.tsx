import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle } from "lucide-react";
import { toast } from "sonner";

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
import { apiErrorMessage } from "@/lib/api";

import {
  getExpenseArticles,
  getWallets,
  payInvoice,
  type CounterpartyInvoice,
} from "./api";
import { formatRub } from "./shared";
import { todayIso } from "@/lib/date";

const DEFAULT_ARTICLE = "default";


export function PayInvoiceDialog({
  invoice,
  onOpenChange,
}: {
  invoice: CounterpartyInvoice | null;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const open = Boolean(invoice);
  const remaining = invoice?.remaining ?? 0;

  const [amount, setAmount] = useState("");
  const [walletId, setWalletId] = useState("");
  const [operationDate, setOperationDate] = useState(todayIso());
  const [articleId, setArticleId] = useState<string>(DEFAULT_ARTICLE);
  const [comment, setComment] = useState("");

  const walletsQuery = useQuery({ queryKey: ["cp", "wallets"], queryFn: getWallets, enabled: open });
  const articlesQuery = useQuery({
    queryKey: ["cp", "expense-articles"],
    queryFn: getExpenseArticles,
    enabled: open,
  });

  useEffect(() => {
    setAmount(remaining ? String(remaining) : "");
    setWalletId("");
    setOperationDate(todayIso());
    setArticleId(DEFAULT_ARTICLE);
    setComment("");
  }, [invoice, remaining]);

  const payMutation = useMutation({
    mutationFn: () =>
      payInvoice(invoice!.id, {
        amount: Number(amount),
        wallet_id: walletId,
        operation_date: operationDate,
        article_id: articleId === DEFAULT_ARTICLE ? null : articleId,
        comment: comment || null,
      }),
    onSuccess: async (updated) => {
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      onOpenChange(false);
      toast.success(
        updated.payment_status === "paid"
          ? "Накладная оплачена"
          : `Оплачено частично, остаток ${formatRub(updated.remaining)}`,
      );
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось провести оплату")),
  });

  const numericAmount = Number(amount);
  const amountValid = numericAmount > 0 && numericAmount <= remaining;
  const canSubmit = amountValid && Boolean(walletId) && Boolean(operationDate);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Оплата со счёта ДДС</DialogTitle>
        </DialogHeader>
        {invoice ? (
          <div className="grid gap-4">
            <p className="text-sm text-muted-foreground">
              {invoice.counterparty_name} · счёт {invoice.number ?? "—"} · к оплате{" "}
              <span className="font-medium tabular-nums">{formatRub(remaining)}</span>
            </p>
            <div className="grid grid-cols-2 gap-4">
              <div className="grid gap-2">
                <Label>Сумма, ₽</Label>
                <Input
                  type="number"
                  value={amount}
                  onChange={(event) => setAmount(event.target.value)}
                />
                {!amountValid && amount ? (
                  <span className="text-xs text-red-600">Не больше остатка</span>
                ) : null}
              </div>
              <div className="grid gap-2">
                <Label>Дата</Label>
                <Input
                  type="date"
                  value={operationDate}
                  onChange={(event) => setOperationDate(event.target.value)}
                />
              </div>
            </div>
            <div className="grid gap-2">
              <Label>Счёт (кошелёк ДДС)</Label>
              <Select value={walletId} onValueChange={setWalletId}>
                <SelectTrigger>
                  <SelectValue placeholder="Выберите счёт" />
                </SelectTrigger>
                <SelectContent>
                  {(walletsQuery.data ?? []).map((wallet) => (
                    <SelectItem key={wallet.id} value={wallet.id}>
                      {wallet.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="grid gap-2">
              <Label>Статья ДДС</Label>
              <Select value={articleId} onValueChange={setArticleId}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={DEFAULT_ARTICLE}>Оплата поставщикам (по умолчанию)</SelectItem>
                  {(articlesQuery.data ?? []).map((article) => (
                    <SelectItem key={article.id} value={article.id}>
                      {article.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="grid gap-2">
              <Label>Комментарий</Label>
              <Input value={comment} onChange={(event) => setComment(event.target.value)} />
            </div>
            <p className="text-xs text-muted-foreground">
              Создаст расход в ДДС с выбранного счёта. Неоплаченный остаток можно отправить в банк
              платёжкой.
            </p>
          </div>
        ) : null}
        <DialogFooter>
          <Button disabled={!canSubmit || payMutation.isPending} onClick={() => payMutation.mutate()}>
            {payMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : null}
            Провести оплату
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
