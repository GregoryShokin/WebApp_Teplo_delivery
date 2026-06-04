import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Pencil, Plus, Power, Trash2 } from "lucide-react";
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
import { DataTable, type DataTableColumn } from "@/components/ui-app/DataTable";
import {
  apiErrorMessage,
  createClassificationRule,
  deleteClassificationRule,
  getDdsArticles,
  getDdsClassificationRules,
  patchClassificationRule,
  toggleClassificationRule,
  type ClassificationRuleCreate,
  type ClassificationRuleRead,
  type DdsProvider,
} from "@/lib/api";
import { DdsStatusBadge, DirectionBadge, ProviderBadge, badgeMutedClass, compactText } from "@/routes/dds/shared";

type RuleDraft = {
  action: ClassificationRuleCreate["action"];
  amount_max: string;
  amount_min: string;
  article_id: string;
  counterparty_inn_match: string;
  counterparty_name_pattern: string;
  direction: "any" | "in" | "out";
  name: string;
  priority: string;
  provider: "any" | DdsProvider;
  purpose_pattern: string;
};

export function RulesTab() {
  const queryClient = useQueryClient();
  const rulesQuery = useQuery({
    queryKey: ["dds", "classification-rules"],
    queryFn: getDdsClassificationRules,
  });
  const articlesQuery = useQuery({ queryKey: ["dds", "articles"], queryFn: getDdsArticles });
  const [editingRule, setEditingRule] = useState<ClassificationRuleRead | null>(null);
  const [isCreateOpen, setIsCreateOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<ClassificationRuleRead | null>(null);
  const articlesById = new Map((articlesQuery.data ?? []).map((article) => [article.id, article]));

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteClassificationRule(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["dds", "classification-rules"] });
      setDeleteTarget(null);
      toast.success("Правило удалено");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось удалить правило")),
  });

  const toggleMutation = useMutation({
    mutationFn: (id: string) => toggleClassificationRule(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["dds", "classification-rules"] });
      toast.success("Статус правила обновлён");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось переключить правило")),
  });

  const columns: Array<DataTableColumn<ClassificationRuleRead>> = [
    {
      key: "priority",
      header: "Priority",
      cell: (rule) => rule.priority,
      className: "tabular-nums",
    },
    {
      key: "name",
      header: "Название",
      cell: (rule) => <div className="font-medium">{rule.name}</div>,
      className: "min-w-[220px]",
    },
    { key: "provider", header: "Провайдер", cell: (rule) => <ProviderBadge provider={rule.provider} /> },
    { key: "direction", header: "Направление", cell: (rule) => <DirectionBadge direction={rule.direction} /> },
    {
      key: "patterns",
      header: "Matching",
      cell: (rule) => (
        <div className="grid max-w-[360px] gap-1 text-xs text-muted-foreground">
          <span>ИНН: {compactText(rule.counterparty_inn_match)}</span>
          <span>Имя: {compactText(rule.counterparty_name_pattern)}</span>
          <span>Назначение: {compactText(rule.purpose_pattern)}</span>
        </div>
      ),
    },
    {
      key: "action",
      header: "Action",
      cell: (rule) => <DdsStatusBadge status={rule.action} />,
    },
    {
      key: "article",
      header: "Статья",
      cell: (rule) =>
        rule.article_id ? articlesById.get(rule.article_id)?.name ?? rule.article_id : "—",
    },
    {
      key: "active",
      header: "Активно",
      cell: (rule) => (
        <Badge className={badgeMutedClass(rule.is_active)}>{rule.is_active ? "Да" : "Нет"}</Badge>
      ),
    },
    {
      key: "actions",
      header: "",
      className: "text-right",
      cell: (rule) => (
        <div className="flex justify-end gap-1">
          <Button
            onClick={(event) => {
              event.stopPropagation();
              setEditingRule(rule);
            }}
            size="icon"
            title="Редактировать"
            variant="ghost"
          >
            <Pencil size={15} aria-hidden="true" />
          </Button>
          <Button
            onClick={(event) => {
              event.stopPropagation();
              toggleMutation.mutate(rule.id);
            }}
            size="icon"
            title="Переключить"
            variant="ghost"
          >
            <Power size={15} aria-hidden="true" />
          </Button>
          <Button
            onClick={(event) => {
              event.stopPropagation();
              setDeleteTarget(rule);
            }}
            size="icon"
            title="Удалить"
            variant="ghost"
          >
            <Trash2 size={15} aria-hidden="true" />
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-5">
      <div className="flex justify-end">
        <Button onClick={() => setIsCreateOpen(true)}>
          <Plus size={16} aria-hidden="true" />
          Добавить правило
        </Button>
      </div>

      <DataTable
        columns={columns}
        rows={[...(rulesQuery.data ?? [])].sort((left, right) => left.priority - right.priority)}
        isLoading={rulesQuery.isLoading}
        getRowKey={(rule) => rule.id}
        onRowClick={setEditingRule}
        emptyMessage="Правила не найдены"
      />

      <RuleDialog
        articles={articlesQuery.data ?? []}
        onOpenChange={setIsCreateOpen}
        open={isCreateOpen}
      />
      <RuleDialog
        articles={articlesQuery.data ?? []}
        onOpenChange={(open) => !open && setEditingRule(null)}
        open={Boolean(editingRule)}
        rule={editingRule}
      />

      <AlertDialog open={Boolean(deleteTarget)} onOpenChange={() => setDeleteTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Удалить правило?</AlertDialogTitle>
            <AlertDialogDescription>
              Правило станет неактивным и перестанет участвовать в классификации.
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

function RuleDialog({
  articles,
  onOpenChange,
  open,
  rule,
}: {
  articles: Array<{ id: string; code: string; name: string }>;
  onOpenChange: (open: boolean) => void;
  open: boolean;
  rule?: ClassificationRuleRead | null;
}) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState(ruleDraft());

  useEffect(() => {
    if (rule) {
      setDraft({
        action: rule.action as ClassificationRuleCreate["action"],
        amount_max: rule.amount_max ?? "",
        amount_min: rule.amount_min ?? "",
        article_id: rule.article_id ?? "none",
        counterparty_inn_match: rule.counterparty_inn_match ?? "",
        counterparty_name_pattern: rule.counterparty_name_pattern ?? "",
        direction: rule.direction ?? "any",
        name: rule.name,
        priority: String(rule.priority),
        provider: rule.provider ?? "any",
        purpose_pattern: rule.purpose_pattern ?? "",
      });
      return;
    }
    if (open) {
      setDraft(ruleDraft());
    }
  }, [open, rule]);

  const saveMutation = useMutation({
    mutationFn: () =>
      rule
        ? patchClassificationRule(rule.id, toRulePayload(draft))
        : createClassificationRule(toRulePayload(draft)),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["dds", "classification-rules"] });
      onOpenChange(false);
      toast.success(rule ? "Правило сохранено" : "Правило добавлено");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось сохранить правило")),
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>{rule ? "Редактировать правило" : "Новое правило"}</DialogTitle>
        </DialogHeader>
        <RuleForm articles={articles} draft={draft} onDraftChange={setDraft} />
        <DialogFooter>
          <Button
            disabled={!draft.name.trim() || saveMutation.isPending}
            onClick={() => saveMutation.mutate()}
          >
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

function RuleForm({
  articles,
  draft,
  onDraftChange,
}: {
  articles: Array<{ id: string; code: string; name: string }>;
  draft: RuleDraft;
  onDraftChange: (draft: RuleDraft) => void;
}) {
  const setField = <K extends keyof RuleDraft>(key: K, value: RuleDraft[K]) =>
    onDraftChange({ ...draft, [key]: value });

  return (
    <div className="grid gap-4">
      <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_120px]">
        <div className="grid gap-2">
          <Label>Название</Label>
          <Input value={draft.name} onChange={(event) => setField("name", event.target.value)} />
        </div>
        <div className="grid gap-2">
          <Label>Priority</Label>
          <Input
            type="number"
            value={draft.priority}
            onChange={(event) => setField("priority", event.target.value)}
          />
        </div>
      </div>
      <div className="grid gap-3 md:grid-cols-3">
        <div className="grid gap-2">
          <Label>Провайдер</Label>
          <Select value={draft.provider} onValueChange={(value) => setField("provider", value as RuleDraft["provider"])}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="any">Любой</SelectItem>
              <SelectItem value="sber">Сбер</SelectItem>
              <SelectItem value="tbank">T-Bank</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="grid gap-2">
          <Label>Направление</Label>
          <Select value={draft.direction} onValueChange={(value) => setField("direction", value as RuleDraft["direction"])}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="any">Любое</SelectItem>
              <SelectItem value="in">Поступление</SelectItem>
              <SelectItem value="out">Списание</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="grid gap-2">
          <Label>Action</Label>
          <Select value={draft.action} onValueChange={(value) => setField("action", value as RuleDraft["action"])}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="set_article">set_article</SelectItem>
              <SelectItem value="mark_internal_transfer">mark_internal_transfer</SelectItem>
              <SelectItem value="exclude">exclude</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>
      <div className="grid gap-3 md:grid-cols-3">
        <div className="grid gap-2">
          <Label>ИНН</Label>
          <Input value={draft.counterparty_inn_match} onChange={(event) => setField("counterparty_inn_match", event.target.value)} />
        </div>
        <div className="grid gap-2">
          <Label>Имя контрагента</Label>
          <Input value={draft.counterparty_name_pattern} onChange={(event) => setField("counterparty_name_pattern", event.target.value)} />
        </div>
        <div className="grid gap-2">
          <Label>Назначение</Label>
          <Input value={draft.purpose_pattern} onChange={(event) => setField("purpose_pattern", event.target.value)} />
        </div>
      </div>
      <div className="grid gap-3 md:grid-cols-3">
        <div className="grid gap-2">
          <Label>Сумма от</Label>
          <Input value={draft.amount_min} onChange={(event) => setField("amount_min", event.target.value)} />
        </div>
        <div className="grid gap-2">
          <Label>Сумма до</Label>
          <Input value={draft.amount_max} onChange={(event) => setField("amount_max", event.target.value)} />
        </div>
        {draft.action === "set_article" ? (
          <div className="grid gap-2">
            <Label>Статья ДДС</Label>
            <Select value={draft.article_id} onValueChange={(value) => setField("article_id", value)}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="none">Не выбрана</SelectItem>
                {articles.map((article) => (
                  <SelectItem key={article.id} value={article.id}>
                    {article.code} · {article.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function ruleDraft(): RuleDraft {
  return {
    action: "set_article",
    amount_max: "",
    amount_min: "",
    article_id: "none",
    counterparty_inn_match: "",
    counterparty_name_pattern: "",
    direction: "any",
    name: "",
    priority: "100",
    provider: "any",
    purpose_pattern: "",
  };
}

function toRulePayload(draft: RuleDraft): ClassificationRuleCreate {
  return {
    action: draft.action,
    amount_max: draft.amount_max || null,
    amount_min: draft.amount_min || null,
    article_id: draft.action === "set_article" && draft.article_id !== "none" ? draft.article_id : null,
    counterparty_inn_match: draft.counterparty_inn_match || null,
    counterparty_name_pattern: draft.counterparty_name_pattern || null,
    direction: draft.direction === "any" ? null : draft.direction,
    name: draft.name,
    priority: Number(draft.priority) || 100,
    provider: draft.provider === "any" ? null : draft.provider,
    purpose_pattern: draft.purpose_pattern || null,
  };
}
