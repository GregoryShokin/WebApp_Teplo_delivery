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
import { Combobox, type ComboboxOption } from "@/components/ui/combobox";
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
  createBankPrepaymentDraft,
  createPrepayment,
  getExpenseArticles,
  getRegistry,
  getWallets,
} from "./api";

type PayMode = "safe" | "bank";

const DEFAULT_ARTICLE = "default";
const SAFE_WALLET_CODE = "cash_safe";

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

export function CreatePrepaymentDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();

  const [mode, setMode] = useState<PayMode>("safe");
  const [counterpartyId, setCounterpartyId] = useState("");
  const [walletId, setWalletId] = useState("");
  const [amount, setAmount] = useState("");
  const [operationDate, setOperationDate] = useState(today());
  const [articleId, setArticleId] = useState<string>(DEFAULT_ARTICLE);
  const [note, setNote] = useState("");

  const registryQuery = useQuery({
    queryKey: ["cp", "registry"],
    queryFn: () => getRegistry(),
    enabled: open,
  });
  const walletsQuery = useQuery({ queryKey: ["cp", "wallets"], queryFn: getWallets, enabled: open });
  const articlesQuery = useQuery({
    queryKey: ["cp", "expense-articles"],
    queryFn: getExpenseArticles,
    enabled: open,
  });

  useEffect(() => {
    if (!open) return;
    setMode("safe");
    setCounterpartyId("");
    setAmount("");
    setOperationDate(today());
    setArticleId(DEFAULT_ARTICLE);
    setNote("");
  }, [open]);

  // По умолчанию платим с «Сейфа» — основной кошелёк предоплат.
  useEffect(() => {
    if (walletId || !walletsQuery.data) return;
    const safe = walletsQuery.data.find((wallet) => wallet.code === SAFE_WALLET_CODE);
    if (safe) setWalletId(safe.id);
  }, [walletsQuery.data, walletId]);

  const createMutation = useMutation({
    mutationFn: async () => {
      const articleId_ = articleId === DEFAULT_ARTICLE ? null : articleId;
      if (mode === "bank") {
        await createBankPrepaymentDraft({
          counterparty_id: counterpartyId,
          amount: Number(amount),
          article_id: articleId_,
        });
        return;
      }
      await createPrepayment({
        counterparty_id: counterpartyId,
        wallet_id: walletId,
        amount: Number(amount),
        operation_date: operationDate,
        article_id: articleId_,
        note: note || null,
      });
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["cp"] });
      await queryClient.invalidateQueries({ queryKey: ["dds"] });
      onOpenChange(false);
      toast.success(
        mode === "bank"
          ? "Платёж отправлен в банк — при оплате появится предоплата"
          : "Предоплата создана — расход проведён, дебиторка учтена",
      );
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось создать платёж")),
  });

  const numericAmount = Number(amount);
  const canSubmit =
    Boolean(counterpartyId) &&
    numericAmount > 0 &&
    (mode === "bank" || (Boolean(walletId) && Boolean(operationDate)));

  const counterpartyOptions: ComboboxOption[] = [...(registryQuery.data ?? [])]
    .sort((a, b) => a.name.localeCompare(b.name, "ru"))
    .map((item) => ({
      value: item.counterparty_id,
      label: item.name,
      keywords: item.inn ?? undefined,
    }));

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Создать платёж поставщику</DialogTitle>
        </DialogHeader>
        <div className="grid gap-4">
          <div className="grid grid-cols-2 gap-2">
            <Button
              type="button"
              variant={mode === "safe" ? "default" : "outline"}
              onClick={() => setMode("safe")}
            >
              С Сейфа (предоплата)
            </Button>
            <Button
              type="button"
              variant={mode === "bank" ? "default" : "outline"}
              onClick={() => setMode("bank")}
            >
              Банк по реквизитам
            </Button>
          </div>
          <p className="text-sm text-muted-foreground">
            {mode === "bank"
              ? "Платёж уходит в банк по реквизитам контрагента (нужны подтверждённые реквизиты). Когда банк проведёт оплату — появится предоплата (дебиторка)."
              : "Платим поставщику вперёд с кошелька (обычно Сейф) — сразу возникает дебиторка «поставщик нам должен». Приходящие накладные потом гасятся из остатка."}
          </p>
          <div className="grid gap-2">
            <Label>Контрагент</Label>
            <Combobox
              options={counterpartyOptions}
              value={counterpartyId}
              onChange={setCounterpartyId}
              placeholder="Выберите поставщика"
              searchPlaceholder="Поиск по названию или ИНН…"
            />
          </div>
          <div className={mode === "safe" ? "grid grid-cols-2 gap-4" : "grid gap-2"}>
            <div className="grid gap-2">
              <Label>Сумма, ₽</Label>
              <Input
                type="number"
                value={amount}
                onChange={(event) => setAmount(event.target.value)}
              />
            </div>
            {mode === "safe" ? (
              <div className="grid gap-2">
                <Label>Дата</Label>
                <Input
                  type="date"
                  value={operationDate}
                  onChange={(event) => setOperationDate(event.target.value)}
                />
              </div>
            ) : null}
          </div>
          {mode === "safe" ? (
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
          ) : null}
          <div className="grid gap-2">
            <Label>Статья ДДС</Label>
            <Select value={articleId} onValueChange={setArticleId}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={DEFAULT_ARTICLE}>Авансы поставщикам (по умолчанию)</SelectItem>
                {(articlesQuery.data ?? []).map((article) => (
                  <SelectItem key={article.id} value={article.id}>
                    {article.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          {mode === "safe" ? (
            <div className="grid gap-2">
              <Label>Комментарий</Label>
              <Input value={note} onChange={(event) => setNote(event.target.value)} />
            </div>
          ) : null}
        </div>
        <DialogFooter>
          <Button
            disabled={!canSubmit || createMutation.isPending}
            onClick={() => createMutation.mutate()}
          >
            {createMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : null}
            {mode === "bank" ? "Отправить в банк" : "Создать предоплату"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
