import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, Plus } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardFooter, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import {
  apiErrorMessage,
  classifyOwnerReviewCase,
  createDdsCounterparty,
  dismissOwnerReviewCase,
  getDdsArticles,
  getDdsCounterparties,
  getDdsOwnerReview,
  type ClassifyPayload,
  type CounterpartyRead,
  type DdsArticleRead,
  type OwnerReviewKind,
  type ReconciliationCaseRead,
} from "@/lib/api";
import {
  DirectionBadge,
  PaginationControls,
  ProviderBadge,
  compactText,
  formatDate,
  formatDdsMoney,
} from "@/routes/dds/shared";

const LIMIT = 20;

export function OwnerReviewTab() {
  const [kind, setKind] = useState<OwnerReviewKind | "all">("all");
  const [offset, setOffset] = useState(0);
  const ownerReviewQuery = useQuery({
    queryKey: ["dds", "owner-review", kind, offset],
    queryFn: () => getDdsOwnerReview({ kind, limit: LIMIT, offset }),
  });
  const articlesQuery = useQuery({ queryKey: ["dds", "articles"], queryFn: getDdsArticles });
  const counterpartiesQuery = useQuery({
    queryKey: ["dds", "counterparties", "owner-review"],
    queryFn: () => getDdsCounterparties(),
  });

  return (
    <div className="space-y-5">
      <div className="max-w-xs">
        <Label>Тип кейса</Label>
        <Select
          value={kind}
          onValueChange={(value) => {
            setKind(value as OwnerReviewKind | "all");
            setOffset(0);
          }}
        >
          <SelectTrigger className="mt-2">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">Все</SelectItem>
            <SelectItem value="unclassified_operation">Неразмеченная операция</SelectItem>
            <SelectItem value="invalid_credentials">Проблема с доступом</SelectItem>
            <SelectItem value="unmatched_transfer">Найти перевод</SelectItem>
          </SelectContent>
        </Select>
      </div>

      <div className="grid gap-4">
        {ownerReviewQuery.isLoading
          ? Array.from({ length: 3 }).map((_, index) => (
              <Card key={index}>
                <CardContent className="grid gap-3 p-5">
                  <div className="h-5 w-40 rounded bg-muted" />
                  <div className="h-20 rounded bg-muted" />
                  <div className="h-10 rounded bg-muted" />
                </CardContent>
              </Card>
            ))
          : (ownerReviewQuery.data?.items ?? []).map((item) => (
              <OwnerReviewCard
                articles={articlesQuery.data ?? []}
                counterparties={counterpartiesQuery.data ?? []}
                item={item}
                key={item.id}
              />
            ))}
      </div>

      {!ownerReviewQuery.isLoading && (ownerReviewQuery.data?.items.length ?? 0) === 0 ? (
        <div className="rounded-lg border border-dashed p-6 text-sm text-muted-foreground">
          Очередь owner review пуста.
        </div>
      ) : null}

      <PaginationControls
        limit={LIMIT}
        offset={offset}
        total={ownerReviewQuery.data?.total ?? 0}
        onOffsetChange={setOffset}
      />
    </div>
  );
}

