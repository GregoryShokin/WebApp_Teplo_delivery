import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Plus, Trash2 } from "lucide-react";
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
import { Card, CardContent, CardHeader } from "@/components/ui/card";
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
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import {
  apiErrorMessage,
  createDdsArticle,
  createDdsArticleAlias,
  deleteDdsArticle,
  deleteDdsArticleAlias,
  getDdsArticles,
  patchDdsArticle,
  type DdsArticleCreate,
  type DdsArticleRead,
} from "@/lib/api";
import { MovementBadge, activityLabels, badgeMutedClass, compactText } from "@/routes/dds/shared";

type MovementType = DdsArticleCreate["movement_type"];

export function ArticlesTab({ canEdit }: { canEdit: boolean }) {
  const queryClient = useQueryClient();
  const articlesQuery = useQuery({ queryKey: ["dds", "articles"], queryFn: getDdsArticles });
  const [isCreateOpen, setIsCreateOpen] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<DdsArticleRead | null>(null);

  const selected = useMemo(
    () => (articlesQuery.data ?? []).find((article) => article.id === selectedId) ?? null,
    [articlesQuery.data, selectedId],
  );
  const groups = groupArticles(articlesQuery.data ?? []);

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteDdsArticle(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["dds", "articles"] });
      setDeleteTarget(null);
      setSelectedId(null);
      toast.success("Статья удалена");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось удалить статью")),
  });

  const columns: Array<DataTableColumn<DdsArticleRead>> = [
    {
      key: "name",
      header: "Название",
      cell: (article) => <div className="font-medium">{article.name}</div>,
      className: "min-w-[220px]",
    },
    {
      key: "movement",
      header: "Движение",
      cell: (article) => <MovementBadge movement={article.movement_type} />,
    },
    {
      key: "active",
      header: "Активна",
      cell: (article) => (
        <Badge className={badgeMutedClass(article.is_active)}>
          {article.is_active ? "Да" : "Нет"}
        </Badge>
      ),
    },
  ];

  return (
    <div className="space-y-5">
      {canEdit ? (
        <div className="flex justify-end">
          <Button onClick={() => setIsCreateOpen(true)}>
            <Plus size={16} aria-hidden="true" />
            Добавить
          </Button>
        </div>
      ) : null}

      {articlesQuery.isLoading ? (
        <DataTable columns={columns} rows={[]} isLoading />
      ) : (
        <div className="grid gap-4">
          {groups.map(([activityType, articles]) => (
            <Card key={activityType}>
              <CardHeader className="pb-3">
                <div className="flex items-center justify-between gap-3">
                  <h3 className="text-base font-semibold">
                    {activityLabels[activityType] ?? activityType}
                  </h3>
                  <Badge variant="outline">{articles.length}</Badge>
                </div>
              </CardHeader>
              <CardContent>
                <DataTable
                  columns={columns}
                  rows={articles}
                  getRowKey={(article) => article.id}
                  onRowClick={(article) => setSelectedId(article.id)}
                  emptyMessage="Статей нет"
                />
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      <ArticleDialog open={canEdit && isCreateOpen} onOpenChange={setIsCreateOpen} />
      <ArticleSheet
        article={selected}
        canEdit={canEdit}
        onClose={() => setSelectedId(null)}
        onDelete={setDeleteTarget}
      />

      <AlertDialog open={Boolean(deleteTarget)} onOpenChange={() => setDeleteTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Удалить статью?</AlertDialogTitle>
            <AlertDialogDescription>
              Статья станет неактивной, исторические транзакции останутся на месте.
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
    </div>
  );
}

function ArticleDialog({
  onOpenChange,
  open,
}: {
  onOpenChange: (open: boolean) => void;
  open: boolean;
}) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState(articleDraft());
  const createMutation = useMutation({
    mutationFn: () => createDdsArticle(toArticlePayload(draft)),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["dds", "articles"] });
      setDraft(articleDraft());
      onOpenChange(false);
      toast.success("Статья добавлена");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось добавить статью")),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Новая статья ДДС</DialogTitle>
        </DialogHeader>
        <ArticleForm draft={draft} onDraftChange={setDraft} />
        <DialogFooter>
          <Button
            disabled={!draft.name.trim() || createMutation.isPending}
            onClick={() => createMutation.mutate()}
          >
            {createMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : null}
            Сохранить
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ArticleSheet({
  article,
  canEdit,
  onClose,
  onDelete,
}: {
  article: DdsArticleRead | null;
  canEdit: boolean;
  onClose: () => void;
  onDelete: (article: DdsArticleRead) => void;
}) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState(articleDraft());
  const [alias, setAlias] = useState("");

  useEffect(() => {
    if (!article) {
      return;
    }
    setDraft({
      activity_type: article.activity_type,
      code: article.code,
      description: article.description ?? "",
      is_active: article.is_active,
      kassa_enabled: article.kassa_enabled,
      movement_type: article.movement_type as MovementType,
      name: article.name,
      parent_id: article.parent_id ?? "none",
    });
  }, [article]);

  const patchMutation = useMutation({
    mutationFn: () =>
      article
        ? patchDdsArticle(article.id, toArticlePayload(draft))
        : Promise.reject(new Error("Статья не выбрана")),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["dds", "articles"] });
      toast.success("Статья сохранена");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить статью")),
  });

  const aliasMutation = useMutation({
    mutationFn: () =>
      article
        ? createDdsArticleAlias(article.id, { alias })
        : Promise.reject(new Error("Статья не выбрана")),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["dds", "articles"] });
      setAlias("");
      toast.success("Синоним добавлен");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось добавить синоним")),
  });

  const deleteAliasMutation = useMutation({
    mutationFn: deleteDdsArticleAlias,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["dds", "articles"] });
      toast.success("Синоним удалён");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось удалить синоним")),
  });

  return (
    <Sheet open={Boolean(article)} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-full overflow-y-auto sm:max-w-xl">
        <SheetHeader>
          <SheetTitle>Статья ДДС</SheetTitle>
          <SheetDescription>{article?.name}</SheetDescription>
        </SheetHeader>
        {article ? (
          <div className="mt-5 space-y-5">
            <ArticleForm disabled={!canEdit} draft={draft} onDraftChange={setDraft} />
            {canEdit ? (
              <div className="flex flex-wrap gap-2">
                <Button disabled={patchMutation.isPending} onClick={() => patchMutation.mutate()}>
                  {patchMutation.isPending ? (
                    <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
                  ) : null}
                  Сохранить
                </Button>
                <Button onClick={() => onDelete(article)} variant="outline">
                  <Trash2 size={16} aria-hidden="true" />
                  Удалить
                </Button>
              </div>
            ) : null}
            {canEdit || article.aliases.length > 0 ? (
            <div className="space-y-3 border-t pt-5">
              <h3 className="text-sm font-semibold">Синонимы</h3>
              {canEdit ? (
                <div className="flex gap-2">
                  <Input value={alias} onChange={(event) => setAlias(event.target.value)} />
                  <Button
                    disabled={!alias.trim() || aliasMutation.isPending}
                    onClick={() => aliasMutation.mutate()}
                    variant="outline"
                  >
                    Добавить
                  </Button>
                </div>
              ) : null}
              <div className="grid gap-2">
                {article.aliases.map((item) => (
                  <div
                    className="flex items-center justify-between rounded-md border p-2 text-sm"
                    key={item.id}
                  >
                    <span className="min-w-0 truncate">{item.alias}</span>
                    {canEdit ? (
                      <Button
                        onClick={() => deleteAliasMutation.mutate(item.id)}
                        size="icon"
                        title="Удалить alias"
                        variant="ghost"
                      >
                        <Trash2 size={15} aria-hidden="true" />
                      </Button>
                    ) : null}
                  </div>
                ))}
              </div>
            </div>
            ) : null}
          </div>
        ) : null}
      </SheetContent>
    </Sheet>
  );
}

