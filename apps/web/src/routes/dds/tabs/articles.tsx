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
import { MovementBadge, badgeMutedClass, compactText } from "@/routes/dds/shared";

type MovementType = DdsArticleCreate["movement_type"];

export function ArticlesTab() {
  const queryClient = useQueryClient();
  const articlesQuery = useQuery({ queryKey: ["dds", "articles"], queryFn: getDdsArticles });
  const [isCreateOpen, setIsCreateOpen] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<DdsArticleRead | null>(null);

  const selected = useMemo(
    () => (articlesQuery.data ?? []).find((article) => article.id === selectedId) ?? null,
    [articlesQuery.data, selectedId],
  );
  const articlesById = new Map((articlesQuery.data ?? []).map((article) => [article.id, article]));
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
    { key: "code", header: "Код", cell: (article) => article.code, className: "font-mono" },
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
      key: "parent",
      header: "Родитель",
      cell: (article) =>
        article.parent_id ? articlesById.get(article.parent_id)?.name ?? article.parent_id : "—",
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
      <div className="flex justify-end">
        <Button onClick={() => setIsCreateOpen(true)}>
          <Plus size={16} aria-hidden="true" />
          Добавить
        </Button>
      </div>

      {articlesQuery.isLoading ? (
        <DataTable columns={columns} rows={[]} isLoading />
      ) : (
        <div className="grid gap-4">
          {groups.map(([activityType, articles]) => (
            <Card key={activityType}>
              <CardHeader className="pb-3">
                <div className="flex items-center justify-between gap-3">
                  <h3 className="text-base font-semibold">{activityType}</h3>
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

      <ArticleDialog open={isCreateOpen} onOpenChange={setIsCreateOpen} articles={articlesQuery.data ?? []} />
      <ArticleSheet
        article={selected}
        articles={articlesQuery.data ?? []}
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
  articles,
  onOpenChange,
  open,
}: {
  articles: DdsArticleRead[];
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
        <ArticleForm articles={articles} draft={draft} onDraftChange={setDraft} />
        <DialogFooter>
          <Button
            disabled={!draft.code.trim() || !draft.name.trim() || createMutation.isPending}
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
  articles,
  onClose,
  onDelete,
}: {
  article: DdsArticleRead | null;
  articles: DdsArticleRead[];
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
      toast.success("Alias добавлен");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось добавить alias")),
  });

  const deleteAliasMutation = useMutation({
    mutationFn: deleteDdsArticleAlias,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["dds", "articles"] });
      toast.success("Alias удалён");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось удалить alias")),
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
            <ArticleForm articles={articles.filter((item) => item.id !== article.id)} draft={draft} onDraftChange={setDraft} />
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
            <div className="space-y-3 border-t pt-5">
              <h3 className="text-sm font-semibold">Aliases</h3>
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
              <div className="grid gap-2">
                {article.aliases.map((item) => (
                  <div
                    className="flex items-center justify-between rounded-md border p-2 text-sm"
                    key={item.id}
                  >
                    <span className="min-w-0 truncate">{item.alias}</span>
                    <Button
                      onClick={() => deleteAliasMutation.mutate(item.id)}
                      size="icon"
                      title="Удалить alias"
                      variant="ghost"
                    >
                      <Trash2 size={15} aria-hidden="true" />
                    </Button>
                  </div>
                ))}
              </div>
            </div>
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
  movement_type: MovementType;
  name: string;
  parent_id: string;
};

function ArticleForm({
  articles,
  draft,
  onDraftChange,
}: {
  articles: DdsArticleRead[];
  draft: ArticleDraft;
  onDraftChange: (draft: ArticleDraft) => void;
}) {
  const setField = <K extends keyof ArticleDraft>(key: K, value: ArticleDraft[K]) =>
    onDraftChange({ ...draft, [key]: value });

  return (
    <div className="grid gap-4">
      <div className="grid gap-2">
        <Label>Код</Label>
        <Input value={draft.code} onChange={(event) => setField("code", event.target.value)} />
      </div>
      <div className="grid gap-2">
        <Label>Название</Label>
        <Input value={draft.name} onChange={(event) => setField("name", event.target.value)} />
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="grid gap-2">
          <Label>Тип движения</Label>
          <Select
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
          <Label>Activity type</Label>
          <Input
            value={draft.activity_type}
            onChange={(event) => setField("activity_type", event.target.value)}
          />
        </div>
      </div>
      <div className="grid gap-2">
        <Label>Родитель</Label>
        <Select value={draft.parent_id} onValueChange={(value) => setField("parent_id", value)}>
          <SelectTrigger>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="none">Нет</SelectItem>
            {articles.map((article) => (
              <SelectItem key={article.id} value={article.id}>
                {article.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div className="grid gap-2">
        <Label>Описание</Label>
        <Input
          value={draft.description}
          onChange={(event) => setField("description", event.target.value)}
        />
      </div>
      <label className="flex items-center gap-2 text-sm">
        <input
          checked={draft.is_active}
          className="h-4 w-4"
          onChange={(event) => setField("is_active", event.target.checked)}
          type="checkbox"
        />
        Активна
      </label>
    </div>
  );
}

function articleDraft(): ArticleDraft {
  return {
    activity_type: "operating",
    code: "",
    description: "",
    is_active: true,
    movement_type: "outflow",
    name: "",
    parent_id: "none",
  };
}

function toArticlePayload(draft: ArticleDraft): DdsArticleCreate {
  return {
    activity_type: draft.activity_type,
    code: draft.code,
    description: compactText(draft.description, ""),
    is_active: draft.is_active,
    movement_type: draft.movement_type,
    name: draft.name,
    parent_id: draft.parent_id === "none" ? null : draft.parent_id,
  };
}

function groupArticles(articles: DdsArticleRead[]) {
  const groups = new Map<string, DdsArticleRead[]>();
  articles.forEach((article) => {
    const group = groups.get(article.activity_type) ?? [];
    group.push(article);
    groups.set(article.activity_type, group);
  });
  return [...groups.entries()].sort(([left], [right]) => left.localeCompare(right, "ru"));
}
