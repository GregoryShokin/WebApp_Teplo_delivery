import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle } from "lucide-react";
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
import { apiErrorMessage } from "@/lib/api";
import { formatRub } from "@/routes/counterparties/shared";
import {
  createKassaPayin,
  getKassaPayinPresets,
  getKassaPayoutContext,
  updateKassaPayin,
  type KassaJournalItem,
} from "@/routes/kassa/api";

/** Запись журнала-внесение, открытая на правку (своя сегодняшняя). */
export type KassaPayinEditTarget = {
  transactionId: string;
  label: string | null;
  amount: number;
  comment: string | null;
};

type KassaPayinDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSaved: () => void;
  /** null — новое внесение по пресету; иначе правка своей сегодняшней записи. */
  editTarget?: KassaPayinEditTarget | null;
};

/** Форма «Внести деньги»: кассир выбирает пресет по имени, бэкенд знает статью ДДС. */
export function KassaPayinDialog({
  open,
  onOpenChange,
  onSaved,
  editTarget = null,
}: KassaPayinDialogProps) {
  const queryClient = useQueryClient();
  const [presetId, setPresetId] = useState("");
  const [amount, setAmount] = useState("");
  const [comment, setComment] = useState("");
  // Пользователь тронул комментарий вручную — не перетирать его шаблоном пресета.
  const [commentTouched, setCommentTouched] = useState(false);

  const isEdit = editTarget !== null;

  const presetsQuery = useQuery({
    queryKey: ["kassa", "payin-presets"],
    queryFn: getKassaPayinPresets,
    enabled: open && !isEdit,
  });
  const contextQuery = useQuery({
    queryKey: ["kassa", "payout-context"],
    queryFn: getKassaPayoutContext,
    enabled: open,
  });

  const preset = useMemo(
    () => (presetsQuery.data ?? []).find((item) => item.id === presetId) ?? null,
    [presetsQuery.data, presetId],
  );

  // Предзаполнение при открытии: правка — из записи журнала, создание — с чистого листа.
  useEffect(() => {
    if (!open) {
      return;
    }
    setPresetId("");
    setAmount(editTarget ? String(editTarget.amount) : "");
    setComment(editTarget?.comment ?? "");
    setCommentTouched(isEdit);
  }, [open, editTarget, isEdit]);

  // Смена пресета подставляет его шаблон в комментарий, пока кассир не правил вручную.
  useEffect(() => {
    if (isEdit || commentTouched) {
      return;
    }
    setComment(preset?.comment_template ?? "");
  }, [preset, isEdit, commentTouched]);

  const saveMutation = useMutation({
    mutationFn: () => {
      const trimmedComment = comment.trim() || null;
      if (editTarget) {
        return updateKassaPayin(editTarget.transactionId, {
          amount: Number(amount),
          comment: trimmedComment,
        });
      }
      return createKassaPayin({
        preset_id: presetId,
        amount: Number(amount),
        comment: trimmedComment,
      });
    },
    onSuccess: () => {
      toast.success(isEdit ? "Внесение исправлено" : "Внесение проведено — запись в журнале");
      void queryClient.invalidateQueries({ queryKey: ["kassa"] });
      void queryClient.invalidateQueries({ queryKey: ["dds"] });
      void queryClient.invalidateQueries({ queryKey: ["cp"] });
      onOpenChange(false);
      onSaved();
    },
    onError: (error) =>
      toast.error(
        apiErrorMessage(
          error,
          isEdit ? "Не удалось исправить внесение" : "Не удалось провести внесение",
        ),
      ),
  });

  const amountNumber = Number(amount);
  const canSave =
    (isEdit || Boolean(presetId)) &&
    Number.isFinite(amountNumber) &&
    amountNumber > 0 &&
    !saveMutation.isPending;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{isEdit ? "Изменить внесение" : "Внести деньги"}</DialogTitle>
          <DialogDescription>
            {contextQuery.data
              ? `Счёт: ${contextQuery.data.wallet_name} · в кассе ${formatRub(contextQuery.data.balance)}`
              : "Приход наличных в кассу. Дата операции — сегодня."}
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4">
          {isEdit ? (
            editTarget?.label ? (
              <div className="rounded-md bg-muted px-3 py-2 text-sm text-muted-foreground">
                {editTarget.label}
              </div>
            ) : null
          ) : (
            <div className="grid gap-2">
              <Label>Вид внесения</Label>
              <Select value={presetId} onValueChange={setPresetId}>
                <SelectTrigger>
                  <SelectValue
                    placeholder={presetsQuery.isLoading ? "Загрузка…" : "Выберите вид внесения"}
                  />
                </SelectTrigger>
                <SelectContent>
                  {(presetsQuery.data ?? []).map((item) => (
                    <SelectItem key={item.id} value={item.id}>
                      {item.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {!presetsQuery.isLoading && (presetsQuery.data ?? []).length === 0 ? (
                <p className="text-xs text-muted-foreground">
                  Виды внесений пока не заведены — их настраивает владелец в «Настройках → Касса».
                </p>
              ) : null}
            </div>
          )}

          <div className="grid gap-2">
            <Label>Сумма, ₽</Label>
            <Input
              type="number"
              min="0"
              step="0.01"
              value={amount}
              onChange={(event) => setAmount(event.target.value)}
            />
          </div>

          <div className="grid gap-2">
            <Label>Комментарий</Label>
            <Input
              value={comment}
              placeholder="Назначение внесения"
              onChange={(event) => {
                setCommentTouched(true);
                setComment(event.target.value);
              }}
            />
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Отмена
          </Button>
          <Button disabled={!canSave} onClick={() => saveMutation.mutate()}>
            {saveMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : null}
            {isEdit ? "Сохранить" : "Внести"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Собрать цель правки из строки журнала (только для editable payin-записей). */
export function payinEditTargetFromJournalItem(item: KassaJournalItem): KassaPayinEditTarget {
  return {
    transactionId: item.id,
    label: item.article_name,
    amount: item.amount,
    comment: item.comment ?? item.purpose,
  };
}
