import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Combobox, type ComboboxOption } from "@/components/ui/combobox";
import { Label } from "@/components/ui/label";
import { ArticleCombobox } from "@/components/ui-app/ArticleCombobox";
import {
  apiErrorMessage,
  classifyTransaction,
  getDdsArticles,
  getDdsCounterparties,
  type JournalRow,
} from "@/lib/api";
import { DirectionBadge, compactText, formatDate, formatDdsMoney } from "@/routes/dds/shared";

/**
 * Ручная разметка проводки ДДС без bank-операции (её нельзя разобрать через операцию выписки):
 * назначить статью и контрагента. Статья не влияет на баланс — только аналитика.
 */
export function CashflowClassifyDialog({
  row,
  canClassify,
  onClose,
}: {
  row: JournalRow | null;
  canClassify: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const articlesQuery = useQuery({ queryKey: ["dds", "articles"], queryFn: getDdsArticles });
  const counterpartiesQuery = useQuery({
    queryKey: ["dds", "counterparties"],
    queryFn: () => getDdsCounterparties(),
  });
  const [articleId, setArticleId] = useState("none");
  const [counterpartyId, setCounterpartyId] = useState("");

  useEffect(() => {
    if (row) {
      setArticleId(row.article_id ?? "none");
      setCounterpartyId(row.counterparty_id ?? "");
    }
  }, [row?.id]);

  const mutation = useMutation({
    mutationFn: () =>
      classifyTransaction(row!.id, {
        article_id: articleId === "none" ? null : articleId,
        counterparty_id: counterpartyId || null,
      }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["dds", "journal"] }),
        queryClient.invalidateQueries({ queryKey: ["dds", "cashflow"] }),
        queryClient.invalidateQueries({ queryKey: ["dds", "wallets"] }),
      ]);
      toast.success("Проводка размечена");
      onClose();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить разметку")),
  });

  if (!row) {
    return null;
  }

  const counterparties = counterpartiesQuery.data ?? [];
  const counterpartyOptions: ComboboxOption[] = [
    { value: "", label: "Не указан" },
    ...counterparties.map((cp) => ({
      value: cp.id,
      label: cp.name,
      keywords: cp.inn ?? undefined,
    })),
  ];

  return (
    <Dialog open={Boolean(row)} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex flex-wrap items-center gap-3">
            <span>Разметка проводки</span>
            <DirectionBadge direction={row.direction} />
          </DialogTitle>
          <DialogDescription className="text-base font-medium text-foreground">
            {formatDate(row.operation_date)} · {formatDdsMoney(row.amount)}
          </DialogDescription>
        </DialogHeader>

        <div className="rounded-lg border bg-muted/20 p-3">
          <div className="text-xs font-medium uppercase text-muted-foreground">
            Назначение платежа
          </div>
          <div className="mt-1 break-words text-sm">{compactText(row.payment_purpose)}</div>
        </div>

        {canClassify ? (
          <div className="grid gap-3 border-t pt-4">
            <div className="grid gap-1.5">
              <Label className="text-sm">Статья ДДС</Label>
              <ArticleCombobox
                articles={articlesQuery.data ?? []}
                value={articleId}
                onChange={setArticleId}
              />
            </div>
            <div className="grid gap-1.5">
              <Label className="text-sm">Контрагент</Label>
              <Combobox
                options={counterpartyOptions}
                value={counterpartyId}
                onChange={setCounterpartyId}
                placeholder="Не указан"
                searchPlaceholder="Поиск по названию или ИНН…"
              />
            </div>
            <div className="flex gap-2 border-t pt-4">
              <Button disabled={mutation.isPending} onClick={() => mutation.mutate()}>
                {mutation.isPending ? (
                  <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                ) : null}
                Сохранить
              </Button>
              <Button disabled={mutation.isPending} onClick={onClose} type="button" variant="outline">
                Отмена
              </Button>
            </div>
          </div>
        ) : (
          <div className="rounded-md border bg-muted/40 px-3 py-2 text-sm text-muted-foreground">
            Режим просмотра. Разметка недоступна.
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