function OwnerReviewCard({
  articles,
  counterparties,
  item,
}: {
  articles: DdsArticleRead[];
  counterparties: CounterpartyRead[];
  item: ReconciliationCaseRead;
}) {
  const queryClient = useQueryClient();
  const operation = item.operation;
  const [articleId, setArticleId] = useState("none");
  const [counterpartyId, setCounterpartyId] = useState("none");
  const [rememberAsRule, setRememberAsRule] = useState(false);
  const [isCreateCounterpartyOpen, setIsCreateCounterpartyOpen] = useState(false);
  const [newCounterpartyName, setNewCounterpartyName] = useState(
    operation?.counterparty_name_raw ?? "",
  );
  const [newCounterpartyInn, setNewCounterpartyInn] = useState(
    operation?.counterparty_inn_raw ?? "",
  );

  const invalidate = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["dds", "owner-review"] }),
      queryClient.invalidateQueries({ queryKey: ["dds", "operations"] }),
      queryClient.invalidateQueries({ queryKey: ["dds", "cashflow"] }),
      queryClient.invalidateQueries({ queryKey: ["dds", "classification-rules"] }),
    ]);
  };

  const classifyMutation = useMutation({
    mutationFn: (payload: ClassifyPayload) => classifyOwnerReviewCase(item.id, payload),
    onSuccess: async (_result, payload) => {
      await invalidate();
      toast.success(payload.remember_as_rule ? "Классифицировано. Правило сохранено" : "Классифицировано");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось классифицировать")),
  });

  const dismissMutation = useMutation({
    mutationFn: () => dismissOwnerReviewCase(item.id),
    onSuccess: async () => {
      await invalidate();
      toast.success("Кейс отложен");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось отложить кейс")),
  });

  const createCounterpartyMutation = useMutation({
    mutationFn: () =>
      createDdsCounterparty({
        name: newCounterpartyName,
        inn: newCounterpartyInn || null,
        type: "legal_entity",
      }),
    onSuccess: async (counterparty) => {
      await queryClient.invalidateQueries({ queryKey: ["dds", "counterparties"] });
      setCounterpartyId(counterparty.id);
      setIsCreateCounterpartyOpen(false);
      toast.success("Контрагент создан");
    },
    onError: (error) => toast.error(apiErrorMessage(error, "Не удалось создать контрагента")),
  });

  function classify(action: ClassifyPayload["action"]) {
    if (action === "set_article" && articleId === "none") {
      toast.error("Выберите статью ДДС");
      return;
    }
    classifyMutation.mutate({
      action,
      article_id: action === "set_article" ? articleId : null,
      counterparty_id: counterpartyId === "none" ? null : counterpartyId,
      remember_as_rule: rememberAsRule,
    });
  }

  const isBusy =
    classifyMutation.isPending || dismissMutation.isPending || createCounterpartyMutation.isPending;

  return (
    <Card>
      <CardHeader className="space-y-3">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div className="flex flex-wrap items-center gap-2">
            <ProviderBadge provider={item.provider} />
            <Badge variant="outline">{caseKindLabel(item.kind)}</Badge>
            {operation ? <DirectionBadge direction={operation.direction} /> : null}
          </div>
          <div className="text-left lg:text-right">
            <div className="text-2xl font-semibold tabular-nums">
              {operation ? formatDdsMoney(operation.amount) : "—"}
            </div>
            <div className="text-sm text-muted-foreground">
              {operation ? formatDate(operation.operation_date) : formatDate(item.created_at)}
            </div>
          </div>
        </div>
      </CardHeader>
      <CardContent className="grid gap-4">
        <div className="grid gap-3 md:grid-cols-3">
          <ReviewField label="Контрагент" value={compactText(operation?.counterparty_name_raw)} />
          <ReviewField label="ИНН" value={compactText(operation?.counterparty_inn_raw)} />
          <ReviewField label="Документ" value={compactText(operation?.document_number)} />
        </div>
        <div className="grid gap-2">
          <Label>Назначение платежа</Label>
          <Textarea
            className="min-h-[96px]"
            readOnly
            value={operation?.payment_purpose ?? JSON.stringify(item.payload, null, 2)}
          />
        </div>
      </CardContent>
      <CardFooter className="grid gap-4 border-t pt-4">
        <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] lg:items-end">
          <div className="grid gap-2">
            <Label>Статья ДДС</Label>
            <Select value={articleId} onValueChange={setArticleId}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
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
          <div className="grid gap-2">
            <Label>Контрагент</Label>
            <Select value={counterpartyId} onValueChange={setCounterpartyId}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="none">Не выбран</SelectItem>
                {counterparties.map((counterparty) => (
                  <SelectItem key={counterparty.id} value={counterparty.id}>
                    {counterparty.name}
                    {counterparty.inn ? ` · ${counterparty.inn}` : ""}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <Button
            onClick={() => setIsCreateCounterpartyOpen((value) => !value)}
            type="button"
            variant="outline"
          >
            <Plus size={16} aria-hidden="true" />
            Создать нового
          </Button>
        </div>

        {isCreateCounterpartyOpen ? (
          <div className="grid gap-3 rounded-lg border bg-muted/30 p-3 md:grid-cols-[minmax(0,1fr)_180px_auto] md:items-end">
            <div className="grid gap-2">
              <Label htmlFor={`counterparty-name-${item.id}`}>Название</Label>
              <Input
                id={`counterparty-name-${item.id}`}
                value={newCounterpartyName}
                onChange={(event) => setNewCounterpartyName(event.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor={`counterparty-inn-${item.id}`}>ИНН</Label>
              <Input
                id={`counterparty-inn-${item.id}`}
                value={newCounterpartyInn}
                onChange={(event) => setNewCounterpartyInn(event.target.value)}
              />
            </div>
            <Button
              disabled={!newCounterpartyName.trim() || createCounterpartyMutation.isPending}
              onClick={() => createCounterpartyMutation.mutate()}
            >
              {createCounterpartyMutation.isPending ? (
                <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
              ) : null}
              Сохранить
            </Button>
          </div>
        ) : null}

        <label className="flex items-start gap-3 text-sm">
          <input
            checked={rememberAsRule}
            className="mt-1 h-4 w-4"
            onChange={(event) => setRememberAsRule(event.target.checked)}
            type="checkbox"
          />
          <span>
            <span className="block font-medium">Запомнить как правило</span>
            <span className="block text-muted-foreground">
              Если включить, операции с тем же ИНН или паттерном в назначении будут
              классифицироваться автоматически.
            </span>
          </span>
        </label>

        <div className="flex flex-wrap gap-2">
          <Button disabled={isBusy} onClick={() => classify("set_article")}>
            {classifyMutation.isPending ? (
              <LoaderCircle className="animate-spin" size={16} aria-hidden="true" />
            ) : null}
            Классифицировать
          </Button>
          <Button disabled={isBusy} onClick={() => classify("mark_internal_transfer")} variant="outline">
            Внутренний перевод
          </Button>
          <Button disabled={isBusy} onClick={() => classify("exclude")} variant="outline">
            Исключить
          </Button>
          <Button disabled={isBusy} onClick={() => dismissMutation.mutate()} variant="ghost">
            Отложить
          </Button>
        </div>
      </CardFooter>
    </Card>
  );
}

function ReviewField({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border bg-muted/20 p-3">
      <div className="text-xs font-medium uppercase text-muted-foreground">{label}</div>
      <div className="mt-1 min-w-0 break-words text-sm">{value}</div>
    </div>
  );
}

function caseKindLabel(kind: string) {
  if (kind === "unclassified_operation") {
    return "Неразмеченная операция";
  }
  if (kind === "invalid_credentials") {
    return "Проблема с доступом";
  }
  if (kind === "unmatched_transfer") {
    return "Найти перевод";
  }
  return kind;
}
