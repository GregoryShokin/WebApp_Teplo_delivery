import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
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
  classifyOperation,
  getDdsArticles,
  type BankOperationRead,
  type OperationClassifyPayload,
} from "@/lib/api";
import {
  DdsStatusBadge,
  DirectionBadge,
  compactText,
  formatDate,
  formatDdsMoney,
} from "@/routes/dds/shared";

type SplitRow = { key: string; articleId: string; amount: string };

function round2(value: number): number {
  return Math.round(value * 100) / 100;
}

export function OperationReviewDialog({
  operation,
  canClassify,
  onClose,
}: {
  operation: BankOperationRead | null;
  canClassify: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const articlesQuery = useQuery({ queryKey: ["dds", "articles"], queryFn: getDdsArticles });
  const [rows, setRows] = useState<SplitRow[]>([]);
  const [rememberAsRule, setRememberAsRule] = useState(false);

  // Reset to a single row covering the whole amount whenever the operation changes.
  useEffect(() => {
    if (operation) {
      setRows([{ key: crypto.randomUUID(), articleId: "none", amount: operation.amount }]);
      setRememberAsRule(false);
    }
  }, [operation?.id]);

  const total = operation ? Number(operation.amount) : 0;
  const allocated = round2(rows.reduce((sum, row) => sum + (Number(row.amount) || 0), 0));
  const remainder = round2(total - allocated);
  const balanced = Math.abs(remainder) < 0.005 && rows.every((row) => Number(row.amount) > 0);

  const invalidate = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["dds", "operations"] }),
      queryClient.invalidateQueries({ queryKey: ["dds", "cashflow"] }),
      queryClient.invalidateQueries({ queryKey: ["dds", "owner-review"] }),
      queryClient.invalidateQueries({ queryKey: ["dds", "wallets"] }),
      queryClient.invalidateQueries({ queryKey: ["dds", "classification-rules"] }),
    ]);
  };

  const mutation = useMutation({
    mutationFn: (payload: OperationClassifyPayload) => classifyOperation(operation!.id, payload),
    onSuccess: async (_data, payload) => {
      await invalidate();
      toast.success(
        payload.action === "split"
          ? "Операция разнесена по статьям"
          : payload.action === "mark_internal_transfer"
            ? "Отмечено как внутренний перевод"
            : "Операция исключена",
      );
      onClose();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить разбор")),
  });

  if (!operation) {
    return null;
  }

  function updateRow(key: string, patch: Partial<SplitRow>) {
    setRows((current) => current.map((row) => (row.key === key ? { ...row, ...patch } : row)));
  }

  function addRow() {
    setRows((current) => [
      ...current,
      { key: crypto.randomUUID(), articleId: "none", amount: remainder > 0 ? String(remainder) : "" },
    ]);
  }

  function removeRow(key: string) {
    setRows((current) => (current.length > 1 ? current.filter((row) => row.key !== key) : current));
  }

  function submitSplit() {
    if (rows.some((row) => row.articleId === "none")) {
      toast.error("Выберите статью в каждой строке");
      return;
    }
    if (!balanced) {
      toast.error("Сумма по статьям должна равняться сумме операции");
      return;
    }
    mutation.mutate({
      action: "split",
      splits: rows.map((row) => ({ article_id: row.articleId, amount: row.amount })),
      remember_as_rule: rememberAsRule && rows.length === 1,
    });
  }

  const articles = articlesQuery.data ?? [];
  const isBusy = mutation.isPending;

  return (
    <Dialog open={Boolean(operation)} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex flex-wrap items-center gap-3">
            <span>Разбор операции</span>
            <DirectionBadge direction={operation.direction} />
            <DdsStatusBadge status={operation.classification_status} />
          </DialogTitle>
          <DialogDescription className="text-base font-medium text-foreground">
            {formatDate(operation.operation_date)} · {formatDdsMoney(operation.amount)}
          </DialogDescription>
        </DialogHeader>

        <div className="rounded-lg border bg-muted/20 p-3">
          <div className="text-xs font-medium uppercase text-muted-foreground">
            Назначение платежа
          </div>
          <div className="mt-1 break-words text-sm">{compactText(operation.payment_purpose)}</div>
        </div>

        {canClassify ? (
          <div className="grid gap-3 border-t pt-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <Label className="text-base font-semibold">Разнести по статьям ДДС</Label>
              <div className="text-sm tabular-nums text-muted-foreground">
                Распределено {formatDdsMoney(allocated)} из {formatDdsMoney(total)}
                {Math.abs(remainder) >= 0.005 ? (
                  <span className="ml-2 font-medium text-amber-600">
                    остаток {formatDdsMoney(remainder)}
                  </span>
                ) : (
                  <span className="ml-2 font-medium text-emerald-600">сходится ✓</span>
                )}
              </div>
            </div>

            <div className="grid gap-2">
              {rows.map((row) => (
                <div
                  key={row.key}
                  className="grid grid-cols-[minmax(0,1fr)_140px_auto] items-center gap-2"
                >
                  <Select
                    value={row.articleId}
                    onValueChange={(value) => updateRow(row.key, { articleId: value })}
                  >
                    <SelectTrigger>
                      <SelectValue placeholder="Статья ДДС" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="none">Статья не выбрана</SelectItem>
                      {articles.map((article) => (
                        <SelectItem key={article.id} value={article.id}>
                          {article.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <Input
                    className="text-right tabular-nums"
                    inputMode="decimal"
                    value={row.amount}
                    onChange={(event) => updateRow(row.key, { amount: event.target.value })}
                  />
                  <Button
                    disabled={rows.length === 1}
                    onClick={() => removeRow(row.key)}
                    size="icon"
                    title="Удалить строку"
                    type="button"
                    variant="ghost"
                  >
                    <Trash2 size={16} aria-hidden="true" />
                  </Button>
                </div>
              ))}
            </div>

            <div className="flex flex-wrap items-center justify-between gap-3">
              <Button onClick={addRow} size="sm" type="button" variant="outline">
                <Plus size={16} aria-hidden="true" />
                Добавить статью
              </Button>
              {rows.length === 1 ? (
                <label className="flex items-center gap-2 text-sm">
                  <input
                    checked={rememberAsRule}
                    className="h-4 w-4"
                    onChange={(event) => setRememberAsRule(event.target.checked)}
                    type="checkbox"
                  />
                  Запомнить как правило
                </label>
              ) : null}
            </div>

            <div className="flex flex-wrap gap-2 border-t pt-4">
              <Button disabled={isBusy || !balanced} onClick={submitSplit}>
                {isBusy ? (
                  <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                ) : null}
                Разнести
              </Button>
              <Button
                disabled={isBusy}
                onClick={() => mutation.mutate({ action: "mark_internal_transfer" })}
                variant="outline"
              >
                Внутренний перевод
              </Button>
              <Button
                disabled={isBusy}
                onClick={() => mutation.mutate({ action: "exclude" })}
                variant="outline"
              >
                Исключить
              </Button>
            </div>
          </div>
        ) : (
          <div className="rounded-md border bg-muted/40 px-3 py-2 text-sm text-muted-foreground">
            Режим просмотра. Разбор недоступен.
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
