import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Pencil, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
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
import { Switch } from "@/components/ui/switch";
import { apiErrorMessage } from "@/lib/api";
import {
  createKassaPayinPreset,
  deleteKassaPayinPreset,
  getKassaPayinPresetArticles,
  getKassaPayinPresetCounterparties,
  getKassaPayinPresetsAll,
  updateKassaPayinPreset,
  type KassaPayinPreset,
} from "@/routes/kassa/api";

const NO_COUNTERPARTY = "__none__";

/** Каталог «видов внесения» (Настройки → Касса): имя → статья ДДС + контрагент + шаблон. */
export function PayinPresetsPanel({ canEdit }: { canEdit: boolean }) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<KassaPayinPreset | "new" | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<KassaPayinPreset | null>(null);

  const presetsQuery = useQuery({
    queryKey: ["kassa", "payin-presets-all"],
    queryFn: getKassaPayinPresetsAll,
  });

  const deleteMutation = useMutation({
    mutationFn: (presetId: string) => deleteKassaPayinPreset(presetId),
    onSuccess: async () => {
      toast.success("Пресет удалён");
      setDeleteTarget(null);
      await queryClient.invalidateQueries({ queryKey: ["kassa", "payin-presets-all"] });
      await queryClient.invalidateQueries({ queryKey: ["kassa", "payin-presets"] });
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось удалить пресет")),
  });

  const presets = presetsQuery.data ?? [];

  return (
    <section className="space-y-3">
      <div className="flex flex-col gap-1 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 className="text-lg font-semibold tracking-normal">Виды внесения в кассу</h2>
          <p className="text-sm text-muted-foreground">
            Кассир выбирает вид по имени, а система сама проводит приход по нужной статье ДДС.
          </p>
        </div>
        {canEdit ? (
          <Button onClick={() => setEditing("new")} size="sm" type="button" variant="outline">
            <Plus size={15} aria-hidden="true" />
            Добавить вид
          </Button>
        ) : null}
      </div>

      <div className="grid gap-2 rounded-lg border bg-card p-4 shadow-sm">
        {presetsQuery.isLoading ? (
          <div className="text-sm text-muted-foreground">Загрузка видов внесения…</div>
        ) : null}

        {!presetsQuery.isLoading && presets.length === 0 ? (
          <div className="rounded-md border bg-muted/30 px-3 py-2 text-sm text-muted-foreground">
            Виды внесения пока не заведены.
          </div>
        ) : null}

        {presets.map((preset) => (
          <div
            className="flex items-start justify-between gap-3 rounded-md border bg-background p-3"
            key={preset.id}
          >
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span className="font-medium">{preset.name}</span>
                {!preset.is_active ? (
                  <Badge className="border-muted bg-muted text-muted-foreground">выключен</Badge>
                ) : null}
              </div>
              <div className="mt-0.5 text-xs text-muted-foreground">
                Статья: {preset.article_name}
                {preset.counterparty_name ? ` · ${preset.counterparty_name}` : ""}
                {preset.comment_template ? ` · «${preset.comment_template}»` : ""}
              </div>
            </div>
            {canEdit ? (
              <div className="flex shrink-0 gap-1">
                <Button
                  onClick={() => setEditing(preset)}
                  size="icon"
                  title="Изменить"
                  type="button"
                  variant="ghost"
                >
                  <Pencil size={16} aria-hidden="true" />
                </Button>
                <Button
                  onClick={() => setDeleteTarget(preset)}
                  size="icon"
                  title="Удалить"
                  type="button"
                  variant="ghost"
                >
                  <Trash2 size={16} aria-hidden="true" />
                </Button>
              </div>
            ) : null}
          </div>
        ))}
      </div>

      <PayinPresetDialog
        open={editing !== null}
        preset={editing === "new" ? null : editing}
        onOpenChange={(open) => !open && setEditing(null)}
        onSaved={() => setEditing(null)}
      />

      <AlertDialog open={Boolean(deleteTarget)} onOpenChange={() => setDeleteTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Удалить вид внесения?</AlertDialogTitle>
            <AlertDialogDescription>
              «{deleteTarget?.name}» пропадёт из формы кассира. Уже проведённые по нему внесения
              останутся в журнале.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Отмена</AlertDialogCancel>
            <AlertDialogAction
              disabled={deleteMutation.isPending}
              onClick={() => deleteTarget && deleteMutation.mutate(deleteTarget.id)}
            >
              Удалить
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}

function PayinPresetDialog({
  open,
  preset,
  onOpenChange,
  onSaved,
}: {
  open: boolean;
  preset: KassaPayinPreset | null;
  onOpenChange: (open: boolean) => void;
  onSaved: () => void;
}) {
  const queryClient = useQueryClient();
  const isEdit = preset !== null;

  const [name, setName] = useState("");
  const [articleId, setArticleId] = useState("");
  const [counterpartyId, setCounterpartyId] = useState<string>(NO_COUNTERPARTY);
  const [commentTemplate, setCommentTemplate] = useState("");
  const [isActive, setIsActive] = useState(true);
  const [sortOrder, setSortOrder] = useState("0");

  const articlesQuery = useQuery({
    queryKey: ["kassa", "payin-preset-articles"],
    queryFn: getKassaPayinPresetArticles,
    enabled: open,
  });
  const counterpartiesQuery = useQuery({
    queryKey: ["kassa", "payin-preset-counterparties"],
    queryFn: getKassaPayinPresetCounterparties,
    enabled: open,
  });

  useEffect(() => {
    if (!open) {
      return;
    }
    setName(preset?.name ?? "");
    setArticleId(preset?.article_id ?? "");
    setCounterpartyId(preset?.counterparty_id ?? NO_COUNTERPARTY);
    setCommentTemplate(preset?.comment_template ?? "");
    setIsActive(preset?.is_active ?? true);
    setSortOrder(String(preset?.sort_order ?? 0));
  }, [open, preset]);

  const saveMutation = useMutation({
    mutationFn: () => {
      const payload = {
        name: name.trim(),
        article_id: articleId,
        counterparty_id: counterpartyId === NO_COUNTERPARTY ? null : counterpartyId,
        comment_template: commentTemplate.trim() || null,
        is_active: isActive,
        sort_order: Number(sortOrder) || 0,
      };
      return isEdit
        ? updateKassaPayinPreset(preset.id, payload)
        : createKassaPayinPreset(payload);
    },
    onSuccess: async () => {
      toast.success(isEdit ? "Вид внесения обновлён" : "Вид внесения создан");
      await queryClient.invalidateQueries({ queryKey: ["kassa", "payin-presets-all"] });
      await queryClient.invalidateQueries({ queryKey: ["kassa", "payin-presets"] });
      onOpenChange(false);
      onSaved();
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить вид внесения")),
  });

  const canSave =
    name.trim().length > 0 && Boolean(articleId) && !saveMutation.isPending;

  const counterparties = useMemo(() => {
    const active = counterpartiesQuery.data ?? [];
    // Правка пресета с уже деактивированным контрагентом: его нет в активном списке —
    // добавляем вручную, иначе Select покажет пустой плейсхолдер вместо имени.
    if (preset?.counterparty_id && !active.some((item) => item.id === preset.counterparty_id)) {
      return [
        { id: preset.counterparty_id, name: preset.counterparty_name ?? "(контрагент)" },
        ...active,
      ];
    }
    return active;
  }, [counterpartiesQuery.data, preset]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{isEdit ? "Изменить вид внесения" : "Новый вид внесения"}</DialogTitle>
          <DialogDescription>
            Имя видит кассир; статья ДДС и контрагент подставляются автоматически.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4">
          <div className="grid gap-2">
            <Label>Название (видит кассир)</Label>
            <Input
              value={name}
              placeholder="Например, «Деньги за масло»"
              onChange={(event) => setName(event.target.value)}
            />
          </div>

          <div className="grid gap-2">
            <Label>Статья ДДС (приход)</Label>
            <Select value={articleId} onValueChange={setArticleId}>
              <SelectTrigger>
                <SelectValue
                  placeholder={articlesQuery.isLoading ? "Загрузка…" : "Выберите статью прихода"}
                />
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
            <Label>Контрагент (необязательно)</Label>
            <Select value={counterpartyId} onValueChange={setCounterpartyId}>
              <SelectTrigger>
                <SelectValue
                  placeholder={counterpartiesQuery.isLoading ? "Загрузка…" : "Без контрагента"}
                />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NO_COUNTERPARTY}>Без контрагента</SelectItem>
                {counterparties.map((counterparty) => (
                  <SelectItem key={counterparty.id} value={counterparty.id}>
                    {counterparty.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="grid gap-2">
            <Label>Шаблон комментария</Label>
            <Input
              value={commentTemplate}
              placeholder="Например, «Плата за масло»"
              onChange={(event) => setCommentTemplate(event.target.value)}
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="grid gap-2">
              <Label>Порядок</Label>
              <Input
                type="number"
                value={sortOrder}
                onChange={(event) => setSortOrder(event.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <Label>Активен</Label>
              <div className="flex h-10 items-center gap-2 rounded-md border border-input bg-background px-3">
                <Switch checked={isActive} onCheckedChange={setIsActive} />
                <span className="text-sm text-muted-foreground">{isActive ? "Да" : "Нет"}</span>
              </div>
            </div>
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
            Сохранить
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