type ArticleDraft = {
  activity_type: string;
  code: string;
  description: string;
  is_active: boolean;
  kassa_enabled: boolean;
  movement_type: MovementType;
  name: string;
  parent_id: string;
};

function ArticleForm({
  disabled = false,
  draft,
  onDraftChange,
}: {
  disabled?: boolean;
  draft: ArticleDraft;
  onDraftChange: (draft: ArticleDraft) => void;
}) {
  const setField = <K extends keyof ArticleDraft>(key: K, value: ArticleDraft[K]) =>
    onDraftChange({ ...draft, [key]: value });

  const activityValue = normalizeActivity(draft.activity_type);
  const activityOptions = ACTIVITY_ORDER.includes(activityValue)
    ? ACTIVITY_ORDER
    : [activityValue, ...ACTIVITY_ORDER];

  return (
    <div className="grid gap-4">
      <div className="grid gap-2">
        <Label>Название</Label>
        <Input
          disabled={disabled}
          value={draft.name}
          onChange={(event) => setField("name", event.target.value)}
        />
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="grid gap-2">
          <Label>Тип движения</Label>
          <Select
            disabled={disabled}
            value={draft.movement_type}
            onValueChange={(value) => setField("movement_type", value as MovementType)}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="inflow">Поступление</SelectItem>
              <SelectItem value="outflow">Списание</SelectItem>
              <SelectItem value="internal">Внутреннее</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="grid gap-2">
          <Label>Вид деятельности</Label>
          <Select
            disabled={disabled}
            value={activityValue}
            onValueChange={(value) => setField("activity_type", value)}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {activityOptions.map((option) => (
                <SelectItem key={option} value={option}>
                  {activityLabels[option] ?? option}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>
      <div className="grid gap-2">
        <Label>Описание</Label>
        <Input
          disabled={disabled}
          value={draft.description}
          onChange={(event) => setField("description", event.target.value)}
        />
      </div>
      <label className="flex items-center gap-2 text-sm">
        <input
          checked={draft.is_active}
          className="h-4 w-4"
          disabled={disabled}
          onChange={(event) => setField("is_active", event.target.checked)}
          type="checkbox"
        />
        Активна
      </label>
      <div className="grid gap-1">
        <label className="flex items-center gap-2 text-sm">
          <input
            checked={draft.kassa_enabled}
            className="h-4 w-4"
            disabled={
              disabled ||
              (!draft.kassa_enabled &&
                (draft.movement_type === "internal" ||
                  (draft.movement_type === "inflow" && draft.activity_type === "technical")))
            }
            onChange={(event) => setField("kassa_enabled", event.target.checked)}
            type="checkbox"
          />
          Доступна в кассе
        </label>
        <p className="text-xs text-muted-foreground">
          Расходная статья появится в форме «Выплата из кассы», приходная — в форме «Внести деньги».
          Статьям с собственными контурами и служебным переводам флаг не включить.
        </p>
      </div>
    </div>
  );
}

function articleDraft(): ArticleDraft {
  return {
    activity_type: "operating",
    code: "",
    description: "",
    is_active: true,
    kassa_enabled: false,
    movement_type: "outflow",
    name: "",
    parent_id: "none",
  };
}

function toArticlePayload(draft: ArticleDraft): DdsArticleCreate {
  return {
    activity_type: draft.activity_type,
    code: draft.code.trim() || generateArticleCode(draft.name),
    description: compactText(draft.description, ""),
    is_active: draft.is_active,
    kassa_enabled: draft.kassa_enabled,
    movement_type: draft.movement_type,
    name: draft.name,
    parent_id: draft.parent_id === "none" ? null : draft.parent_id,
  };
}

const CODE_TRANSLIT: Record<string, string> = {
  а: "a", б: "b", в: "v", г: "g", д: "d", е: "e", ё: "e", ж: "zh", з: "z",
  и: "i", й: "i", к: "k", л: "l", м: "m", н: "n", о: "o", п: "p", р: "r",
  с: "s", т: "t", у: "u", ф: "f", х: "h", ц: "c", ч: "ch", ш: "sh", щ: "sch",
  ъ: "", ы: "y", ь: "", э: "e", ю: "yu", я: "ya",
};

function generateArticleCode(name: string): string {
  const base = name
    .toLowerCase()
    .split("")
    .map((char) => CODE_TRANSLIT[char] ?? char)
    .join("")
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 48);
  const suffix = (globalThis.crypto?.randomUUID?.() ?? `${Date.now()}`)
    .replace(/[^a-z0-9]/gi, "")
    .slice(0, 6)
    .toLowerCase();
  return base ? `${base}_${suffix}` : `article_${suffix}`;
}

const ACTIVITY_ORDER = ["operating", "financing", "investing", "technical"];

function normalizeActivity(activityType: string) {
  return activityType === "internal" ? "technical" : activityType;
}

function groupArticles(articles: DdsArticleRead[]) {
  const groups = new Map<string, DdsArticleRead[]>();
  articles.forEach((article) => {
    const key = normalizeActivity(article.activity_type);
    const group = groups.get(key) ?? [];
    group.push(article);
    groups.set(key, group);
  });
  return [...groups.entries()].sort(([left], [right]) => {
    const leftIndex = ACTIVITY_ORDER.indexOf(left);
    const rightIndex = ACTIVITY_ORDER.indexOf(right);
    if (leftIndex === -1 && rightIndex === -1) {
      return left.localeCompare(right, "ru");
    }
    if (leftIndex === -1) {
      return 1;
    }
    if (rightIndex === -1) {
      return -1;
    }
    return leftIndex - rightIndex;
  });
}
